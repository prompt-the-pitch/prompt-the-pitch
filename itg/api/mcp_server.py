"""MCP server: the match intelligence as tools for GitHub Copilot (or any MCP client).

A developer connects VS Code to `<site>/mcp` and asks Copilot about a recorded match or a
watch room that is playing right now; Copilot calls these tools, then can write code with
the data (a chart of the win probability, a shot map in matplotlib...).

The tools reuse the analyst chat's tools over the deterministic stats engine and the
Monte Carlo simulator: they never call the language model, so the endpoint costs nothing
in Foundry. What-if replays use CPU, so they are rate limited.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal

from fastapi import FastAPI, HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from itg.agents.chat import AnalystChat, _AskContext
from itg.agents.orchestrator import MatchOrchestrator
from itg.agents.team import AgentTeam
from itg.api.session import load_cached_reports, load_cached_run
from itg.forecast import ForecastPoint
from itg.schema import EventType as E
from itg.schema import Match

WHAT_IF_PER_MINUTE = 20
WHAT_IF_PARALLEL = 2
COORDINATES = ("Metres on a 105 x 68 pitch, in the team's attacking frame: it attacks towards x = 105; "
               "y = 0 is its right touchline.")

INSTRUCTIONS = """\
Prompt the Pitch is a match-intelligence app on synthetic football matches between fictional
clubs (Expected Ls, Press Gang, Carb Loaders, Benchwarmers FC). Call list_matches first: it gives
the recorded matches (match_id) and the watch rooms open right now (room code).
Every match tool takes either `room` (a watch room: the state at the moment it has reached) or
`match_id` (a recorded match) with an optional `minute` (the state at that minute; full time if
omitted). Win probabilities come from replaying the rest of the match hundreds of times with
the match simulator. All statistics are computed from the event stream, never guessed.
"""

MatchId = Annotated[str | None, Field(description="Recorded match id from list_matches, e.g. 'exl-prg-42'.")]
RoomCode = Annotated[str | None, Field(description="Watch room code shown on the site, e.g. 'A1B2C3'. "
                                                   "Takes precedence over match_id.")]
Minute = Annotated[int | None, Field(description="With match_id: the match minute to look at (1-95). "
                                                 "Omit for full time. Ignored for a watch room.")]
TeamName = Annotated[str, Field(description="Team name or short name, e.g. 'Press Gang' or 'PRG'.")]
Tactic = Literal["high_press", "low_block", "go_direct", "possession_control"]


@dataclass
class _View:
    """One match seen at one moment, with the analyst tools over it."""

    match: Match
    orchestrator: MatchOrchestrator
    where: str

    def context(self) -> str:
        snap = self.orchestrator.engine.snapshot(recent_signals=0)
        home, away = self.match.teams
        return (f"{home.name} {snap.score['home']}-{snap.score['away']} {away.name}, {snap.display_clock} "
                f"({self.where})")

    @property
    def chat(self) -> AnalystChat:
        return AnalystChat(self.orchestrator, self.orchestrator.team.llm)

    def tool(self, name: str):
        return {f.__name__: f for f in self.chat.tools(_AskContext())}[name]

    def answer(self, raw: str, **extra: Any) -> str:
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = raw
        return json.dumps({"match": self.context(), "result": result, **extra}, ensure_ascii=False)


class _RateLimit:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.calls: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        if len(self.calls) >= self.per_minute:
            return False
        self.calls.append(now)
        return True


def _cutoff(minute: int) -> tuple[int, float]:
    minute = max(1, min(95, minute))
    return (1, minute * 60.0) if minute <= 45 else (2, 2700.0 + (minute - 45) * 60.0)


def create_mcp(app: FastAPI, runs_dir: Path, get_match: Callable[[str], Match],
               recorded_matches: Callable[[], Awaitable[list[dict]]]) -> MCPServer:
    """The MCP server over the app's matches (`get_match`, `recorded_matches`) and watch rooms."""
    mcp = MCPServer("Prompt the Pitch", title="Prompt the Pitch: match intelligence",
                    description="Live football statistics, win probabilities and what-if simulations.",
                    instructions=INSTRUCTIONS)
    what_if_limit = _RateLimit(WHAT_IF_PER_MINUTE)
    what_if_slots = asyncio.Semaphore(WHAT_IF_PARALLEL)

    def recorded(match_id: str, minute: int | None) -> _View:
        match = get_match(match_id)
        orchestrator = MatchOrchestrator(match, [], AgentTeam(app.state.llm), forecast_runs=0)
        run = runs_dir / f"{match_id}.json"
        moments, timed = {}, []
        if run.is_file():
            _, moments, _ = load_cached_run(run)
            timed = load_cached_reports(run)
        stop = _cutoff(minute) if minute is not None else (9, 0.0)
        for event in match.events:
            if (event.period, event.clock_s) > stop:
                break
            for signal in orchestrator.engine.ingest(event):
                if signal.id in moments:
                    orchestrator.memory.append(moments[signal.id])
        last = orchestrator.engine.last
        reached = (last.period, last.clock_s) if last else (1, 0.0)
        orchestrator.forecasts = [f for f in timed if isinstance(f, ForecastPoint)
                                  and (f.period, f.clock_s) <= reached]
        return _View(match, orchestrator, f"recorded match {match_id}" + (f" at {minute}'" if minute else ""))

    def view(match_id: str | None, room: str | None, minute: int | None) -> _View:
        if room:
            live = app.state.rooms.get(room.strip().upper())
            if live is None:
                raise ToolError(f"No open watch room {room!r}. Call list_matches to see the open rooms.")
            return _View(live.match, live.session.orchestrator, f"watch room {live.code}, {live.source}")
        if not match_id:
            raise ToolError("Pass a match_id or a room code (see list_matches).")
        try:
            return recorded(match_id, minute)
        except HTTPException as exc:
            raise ToolError(f"{exc.detail}. Call list_matches to see the recorded matches.") from None

    @mcp.tool()
    async def list_matches() -> str:
        """Recorded matches (with final score) and the watch rooms open right now (with their clock)."""
        matches = []
        for item in await recorded_matches():
            match = get_match(item["match_id"])
            goals = [e for e in match.events if e.type == E.GOAL]
            home_goals = sum(e.team_id == match.info.home_team_id for e in goals)
            matches.append({"match_id": item["match_id"], "home": item["home"], "away": item["away"],
                            "final_score": f"{home_goals}-{len(goals) - home_goals}",
                            "agent_run_recorded": item["cached_run"]})
        rooms = []
        for live in app.state.rooms.values():
            last = live.session.orchestrator.engine.last
            rooms.append({"room": live.code, "match_id": live.match.info.match_id,
                          "clock": last.display_clock if last else "0'", "agents": live.source,
                          "fans": len([m for m in live.members.values() if not m.observer])})
        return json.dumps({"recorded_matches": matches, "watch_rooms": rooms}, ensure_ascii=False)

    @mcp.tool()
    def get_match_state(match_id: MatchId = None, room: RoomCode = None, minute: Minute = None) -> str:
        """Score, clock, momentum (-100 away to +100 home), game phase and win probabilities."""
        v = view(match_id, room, minute)
        return v.answer(v.tool("get_match_state")())

    @mcp.tool()
    def get_team_stats(team: TeamName, match_id: MatchId = None, room: RoomCode = None,
                       minute: Minute = None) -> str:
        """One team's statistics: possession, shots, xG, passes, pressing (PPDA), distance covered."""
        v = view(match_id, room, minute)
        return v.answer(v.tool("get_team_stats")(team))

    @mcp.tool()
    def get_player_stats(player: Annotated[str, Field(description="Player name or surname.")],
                         match_id: MatchId = None, room: RoomCode = None, minute: Minute = None) -> str:
        """One player's statistics: goals, xG, passes, pass difficulty, top speed, distance, rating."""
        v = view(match_id, room, minute)
        return v.answer(v.tool("get_player_stats")(player))

    @mcp.tool()
    def get_top_players(
        metric: Annotated[str, Field(description="rating, xg, passes, distance_km, top_speed_kmh, "
                                                 "pass_value_added, key_passes, tackles or sprints")] = "rating",
        limit: Annotated[int, Field(ge=1, le=22)] = 5,
        match_id: MatchId = None, room: RoomCode = None, minute: Minute = None,
    ) -> str:
        """Players ranked by a metric."""
        v = view(match_id, room, minute)
        return v.answer(v.tool("get_top_players")(metric, limit))

    @mcp.tool()
    def get_key_moments(limit: Annotated[int, Field(ge=1, le=40)] = 10, match_id: MatchId = None,
                        room: RoomCode = None, minute: Minute = None) -> str:
        """Key moments so far, with why they mattered and how they moved the win probability."""
        v = view(match_id, room, minute)
        return v.answer(v.tool("get_key_moments")(limit))

    @mcp.tool()
    def get_win_probability_timeline(match_id: MatchId = None, room: RoomCode = None,
                                     minute: Minute = None) -> str:
        """Win probability minute by minute (home, draw, away), ready to chart."""
        v = view(match_id, room, minute)
        points = sorted(v.orchestrator.forecasts, key=lambda f: (f.period, f.clock_s))
        home, away = v.match.teams
        return v.answer(json.dumps({
            "columns": ["minute", home.name, "draw", away.name],
            "rows": [[f.clock, round(f.home, 3), round(f.draw, 3), round(f.away, 3)] for f in points]}))

    @mcp.tool()
    def get_shot_map(team: TeamName, match_id: MatchId = None, room: RoomCode = None,
                     minute: Minute = None) -> str:
        """A team's shots with their position, xG and outcome, ready to plot."""
        v = view(match_id, room, minute)
        summary = v.tool("show_shot_map")(team)
        found = v.chat._team(team)
        if not found:
            return v.answer(summary)
        names = {p.id: p.name for p in found.players}
        shots = [{"clock": s.clock, "player": names.get(s.player_id), "x": s.x, "y": s.y, "xg": s.xg,
                  "result": s.result, "body_part": s.body_part}
                 for s in v.orchestrator.engine.snapshot(recent_signals=0).shots if s.team_id == found.id]
        return v.answer(summary, shots=shots, coordinates=COORDINATES)

    @mcp.tool()
    def get_pass_network(team: TeamName, match_id: MatchId = None, room: RoomCode = None,
                         minute: Minute = None) -> str:
        """A team's passing network: players' average positions and the passes between them."""
        v = view(match_id, room, minute)
        found = v.chat._team(team)
        if not found:
            return v.answer(f"Unknown team {team!r}.")
        net = v.orchestrator.engine.snapshot(recent_signals=0).pass_networks[found.id]
        players = {p.id: p for p in found.players}
        return v.answer(json.dumps({
            "players": [{"name": players[n.player_id].name, "number": players[n.player_id].number,
                         "x": n.x, "y": n.y} for n in net.nodes],
            "links": [{"from": players[e.source].name, "to": players[e.target].name, "passes": e.passes}
                      for e in net.edges]}), coordinates=COORDINATES)

    @mcp.tool()
    async def simulate_what_if(
        minute: Annotated[int | None, Field(description="Minute the scenario starts from, e.g. 60. Required "
                                                        "for a recorded match; omit in a room for 'from now'.")] = None,
        goals_home: Annotated[int, Field(description="Goals added to the home team (negative removes).")] = 0,
        goals_away: Annotated[int, Field(description="Goals added to the away team (negative removes).")] = 0,
        red_cards_home: int = 0,
        red_cards_away: int = 0,
        tactic_home: Annotated[Tactic | None, Field(description="New tactic for the home team.")] = None,
        tactic_away: Annotated[Tactic | None, Field(description="New tactic for the away team.")] = None,
        player_off: Annotated[str | None, Field(description="Player taken off (name).")] = None,
        player_on: Annotated[str | None, Field(description="Substitute brought on (name).")] = None,
        match_id: MatchId = None,
        room: RoomCode = None,
    ) -> str:
        """Replay the rest of the match hundreds of times with a scenario (goal, red card, tactic,
        substitution) and compare the win probabilities with and without it."""
        if minute is None and not room:
            raise ToolError("Give the minute the scenario starts from.")
        if not what_if_limit.allow():
            raise ToolError("Too many simulations right now; try again in a minute.")
        v = view(match_id, room, None if room else minute)
        async with what_if_slots:
            raw = await v.tool("simulate_what_if")(minute, goals_home, goals_away, red_cards_home, red_cards_away,
                                                    tactic_home, tactic_away, player_off, player_on)
        return v.answer(raw)

    return mcp


def mount_mcp(app: FastAPI, runs_dir: Path, get_match: Callable[[str], Match],
              recorded_matches: Callable[[], Awaitable[list[dict]]]) -> MCPServer:
    """Serve the MCP endpoint at /mcp (streamable HTTP, stateless) inside the FastAPI app.

    The session manager must run in the app's lifespan: `async with mcp.session_manager.run()`.
    """
    mcp = create_mcp(app, runs_dir, get_match, recorded_matches)
    # The data is public and read-only; DNS rebinding protection is meant for local servers holding
    # private data, and would reject the public host name.
    http = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                   transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    app.router.routes.extend(http.routes)
    return mcp
