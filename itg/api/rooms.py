"""Watch rooms: several fans follow the same match together.

One room runs one match session and broadcasts it to all its members. The first member is
the host and controls playback. Members take part in the stands: they back a team and cheer
(the battle of the stands), predict the result (the crowd against the AI forecast), propose
substitutions or tactical changes that the others vote on (each proposal is simulated), and
rate the match at the end. A member arriving late can ask what they missed.

A broadcast overlay (the OBS view) joins as an observer: it receives everything but is not
counted as a fan and never becomes the host.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from itg.agents.contracts import FanProfile
from itg.api.session import MatchSession, Send
from itg.forecast import Scenario, TACTIC_CHANGES, compare, state_from_engine
from itg.schema import Match

log = logging.getLogger(__name__)

ROOM_IDLE_S = 30.0  # A room with nobody left closes after this delay (a page reload keeps it).
MAX_QUESTIONS = 25  # Per member and match: each question costs Foundry calls.
MAX_PROPOSALS = 20
PROPOSAL_RUNS = 150
CHEER_COOLDOWN_S = 1.0
PREDICTIONS_CLOSE_MIN = 80
RATING_OPENS_MIN = 85
HISTORY_TYPES = {"moment", "recap", "player_report", "forecast"}

_ADJECTIVES = ["Loud", "Calm", "Lucky", "Sharp", "Brave", "Sneaky", "Golden", "Tidy", "Rapid", "Wise"]
_NOUNS = ["Ultra", "Pundit", "Winger", "Keeper", "Captain", "Scout", "Libero", "Mascot", "Gaffer", "Poacher"]


def nickname() -> str:
    return f"{random.choice(_ADJECTIVES)} {random.choice(_NOUNS)}"


@dataclass
class Member:
    id: str
    nickname: str
    send: Send
    questions: int = 0
    support: str | None = None
    prediction: Literal["home", "draw", "away"] | None = None
    rating: int | None = None
    last_cheer: float = 0.0
    observer: bool = False


class Proposal(BaseModel):
    id: str
    team_id: str
    kind: Literal["substitution", "tactic"]
    player_off: str | None = None
    player_on: str | None = None
    tactic: str | None = None
    author: str
    clock: str
    votes: dict[str, int] = Field(default_factory=dict)
    status: Literal["simulating", "ready", "failed"] = "simulating"
    win_before: float | None = None
    win_after: float | None = None

    @property
    def score(self) -> int:
        return sum(self.votes.values())


class Room:
    def __init__(self, code: str, match: Match, session_factory: Callable[[Send], MatchSession],
                 on_close: Callable[[], Awaitable[None] | None] | None = None):
        self.code = code
        self.match = match
        self.session = session_factory(self.broadcast)
        self.on_close = on_close
        self.members: dict[str, Member] = {}
        self.host: str | None = None
        self.history: list[dict[str, Any]] = []
        self.last_snapshot: dict[str, Any] | None = None
        self.last_status: dict[str, Any] | None = None
        self.cheers: Counter = Counter()
        self.proposals: list[Proposal] = []
        self.runner: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def source(self) -> str:
        return self.session.source

    # ------------------------------------------------------------ members

    async def join(self, member: Member) -> None:
        if self._closing:
            self._closing.cancel()
            self._closing = None
        self.members[member.id] = member
        if self.host is None and not member.observer:
            self.host = member.id
        await member.send(self._room_message(member))
        if self.runner is None:
            # The stands follow the match message the session sends first.
            self.runner = asyncio.create_task(self.session.run())
            self.runner.add_done_callback(self._runner_done)
            return
        # A late arrival gets the match so far: setup, history, latest stats.
        await member.send(self.session.match_message())
        for message in [self.last_status, *self.history, self.last_snapshot]:
            if message:
                await member.send(message)
        await self.broadcast_social()

    async def leave(self, member_id: str) -> None:
        self.members.pop(member_id, None)
        if self.host == member_id:
            self.host = next((m.id for m in self.members.values() if not m.observer), None)
            for member in self.members.values():
                await self._safe_send(member, self._room_message(member))
        if not self.members:
            self._closing = asyncio.create_task(self._close_later())
        else:
            await self.broadcast_social()

    async def _close_later(self) -> None:
        await asyncio.sleep(ROOM_IDLE_S)
        await self.close()

    async def close(self) -> None:
        if self.runner:
            self.runner.cancel()
        self.session.orchestrator.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self.on_close:
            result = self.on_close()
            if asyncio.iscoroutine(result):
                await result

    def _runner_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            log.error("room %s replay failed", self.code, exc_info=task.exception())

    def _room_message(self, member: Member) -> dict[str, Any]:
        return {"type": "room", "code": self.code, "you": member.id, "nickname": member.nickname,
                "host": self.host == member.id}

    # ---------------------------------------------------------- broadcast

    async def broadcast(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind in HISTORY_TYPES:
            self.history.append(message)
        elif kind == "snapshot":
            self.last_snapshot = message
        elif kind == "status":
            self.last_status = message
        for member in list(self.members.values()):
            await self._safe_send(member, message)
        if kind in ("match", "forecast"):
            await self.broadcast_social()  # The stands open with the match; the AI side of "crowd vs AI" moves.

    async def _safe_send(self, member: Member, message: dict[str, Any]) -> None:
        try:
            await member.send(message)
        except Exception:
            pass  # The socket is closing; leave() will clean up.

    async def broadcast_social(self) -> None:
        state = self.social_state()
        for member in list(self.members.values()):
            await self._safe_send(member, {"type": "social", "social": state})

    def _start(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------- state

    def match_minute(self) -> float:
        last = self.session.orchestrator.engine.last
        if last is None:
            return 0.0
        return last.clock_s / 60 if last.period == 1 else 45 + (last.clock_s - 2700) / 60

    def match_over(self) -> bool:
        return self.last_status is not None and self.last_status.get("state") == "ended"

    def social_state(self) -> dict[str, Any]:
        home, away = self.match.teams
        members = [m for m in self.members.values() if not m.observer]
        supporters = Counter(m.support for m in members if m.support)
        predictions = Counter(m.prediction for m in members if m.prediction)
        ratings = [m.rating for m in members if m.rating]
        forecasts = sorted(self.session.orchestrator.forecasts, key=lambda f: (f.period, f.clock_s))
        ai_now = forecasts[-1] if forecasts else None
        ai_kickoff = forecasts[0] if forecasts else None
        proposals = sorted(self.proposals, key=lambda p: (-p.score, p.id))
        return {
            "members": len(members),
            "host": self.host,
            "stands": {t.id: {"fans": supporters[t.id], "cheers": self.cheers[t.id]} for t in (home, away)},
            "predictions": {k: predictions[k] for k in ("home", "draw", "away")},
            "predictions_open": self.match_minute() < PREDICTIONS_CLOSE_MIN and not self.match_over(),
            "ai": ai_now.model_dump(mode="json") if ai_now else None,
            "ai_kickoff": ai_kickoff.model_dump(mode="json") if ai_kickoff else None,
            "proposals": [{**p.model_dump(exclude={"votes"}), "score": p.score, "voters": len(p.votes)}
                          for p in proposals],
            "rating_open": self.match_minute() >= RATING_OPENS_MIN or self.match_over(),
            "ratings": {"count": len(ratings), "average": round(sum(ratings) / len(ratings), 2) if ratings else None},
        }

    # ---------------------------------------------------------- messages

    async def handle(self, member: Member, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind in ("pause", "resume", "speed"):
            if member.id == self.host:
                await self.session.handle(message)
        elif kind in ("languages", "follow"):
            await self.session.handle(message)
        elif kind == "ask":
            await self._ask(member, str(message.get("question", ""))[:500])
        elif kind == "catch_up":
            self._start(self._catch_up(member, str(message.get("profile_id", "")), message.get("since")))
        elif kind == "support":
            if message.get("team_id") in {t.id for t in self.match.teams}:
                member.support = message["team_id"]
                await self.broadcast_social()
        elif kind == "cheer":
            now = time.monotonic()
            if member.support and now - member.last_cheer >= CHEER_COOLDOWN_S:
                member.last_cheer = now
                self.cheers[member.support] += 1
                await self.broadcast_social()
        elif kind == "predict":
            if message.get("outcome") in ("home", "draw", "away") and self.social_state()["predictions_open"]:
                member.prediction = message["outcome"]
                await self.broadcast_social()
        elif kind == "rate":
            stars = message.get("stars")
            if isinstance(stars, int) and 1 <= stars <= 5 and self.social_state()["rating_open"]:
                member.rating = stars
                await self.broadcast_social()
        elif kind == "propose":
            await self._propose(member, message)
        elif kind == "vote":
            proposal = next((p for p in self.proposals if p.id == message.get("proposal_id")), None)
            value = message.get("value")
            if proposal and value in (-1, 0, 1):
                if value == 0:
                    proposal.votes.pop(member.id, None)
                else:
                    proposal.votes[member.id] = value
                await self.broadcast_social()

    async def _ask(self, member: Member, question: str) -> None:
        member.questions += 1
        if member.questions > MAX_QUESTIONS:
            await member.send({"type": "answer", "question": question, "graphic": None,
                               "answer": "The analyst has answered enough questions for this match."})
            return

        async def answer() -> None:
            graphic = None
            try:
                result = await self.session.chat.ask(question)
                text, graphic = result.answer, result.graphic
            except Exception as exc:
                log.exception("analyst chat failed")
                text = f"Sorry, the analyst is unavailable right now ({type(exc).__name__})."
            await self._safe_send(member, {"type": "answer", "question": question, "answer": text, "graphic": graphic})

        self._start(answer())

    async def _catch_up(self, member: Member, profile_id: str, since: Any) -> None:
        orchestrator = self.session.orchestrator
        profile = next((p for p in orchestrator.profiles if p.id == profile_id), None)
        if profile is None:
            profile = FanProfile(id=profile_id or "viewer.en", role="viewer", label="Viewer", mode="casual",
                                 language=profile_id.rsplit(".", 1)[-1] if "." in profile_id else "en")
        since_s = float(since) if isinstance(since, (int, float)) else 0.0
        moments = sorted((m for m in orchestrator.memory if m.signal.period * 10000 + m.signal.clock_s > since_s
                          and m.signal.importance >= 0.45), key=lambda m: (m.signal.period, m.signal.clock_s))
        snap = orchestrator.engine.snapshot(recent_signals=0)
        home, away = snap.teams.values()
        payload = {"score_now": f"{home.name} {snap.score['home']}-{snap.score['away']} {away.name}",
                   "clock_now": snap.display_clock, "win_probability": orchestrator.win_probability(),
                   "moments_missed": [{"clock": m.signal.display_clock, "what": m.signal.headline,
                                       "why": m.insight.why_it_matters} for m in moments][-8:]}
        result, _ = await orchestrator.team.catch_up(payload, profile)
        await self._safe_send(member, {"type": "catch_up", "catch_up": result.model_dump(), "clock": snap.display_clock})

    async def _propose(self, member: Member, message: dict[str, Any]) -> None:
        team = next((t for t in self.match.teams if t.id == message.get("team_id")), None)
        kind = message.get("kind")
        if team is None or kind not in ("substitution", "tactic") or len(self.proposals) >= MAX_PROPOSALS:
            return
        engine = self.session.orchestrator.engine
        squad = {p.id for p in team.players}
        on_pitch = {pid for pid, p in engine.players.items() if p.team_id == team.id and p.on_pitch}
        off, on, tactic = message.get("player_off"), message.get("player_on"), message.get("tactic")
        if kind == "substitution" and not (off in on_pitch and on in squad and on not in on_pitch):
            return
        if kind == "tactic" and tactic not in TACTIC_CHANGES:
            return
        proposal = Proposal(id=secrets.token_hex(4), team_id=team.id, kind=kind,
                            player_off=off if kind == "substitution" else None,
                            player_on=on if kind == "substitution" else None,
                            tactic=tactic if kind == "tactic" else None, author=member.nickname,
                            clock=engine.last.display_clock if engine.last else "0'", votes={member.id: 1})
        self.proposals.append(proposal)
        await self.broadcast_social()
        self._start(self._simulate(proposal))

    async def _simulate(self, proposal: Proposal) -> None:
        """Replay the rest of the match with the proposal applied, from now."""
        side = "home" if proposal.team_id == self.match.info.home_team_id else "away"
        scenario = (Scenario(substitutions=[(proposal.player_off, proposal.player_on)])
                    if proposal.kind == "substitution" else Scenario(**{f"{side}_tactic": proposal.tactic}))
        try:
            state = state_from_engine(self.session.orchestrator.engine, self.match)
            base, alt = await asyncio.to_thread(compare, self.match, state, scenario, PROPOSAL_RUNS, 99)
            proposal.win_before, proposal.win_after = getattr(base, side), getattr(alt, side)
            proposal.status = "ready"
        except Exception:
            log.exception("proposal simulation failed")
            proposal.status = "failed"
        await self.broadcast_social()
