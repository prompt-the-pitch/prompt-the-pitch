"""One viewer session: replays a match in real time and streams everything to the browser.

Two sources of agent output:
- "live": the agents run on Foundry as the match unfolds;
- "cached": a previous run is replayed, perfectly in sync, without calling Foundry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from itg.agents.briefs import LANGUAGES, with_languages
from itg.agents.chat import AnalystChat
from itg.agents.contracts import FanProfile, MomentResult, PlayerReportResult, RecapResult
from itg.forecast import ForecastPoint
from itg.agents.llm import LLM
from itg.agents.orchestrator import MatchOrchestrator
from itg.agents.team import AgentTeam
from itg.api.overlays import cue
from itg.schema import Event, EventType as E, Match

log = logging.getLogger(__name__)
Send = Callable[[dict[str, Any]], Awaitable[None]]
HALF_TIME_PAUSE_S = 3.0
SNAPSHOT_EVERY_S = 1.0


def load_cached_run(path: Path) -> tuple[list[FanProfile], dict[str, MomentResult], dict[str, RecapResult]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = [FanProfile.model_validate(p) for p in data["profiles"]]
    moments = {m["moment_id"]: MomentResult.model_validate(m) for m in data["moments"]}
    recaps = {r["kind"]: RecapResult.model_validate(r) for r in data["recaps"]}
    return profiles, moments, recaps


def load_cached_reports(path: Path) -> list[PlayerReportResult | ForecastPoint]:
    """Timed outputs of a recorded run besides moments and recaps: player check-ins and forecasts."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return ([PlayerReportResult.model_validate(r) for r in data.get("player_reports", [])]
            + [ForecastPoint.model_validate(f) for f in data.get("forecasts", [])])


def match_summary(match: Match) -> dict[str, Any]:
    return {
        "match_id": match.info.match_id,
        "competition": match.info.competition,
        "venue": match.info.venue,
        "weather": match.info.weather,
        "referee": match.info.referee,
        "home_team_id": match.info.home_team_id,
        "away_team_id": match.info.away_team_id,
        "lineups": [lineup.model_dump() for lineup in match.info.lineups],
        "teams": [{"id": t.id, "name": t.name, "short_name": t.short_name, "colors": t.colors,
                   "formation": t.formation,
                   "players": [{"id": p.id, "name": p.name, "short_name": p.short_name, "number": p.number,
                                "position": p.position, "nationality": p.nationality} for p in t.players]}
                  for t in match.teams],
    }


def compact_event(event: Event) -> dict[str, Any]:
    details = {} if event.type == E.PHYSICAL_UPDATE else event.details
    return {"seq": event.seq, "period": event.period, "clock_s": event.clock_s, "clock": event.display_clock,
            "type": event.type.value, "team_id": event.team_id, "player_id": event.player_id,
            "x": event.x, "y": event.y, "end_x": event.end_x, "end_y": event.end_y,
            "outcome": event.outcome, "details": details}


