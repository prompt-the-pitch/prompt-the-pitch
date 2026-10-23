"""Match orchestrator: from the live event stream to explained, personalised moments.

It owns the shared match state (stats engine, memory of processed moments, recaps),
decides which signals deserve the agents' attention for which viewer (triage), runs the
per-moment workflows concurrently, writes the half-time and full-time recaps, and every
15 minutes asks for a check-in on each followed player. Every minute it also replays the rest
of the match hundreds of times to estimate win probabilities, and measures how much each key
moment changed them.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from itg.agents.briefs import build_brief, player_payload, score_line
from itg.agents.contracts import AgentTrace, FanProfile, MomentResult, PlayerReportResult, RecapResult
from itg.agents.team import AgentTeam
from itg.agents.workflow import MomentJob, run_moment
from itg.forecast import ForecastPoint, Scenario, compare, forecast, state_from_engine
from itg.schema import Event, EventType as E, Match
from itg.stats.engine import StatsEngine
from itg.stats.models import Signal, Snapshot
from itg.synthetic.replay import replay

log = logging.getLogger(__name__)

Output = MomentResult | RecapResult | PlayerReportResult | ForecastPoint
Listener = Callable[[Output], Awaitable[None] | None]

# Signal types each kind of viewer cares about more (negative = lower threshold).
_ANALYST_BIAS = {"tactical_shift": -0.15, "phase_change": -0.1, "dominance": -0.1, "high_turnover": -0.05}
_CASUAL_BIAS = {"speed_alert": -0.15, "thunderbolt": -0.15, "phase_change": +0.15, "tactical_shift": +0.05}
_NEVER = {"half_time", "full_time", "added_time", "substitution", "distance_milestone"}
# Player check-ins at these (period, minute); half-time and full-time are added on their signals.
REPORT_MARKS = ((1, 15), (1, 30), (2, 60), (2, 75))
# Moments whose effect on the result is measured by replaying the match without (or with) them.
_IMPACT_TYPES = {"goal", "big_chance", "big_save", "card"}


class MatchOrchestrator:
    def __init__(self, match: Match, profiles: list[FanProfile], team: AgentTeam, max_concurrency: int = 4,
                 forecast_runs: int = 120, every_minute: bool = False):
        """`every_minute`: forecast each minute even if the previous one is still running (recordings).
        Live sessions skip a minute instead, so forecasts never fall behind the match.
        `forecast_runs=0` turns forecasts and impacts off."""
        self.match = match
        self.profiles = profiles
        self.team = team
        self.engine = StatsEngine.for_match(match)
        self.memory: list[MomentResult] = []
        self.recaps: list[RecapResult] = []
        self.listeners: list[Listener] = []
        self.report_players: set[str] = set()
        self.player_reports: list[PlayerReportResult] = []
        self._marks_done: set[tuple[int, int]] = set()
        self._last_report_at: tuple[int, float] = (1, 0.0)
        self.forecasts: list[ForecastPoint] = []
        self.forecast_runs = forecast_runs
        self.every_minute = every_minute
        self._minute: tuple[int, int] | None = None
        self._forecasting = False
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    # ---------------------------------------------------------------- triage

    def audience(self, signal: Signal) -> list[FanProfile]:
        """The viewers for whom this signal deserves an overlay."""
        if signal.type in _NEVER:
            return []
        selected = []
        for profile in self.profiles:
            analyst = profile.mode == "analyst"
            threshold = (0.45 if analyst else 0.5) + (_ANALYST_BIAS if analyst else _CASUAL_BIAS).get(signal.type, 0)
            if profile.favorite_team_id and profile.favorite_team_id == signal.team_id:
                threshold -= 0.1
            if profile.favorite_player_id and profile.favorite_player_id in signal.player_ids:
                threshold = min(threshold, 0.2 if profile.player_focus else threshold - 0.15)
            if signal.importance >= threshold:
                selected.append(profile)
        return selected

    # ----------------------------------------------------------------- flow

    def languages(self) -> list[str]:
        return list(dict.fromkeys(p.language for p in self.profiles)) or ["en"]

    async def ingest(self, event: Event) -> list[Signal]:
        signals = self.engine.ingest(event)
        self._maybe_forecast(event)
        for period, minute in REPORT_MARKS:
            if (period, minute) not in self._marks_done and event.period == period and event.clock_s >= minute * 60:
                self._marks_done.add((period, minute))
                self._schedule_reports(f"{minute}'", event.period, event.clock_s)
        for signal in signals:
            if signal.type in ("half_time", "full_time"):
                self._schedule_reports(signal.display_clock, signal.period, signal.clock_s,
                                       final=signal.type == "full_time")
                await self._wait_pending()
                await self._recap(signal)
                continue
            profiles = self.audience(signal)
            if profiles:
                measure = signal.type in _IMPACT_TYPES and self.forecast_runs > 0
                state = state_from_engine(self.engine, self.match) if measure else None
                self._start(self._moment(signal, profiles, self.engine.snapshot(), state))
        return signals

    # ------------------------------------------------------------- forecasts

    def latest_forecast(self) -> ForecastPoint | None:
        return max(self.forecasts, key=lambda f: (f.period, f.clock_s), default=None)

    def win_probability(self) -> dict[str, float] | None:
        """Latest forecast keyed by team name, as agents read it."""
        point = self.latest_forecast()
        if point is None:
            return None
        home, away = self.match.teams
        return {home.name: point.home, "draw": point.draw, away.name: point.away}

    def _maybe_forecast(self, event: Event) -> None:
        if self.forecast_runs <= 0:
            return
        if event.type == E.MATCH_END:
            home, away = self.engine.score["home"], self.engine.score["away"]
            final = ForecastPoint(period=2, minute=int(event.clock_s // 60) + 1, clock_s=event.clock_s,
                                  clock=event.display_clock,
                                  home=float(home > away), draw=float(home == away), away=float(home < away))
            self.forecasts.append(final)
            self._start(self._emit(final))
            return
        key = (event.period, int(event.clock_s // 60))
        if key == self._minute or event.type == E.PERIOD_END:
            return
        self._minute = key
        if self._forecasting and not self.every_minute:
            return
        state = state_from_engine(self.engine, self.match)
        self._start(self._forecast(key, event.clock_s, event.display_clock, state))

    async def _forecast(self, key: tuple[int, int], clock_s: float, clock: str, state) -> None:
        self._forecasting = True
        try:
            home, away = self.match.teams
            result = await asyncio.to_thread(forecast, home, away, state, self.forecast_runs, key[0] * 1000 + key[1])
        finally:
            self._forecasting = False
        point = ForecastPoint(period=key[0], minute=key[1] + 1, clock_s=clock_s, clock=clock,
                              home=result.home, draw=result.draw, away=result.away)
        self.forecasts.append(point)
        await self._emit(point)

    def _impact(self, signal: Signal, state) -> dict[str, Any] | None:
        """How much the moment changed the acting team's chances, by replaying the match without it."""
        home_id = self.match.info.home_team_id
        team_id = signal.team_id
        if signal.type == "big_save":  # The save is the keeper's; the chance was the other team's.
            team_id = next(t.id for t in self.match.teams if t.id != signal.team_id)
        side = "home" if team_id == home_id else "away"
        if signal.type == "goal":
            scenario, label = Scenario(**{f"{side}_goals": -1}), "without this goal"
        elif signal.type in ("big_chance", "big_save"):
            scenario, label = Scenario(**{f"{side}_goals": 1}), "had the chance been scored"
        elif signal.type == "card" and signal.data.get("card") != "yellow" and signal.player_ids:
            scenario, label = Scenario(reinstate=[signal.player_ids[0]]), "with eleven players"
        else:
            return None
        actual, alternative = compare(self.match, state, scenario, self.forecast_runs, seed=int(signal.clock_s))
        chance, chance_if = getattr(actual, side), getattr(alternative, side)
        team_name = next(t.name for t in self.match.teams if t.id == team_id)
        return {"win_probability": {"home": actual.home, "draw": actual.draw, "away": actual.away},
                "impact": {"team": team_name, "win_chance": chance, "win_chance_if": chance_if,
                           "scenario": label, "delta": round(chance - chance_if, 3)}}

    async def run(self, speed: float = 1e9, on_event: Callable[[Event], Any] | None = None) -> None:
        async for event in replay(self.match, speed):
            if on_event:
                on_event(event)
            await self.ingest(event)
        await self._wait_pending()

    def _start(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule_reports(self, clock: str, period: int, clock_s: float, final: bool = False) -> None:
        """Ask for a check-in on every followed player still involved in the match."""
        if not self.report_players:
            return
        snap = self.engine.snapshot()
        since, self._last_report_at = self._last_report_at, (period, clock_s)
        for player_id in sorted(self.report_players):
            player = snap.players.get(player_id)
            if player is None or player.minutes < 5 or not (player.on_pitch or (final and player.minutes >= 20)):
                continue
            involvement = [s for s in self.engine.signals
                           if (s.period, s.clock_s) > since and player_id in s.player_ids]
            self._start(self._player_report(player_id, clock, period, clock_s, snap, involvement))

    async def _player_report(self, player_id: str, clock: str, period: int, clock_s: float,
                             snap: Snapshot, involvement: list[Signal]) -> None:
        payload = player_payload(player_id, snap, involvement)
        languages = self.languages()
        try:
            results = await asyncio.gather(*(self.team.player_report(payload, lang) for lang in languages))
        except Exception:
            log.exception("player report failed for %s", player_id)
            return
        report = PlayerReportResult(
            player_id=player_id, period=period, clock_s=clock_s, clock=clock,
            reports={lang: r for lang, (r, _) in zip(languages, results)}, trace=[t for _, t in results])
        self.player_reports.append(report)
        await self._emit(report)

    def cancel(self) -> None:
        """Stop the agent work still in flight (the viewer left)."""
        for task in list(self._tasks):
            task.cancel()

    async def _wait_pending(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _emit(self, output: Output) -> None:
        for listener in self.listeners:
            result = listener(output)
            if inspect.isawaitable(result):
                await result

    async def _moment(self, signal: Signal, profiles: list[FanProfile], snap: Snapshot, state=None) -> None:
        async with self._semaphore:
            if state is not None:
                try:
                    impact = await asyncio.to_thread(self._impact, signal, state)
                    if impact:
                        signal.data.update(impact)
                except Exception:
                    log.exception("impact simulation failed for %s", signal.id)
            recent = sorted(self.memory, key=lambda r: (r.signal.period, r.signal.clock_s))
            brief = build_brief(signal, snap, recent, self.win_probability())
            try:
                result = await run_moment(self.team, MomentJob(signal, brief, profiles))
            except Exception:
                log.exception("moment workflow failed for %s", signal.id)
                return
        self.memory.append(result)
        await self._emit(result)

    # --------------------------------------------------------------- recaps

    def recap_payload(self, snap: Snapshot, kind: str) -> dict[str, Any]:
        home, away = snap.teams.values()
        moments = sorted(self.memory, key=lambda r: (r.signal.period, r.signal.clock_s))
        top = sorted(snap.players.values(), key=lambda p: p.rating, reverse=True)[:3]
        best = top[0]
        keys = ("goals", "possession_pct", "shots", "shots_on_target", "xg", "passes", "pass_accuracy",
                "ppda", "high_turnovers", "distance_km")
        return {
            "kind": kind,
            "score": score_line(snap, home.name, away.name),
            "team_stats": {t.name: {k: getattr(t, k) for k in keys} for t in (home, away)},
            "top_players": [{"name": p.short_name, "team": snap.teams[p.team_id].name, "rating": p.rating,
                             "goals": p.goals, "assists": p.assists, "xg": p.xg} for p in top],
            "player_of_the_match": f"{best.short_name} ({snap.teams[best.team_id].name})",
            "key_moments": [{"clock": r.signal.display_clock, "headline": r.signal.headline,
                             "why": r.insight.why_it_matters}
                            for r in moments if r.signal.importance >= 0.5][-10:],
        }

    async def _recap(self, signal: Signal) -> None:
        start = time.perf_counter()
        payload = self.recap_payload(self.engine.snapshot(), signal.type)
        results = await asyncio.gather(*(self.team.recap(payload, p) for p in self.profiles))
        recap = RecapResult(
            kind=signal.type, clock=signal.display_clock,
            recaps={p.id: r for p, (r, _) in zip(self.profiles, results)},
            trace=[t for _, t in results] or [AgentTrace(agent="RecapWriter", status="skipped",
                                                         latency_ms=int(1000 * (time.perf_counter() - start)))],
        )
        self.recaps.append(recap)
        await self._emit(recap)
