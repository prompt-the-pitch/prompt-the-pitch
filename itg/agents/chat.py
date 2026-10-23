"""Analyst chat: a viewer asks questions, an agent answers with tools over the live state.

Besides live statistics, the agent can replay the rest of the match under a scenario
("what if they had scored at 16'?") and put a broadcast graphic on the pitch (shot map,
passing network, a player's touches, a pressing map) to illustrate its answer.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from itg.agents import prompts
from itg.agents.llm import LLM
from itg.agents.orchestrator import MatchOrchestrator
from itg.forecast import Forecast, Scenario, compare, state_at, state_from_engine
from itg.schema import EventType as E
from itg.stats import metrics as m

WHAT_IF_RUNS = 200
ON_BALL = {E.PASS, E.CARRY, E.DRIBBLE, E.SHOT}


class ChatAnswer(BaseModel):
    answer: str
    graphic: dict[str, Any] | None = None


@dataclass
class _AskContext:
    graphic: dict[str, Any] | None = None
    calls: list[str] = field(default_factory=list)


def _percent(forecast: Forecast, home: str, away: str) -> dict[str, str]:
    return {home: f"{forecast.home:.0%}", "draw": f"{forecast.draw:.0%}", away: f"{forecast.away:.0%}"}


class AnalystChat:
    def __init__(self, orchestrator: MatchOrchestrator, llm: LLM):
        self.orchestrator = orchestrator
        self.llm = llm

    # --------------------------------------------------------------- lookups

    def _team(self, name: str | None):
        if not name:
            return None
        needle = name.lower().strip()
        return next((t for t in self.orchestrator.match.teams
                     if needle in (t.name.lower(), t.short_name.lower(), t.id) or needle in t.name.lower()), None)

    def _player(self, name: str | None):
        if not name:
            return None
        needle = name.lower().strip().split()[-1]
        return next((p for t in self.orchestrator.match.teams for p in t.players if needle in p.name.lower()), None)

    def _events_so_far(self):
        last = self.orchestrator.engine.last
        if last is None:
            return []
        return [e for e in self.orchestrator.match.events if (e.period, e.clock_s) <= (last.period, last.clock_s)]

    def _frame(self, e, x, y, team_id):
        return m.to_team_frame(x, y, team_id == self.orchestrator.match.info.home_team_id, e.period)

    # ----------------------------------------------------------------- tools

    def tools(self, ctx: _AskContext) -> list:
        engine = self.orchestrator.engine
        match = self.orchestrator.match
        names = {p.id: p.short_name for t in match.teams for p in t.players}

        def get_match_state() -> str:
            """Current score, clock, momentum (-100 away to +100 home), game phase and win probabilities."""
            snap = engine.snapshot(recent_signals=0)
            home, away = snap.teams.values()
            return json.dumps({"clock": snap.display_clock, "score": f"{home.name} {snap.score['home']}-"
                               f"{snap.score['away']} {away.name}", "momentum": snap.momentum,
                               "phase": snap.phase.model_dump() if snap.phase else None,
                               "win_probability": self.orchestrator.win_probability()})

        def get_team_stats(team: Annotated[str, Field(description="Team name or short name.")]) -> str:
            """Live statistics of one team: possession, shots, xG, passes, pressing (PPDA), distance."""
            t = self._team(team)
            return engine.snapshot(recent_signals=0).teams[t.id].model_dump_json() if t else f"Unknown team {team!r}."

        def get_player_stats(player: Annotated[str, Field(description="Player name or surname.")]) -> str:
            """Live statistics of one player: goals, xG, passes, pass difficulty, speed, distance, rating."""
            p = self._player(player)
            return engine.snapshot(recent_signals=0).players[p.id].model_dump_json() if p else f"No player {player!r}."

        def get_top_players(
            metric: Annotated[str, Field(description="rating, xg, passes, distance_km, top_speed_kmh, "
                                                     "pass_value_added, key_passes, tackles or sprints")] = "rating",
            limit: int = 5,
        ) -> str:
            """Players ranked by a metric."""
            snap = engine.snapshot(recent_signals=0)
            players = [p for p in snap.players.values() if p.minutes > 0]
            if not players or not hasattr(players[0], metric):
                return f"Unknown metric {metric!r}."
            ranked = sorted(players, key=lambda p: getattr(p, metric), reverse=True)[:limit]
            return json.dumps([{"name": p.short_name, "team": snap.teams[p.team_id].name,
                                metric: getattr(p, metric)} for p in ranked])

        def get_key_moments(limit: int = 8) -> str:
            """The most recent key moments with why they mattered (and how they changed win chances)."""
            moments = sorted(self.orchestrator.memory, key=lambda r: (r.signal.period, r.signal.clock_s))
            return json.dumps([{"clock": r.signal.display_clock, "what": r.signal.headline,
                                "why": r.insight.why_it_matters, "impact": r.signal.data.get("impact")}
                               for r in moments[-limit:]], ensure_ascii=False)

        async def simulate_what_if(
            minute: Annotated[int | None, Field(description="Match minute the scenario starts from, e.g. 16. "
                                                            "Omit for 'from now'.")] = None,
            goals_home: Annotated[int, Field(description="Goals added to the home team (negative removes).")] = 0,
            goals_away: Annotated[int, Field(description="Goals added to the away team (negative removes).")] = 0,
            red_cards_home: int = 0,
            red_cards_away: int = 0,
            tactic_home: Annotated[Literal["high_press", "low_block", "go_direct", "possession_control"] | None,
                                   Field(description="New tactic for the home team.")] = None,
            tactic_away: Literal["high_press", "low_block", "go_direct", "possession_control"] | None = None,
            player_off: Annotated[str | None, Field(description="Player taken off (name).")] = None,
            player_on: Annotated[str | None, Field(description="Substitute brought on (name).")] = None,
        ) -> str:
            """Replay the rest of the match hundreds of times with a scenario and compare win probabilities
            with what the match looked like without it. Use for any 'what if' question."""
            home, away = match.teams
            if minute is not None:
                minute = max(1, min(95, minute))
                period, clock = (1, minute * 60) if minute <= 45 else (2, 2700 + (minute - 45) * 60)
                if engine.last is None or (period, clock) > (engine.last.period, engine.last.clock_s):
                    minute = None  # Never start from a moment the viewers have not seen yet.
            if minute is None:
                state, start = state_from_engine(engine, match), engine.last.display_clock if engine.last else "0'"
            else:
                _, state = state_at(match, period, clock)
                start = f"{minute}'"
            subs = []
            off, on = self._player(player_off), self._player(player_on)
            if off and on:
                subs.append((off.id, on.id))
            scenario = Scenario(home_goals=goals_home, away_goals=goals_away, home_red_cards=red_cards_home,
                                away_red_cards=red_cards_away, home_tactic=tactic_home, away_tactic=tactic_away,
                                substitutions=subs)
            base, alt = await asyncio.to_thread(compare, match, state, scenario, WHAT_IF_RUNS, 2024)
            labels = [home.name, "draw", away.name]
            ctx.graphic = {"kind": "what_if", "title": f"What if? From {start}",
                           "labels": labels, "before": [base.home, base.draw, base.away],
                           "after": [alt.home, alt.draw, alt.away],
                           "colors": [home.colors[0], "#9aa0a8", away.colors[0]]}
            ctx.calls.append("what_if")
            return json.dumps({
                "from": start, "score_then": f"{home.name} {state.home_goals}-{state.away_goals} {away.name}",
                "scenario": scenario.model_dump(exclude_defaults=True), "without_scenario": _percent(base, home.name, away.name),
                "with_scenario": _percent(alt, home.name, away.name),
                "expected_goals_with_scenario": {home.name: alt.home_goals, away.name: alt.away_goals},
                "method": f"The rest of the match was replayed {WHAT_IF_RUNS} times with and without the scenario, "
                          "with the same random seeds.",
            }, ensure_ascii=False)

        def show_shot_map(team: Annotated[str, Field(description="Team name.")]) -> str:
            """Draw a team's shots on the pitch (size = xG). Returns a summary of the shots."""
            t = self._team(team)
            if not t:
                return f"Unknown team {team!r}."
            shots = [s for s in engine.snapshot(recent_signals=0).shots if s.team_id == t.id]
            ctx.graphic = {"kind": "shot_map", "title": f"{t.name}: shots", "team_id": t.id, "points": [
                {"x": s.x, "y": s.y, "r": round(0.9 + 4 * s.xg, 2), "style": "goal" if s.result == "goal" else "shot",
                 "label": f"{s.clock} {names[s.player_id]}"} for s in shots]}
            inside = sum(m.in_box(s.x, s.y) for s in shots)
            return json.dumps({"shots": len(shots), "inside_box": inside, "goals": sum(s.result == "goal" for s in shots),
                               "xg": round(sum(s.xg for s in shots), 2)})

        def show_pass_network(team: Annotated[str, Field(description="Team name.")]) -> str:
            """Draw a team's passing network (average positions, most used links). Returns its main links."""
            t = self._team(team)
            if not t:
                return f"Unknown team {team!r}."
            net = engine.snapshot(recent_signals=0).pass_networks[t.id]
            pos = {n.player_id: n for n in net.nodes}
            numbers = {p.id: p.number for p in t.players}
            ctx.graphic = {"kind": "pass_network", "title": f"{t.name}: passing network", "team_id": t.id,
                           "lines": [{"x1": pos[e.source].x, "y1": pos[e.source].y, "x2": pos[e.target].x,
                                      "y2": pos[e.target].y, "w": e.passes} for e in net.edges],
                           "points": [{"x": n.x, "y": n.y, "r": 1.6, "style": "node", "label": str(numbers[n.player_id])}
                                      for n in net.nodes]}
            return json.dumps({"top_links": [f"{names[e.source]} -> {names[e.target]}: {e.passes}" for e in net.edges[:5]]})

        def show_player_touches(player: Annotated[str, Field(description="Player name or surname.")]) -> str:
            """Draw where a player has had the ball so far. Returns how many touches and where."""
            p = self._player(player)
            if not p:
                return f"No player {player!r}."
            team_id = next(t.id for t in match.teams if p in t.players)
            points = [self._frame(e, e.x, e.y, team_id) for e in self._events_so_far()
                      if e.player_id == p.id and e.type in ON_BALL and e.x is not None]
            ctx.graphic = {"kind": "touches", "title": f"{p.short_name}: touches", "team_id": team_id,
                           "points": [{"x": round(x, 1), "y": round(y, 1), "r": 0.8, "style": "dot"} for x, y in points]}
            third = lambda x: "defensive third" if x < 35 else ("middle third" if x < 70 else "final third")
            zones = {z: sum(third(x) == z for x, _ in points) for z in ("defensive third", "middle third", "final third")}
            return json.dumps({"touches": len(points), "by_zone": zones})

        def show_pressing(team: Annotated[str, Field(description="Team name.")],
                          minutes: Annotated[int, Field(description="Look-back window in minutes.")] = 15) -> str:
            """Draw where a team has pressed the ball recently. Returns how many pressures and how high."""
            t = self._team(team)
            if not t:
                return f"Unknown team {team!r}."
            last = engine.last
            since = (last.clock_s - minutes * 60) if last else 0
            points = [self._frame(e, e.x, e.y, t.id) for e in self._events_so_far()
                      if e.team_id == t.id and e.type == E.PRESSURE and e.x is not None
                      and e.period == (last.period if last else 1) and e.clock_s >= since]
            ctx.graphic = {"kind": "pressing", "title": f"{t.name}: pressing, last {minutes} min", "team_id": t.id,
                           "points": [{"x": round(x, 1), "y": round(y, 1), "r": 0.9, "style": "press"} for x, y in points]}
            return json.dumps({"pressures": len(points), "in_opponent_half": sum(x >= 52.5 for x, _ in points)})

        return [get_match_state, get_team_stats, get_player_stats, get_top_players, get_key_moments,
                simulate_what_if, show_shot_map, show_pass_network, show_player_touches, show_pressing]

    # ------------------------------------------------------------------- ask

    async def ask(self, question: str) -> ChatAnswer:
        if not self.llm.online:
            snap = self.orchestrator.engine.snapshot(recent_signals=0)
            home, away = snap.teams.values()
            return ChatAnswer(answer=(
                f"(offline) {snap.display_clock}: {home.name} {snap.score['home']}-{snap.score['away']} "
                f"{away.name}. Possession {home.possession_pct}% / {away.possession_pct}%, xG {home.xg} / {away.xg}."))
        ctx = _AskContext()
        answer = await self.llm.chat("MatchAnalyst", prompts.ANALYST_CHAT, question, self.tools(ctx))
        return ChatAnswer(answer=answer, graphic=ctx.graphic)