class MatchSession:
    def __init__(self, match: Match, llm: LLM, send: Send, source: Literal["live", "cached"],
                 roles: list[FanProfile], languages: list[str], profiles: list[FanProfile] | None = None,
                 cached_moments: dict[str, MomentResult] | None = None,
                 cached_recaps: dict[str, RecapResult] | None = None,
                 cached_reports: list[PlayerReportResult | ForecastPoint] | None = None, speed: float = 30.0):
        """`roles` x `languages` are the viewers served live; a recorded run brings its own `profiles`."""
        self.match = match
        self.send = send
        self.source = source
        self.speed = speed
        self.roles = roles
        self.languages = languages
        profiles = profiles if profiles is not None else with_languages(roles, languages)
        self.orchestrator = MatchOrchestrator(match, profiles, AgentTeam(llm),
                                              forecast_runs=int(os.environ.get("ITG_FORECAST_RUNS", "120")))
        self.orchestrator.listeners.append(self._on_output)
        self.chat = AnalystChat(self.orchestrator, llm)
        self.cached_moments = cached_moments or {}
        self.cached_recaps = cached_recaps or {}
        self.cached_reports = sorted(cached_reports or [], key=lambda r: (r.period, r.clock_s))
        if source == "live":
            # Live check-ins only for the players someone follows, starting with the featured one.
            self.orchestrator.report_players = {r.favorite_player_id for r in roles if r.favorite_player_id}
        self._playing = asyncio.Event()
        self._playing.set()
        self._last_snapshot = 0.0

    @property
    def profiles(self) -> list[FanProfile]:
        return self.orchestrator.profiles

    def match_message(self) -> dict[str, Any]:
        return {"type": "match", "match": match_summary(self.match), "source": self.source,
                "profiles": [p.model_dump() for p in self.profiles]}

    # ------------------------------------------------------------ controls

    async def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "pause":
            self._playing.clear()
            await self._status("paused")
        elif kind == "resume":
            self._playing.set()
            await self._status("playing")
        elif kind == "speed":
            self.speed = max(0.5, min(600.0, float(message["value"])))
            await self._status("playing" if self._playing.is_set() else "paused")
        elif kind == "add_profile":
            await self._add_role(message["profile"])
        elif kind == "languages":
            await self._add_languages(message["value"])
        elif kind == "follow":
            player_id = str(message.get("player_id", ""))
            if self.source == "live" and any(p.id == player_id for t in self.match.teams for p in t.players):
                self.orchestrator.report_players.add(player_id)

    async def _send_profiles(self) -> None:
        self.orchestrator.profiles = with_languages(self.roles, self.languages)
        await self.send({"type": "profiles", "profiles": [p.model_dump() for p in self.profiles]})

    async def _add_role(self, raw: dict[str, Any]) -> None:
        if self.source != "live":
            await self.send({"type": "error", "message": "Custom viewers need live agents."})
            return
        role = FanProfile.model_validate(raw)
        role.role = role.role or role.id
        self.roles = [r for r in self.roles if r.role != role.role] + [role]
        await self._send_profiles()

    async def _add_languages(self, languages: list[str]) -> None:
        """Live sessions only write the languages viewers asked for, from now on."""
        if self.source != "live":
            return
        wanted = [lang for lang in languages if lang in LANGUAGES]
        self.languages = list(dict.fromkeys([*self.languages, *wanted]))
        await self._send_profiles()

    async def _status(self, state: str) -> None:
        await self.send({"type": "status", "state": state, "speed": self.speed, "source": self.source})

    # --------------------------------------------------------------- stream

    async def _on_output(self, output: MomentResult | RecapResult | PlayerReportResult | ForecastPoint) -> None:
        if isinstance(output, ForecastPoint):
            if self.source == "cached":
                self.orchestrator.forecasts.append(output)  # Recorded forecasts feed the stands and the chat.
            await self.send({"type": "forecast", "forecast": output.model_dump(mode="json")})
        elif isinstance(output, PlayerReportResult):
            await self.send({"type": "player_report", "report": output.model_dump(mode="json")})
        elif isinstance(output, MomentResult):
            cues = [c for p in self.profiles if (c := cue(output, p, self.match.info.match_id))]
            await self.send({"type": "moment", "moment": output.model_dump(mode="json"), "cues": cues})
        else:
            await self.send({"type": "recap", "recap": output.model_dump(mode="json")})

    async def _wait(self, seconds: float) -> None:
        """Sleep in match time, honouring pause and speed changes."""
        remaining = seconds
        while remaining > 0:
            await self._playing.wait()
            step = min(0.2, remaining / self.speed)
            await asyncio.sleep(step)
            remaining -= step * self.speed

    async def _snapshot(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_snapshot >= SNAPSHOT_EVERY_S:
            self._last_snapshot = now
            snap = self.orchestrator.engine.snapshot(recent_signals=0)
            await self.send({"type": "snapshot", "snapshot": snap.model_dump(mode="json")})

    async def run(self) -> None:
        await self.send(self.match_message())
        await self._status("playing")
        previous: Event | None = None
        for event in self.match.events:
            if previous is not None:
                if event.period == previous.period:
                    await self._wait(event.clock_s - previous.clock_s)
                else:
                    await self._wait(HALF_TIME_PAUSE_S * self.speed)
            previous = event
            if self.source == "live":
                signals = await self.orchestrator.ingest(event)
            else:
                signals = self._ingest_cached(event)
            await self.send({"type": "event", "event": compact_event(event)})
            while self.cached_reports and (self.cached_reports[0].period, self.cached_reports[0].clock_s) <= (
                    event.period, event.clock_s):
                await self._on_output(self.cached_reports.pop(0))
            for signal in signals:
                await self.send({"type": "signal", "signal": signal.model_dump(mode="json")})
                if self.source == "cached":
                    await self._emit_cached(signal)
            await self._snapshot(force=event.type in (E.GOAL, E.PERIOD_END, E.MATCH_END))
        await self.orchestrator._wait_pending()
        await self._status("ended")

    def _ingest_cached(self, event: Event):
        return self.orchestrator.engine.ingest(event)

    async def _emit_cached(self, signal) -> None:
        moment = self.cached_moments.get(signal.id)
        if moment is not None:
            self.orchestrator.memory.append(moment)
            await self._on_output(moment)
        recap = self.cached_recaps.get(signal.type)
        if recap is not None:
            self.orchestrator.recaps.append(recap)
            await self._on_output(recap)
