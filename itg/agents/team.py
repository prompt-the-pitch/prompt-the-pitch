"""The specialised agents and the guarded way they are called.

Each call goes through the same loop: run the agent on Foundry, let the Verifier check the
output, send it back once with the Verifier's feedback if needed, and fall back to a
deterministic template if the agent still fails, times out or is offline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from itg.agents import fallbacks, prompts
from itg.agents.contracts import (
    AgentTrace,
    CatchUp,
    Commentary,
    FanProfile,
    Insight,
    MomentBrief,
    OverlayCard,
    PlayerReport,
    Recap,
)
from itg.agents.llm import LLM
from itg.agents.verifier import allowed_numbers, check_text

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class AgentTeam:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def _guarded(self, name: str, instructions: str, payload: dict[str, Any], output: type[T],
                       check: Callable[[T], list[str]], fallback: Callable[[], T]) -> tuple[T, AgentTrace]:
        start = time.perf_counter()

        def trace(status: str, detail: str | None = None) -> AgentTrace:
            return AgentTrace(agent=name, status=status, latency_ms=int(1000 * (time.perf_counter() - start)),
                              detail=detail)

        if not self.llm.online:
            return fallback(), trace("fallback", "offline")
        issues: list[str] = []
        for attempt in range(2):
            try:
                request = payload if not issues else {
                    **payload, "verifier_feedback": "Your previous answer was rejected: " + " ".join(issues)}
                result = await self.llm.structured(name, instructions, request, output)
            except Exception as exc:  # Timeouts, network errors, content filters, invalid JSON.
                log.warning("%s failed (attempt %d): %r", name, attempt + 1, exc)
                issues = [f"error: {type(exc).__name__}"]
                if attempt == 0:
                    await asyncio.sleep(2)  # Let a rate limit or a transient error clear.
                continue
            issues = check(result)
            if not issues:
                return result, trace("ok" if attempt == 0 else "retried")
        return fallback(), trace("fallback", " ".join(issues))

    async def explain(self, brief: MomentBrief) -> tuple[Insight, AgentTrace]:
        allowed = allowed_numbers(brief)

        def check(r: Insight) -> list[str]:
            return check_text({"why_it_matters": (r.why_it_matters, 50), "analyst_note": (r.analyst_note, 60),
                               "key_numbers": (" ".join(k.value for k in r.key_numbers), 20)}, allowed)

        return await self._guarded("TacticalExplainer", prompts.EXPLAINER, {"brief": brief.model_dump()},
                                   Insight, check, lambda: fallbacks.insight(brief))

    async def narrate(self, brief: MomentBrief) -> tuple[Commentary, AgentTrace]:
        allowed = allowed_numbers(brief)

        def check(r: Commentary) -> list[str]:
            return check_text({"line": (r.line, 34)}, allowed)

        return await self._guarded("Commentator", prompts.NARRATOR, {"brief": brief.model_dump()},
                                   Commentary, check, lambda: fallbacks.commentary(brief))

    async def personalize(self, brief: MomentBrief, insight: Insight, profile: FanProfile,
                          team_id: str | None) -> tuple[OverlayCard, AgentTrace]:
        allowed = allowed_numbers(brief, insight)
        valid_ids = {p.id for p in brief.players} | {profile.favorite_player_id}

        def check(r: OverlayCard) -> list[str]:
            issues = check_text({"title": (r.title, 7), "body": (r.body, 34), "why": (r.why, 42),
                                 "analyst_note": (r.analyst_note, 48),
                                 "stat_chips": (" ".join(k.value for k in r.stat_chips), 20)}, allowed)
            if r.highlight_player_id and r.highlight_player_id not in valid_ids:
                issues.append("highlight_player_id must be one of the player ids in the brief.")
            return issues

        card, trace = await self._guarded(
            f"Personalizer[{profile.id}]", prompts.PERSONALIZER,
            {"brief": brief.model_dump(), "insight": insight.model_dump(), "profile": profile.model_dump()},
            OverlayCard, check, lambda: fallbacks.overlay(brief, insight, profile, team_id))
        if card.highlight_team_id is not None and team_id is not None:
            card.highlight_team_id = team_id  # Agents see team names; the screen needs the id.
        return card, trace

    async def catch_up(self, payload: dict[str, Any], profile: FanProfile) -> tuple[CatchUp, AgentTrace]:
        allowed = allowed_numbers(payload)

        def check(r: CatchUp) -> list[str]:
            return check_text({"headline": (r.headline, 12), "summary": (r.summary, 70),
                               "key_points": (" ".join(r.key_points), 70)}, allowed)

        return await self._guarded(f"CatchUp[{profile.id}]", prompts.CATCH_UP,
                                   {**payload, "profile": profile.model_dump()}, CatchUp, check,
                                   lambda: fallbacks.catch_up(payload))

    async def player_report(self, payload: dict[str, Any], language: str) -> tuple[PlayerReport, AgentTrace]:
        allowed = allowed_numbers(payload)

        def check(r: PlayerReport) -> list[str]:
            return check_text({"headline": (r.headline, 10), "summary": (r.summary, 52),
                               "key_stats": (" ".join(k.value for k in r.key_stats), 20)}, allowed)

        name = payload["player"]["short_name"]
        return await self._guarded(f"PlayerAnalyst[{name}.{language}]", prompts.PLAYER_REPORT,
                                   {**payload, "language": language}, PlayerReport, check,
                                   lambda: fallbacks.player_report(payload))

    async def recap(self, payload: dict[str, Any], profile: FanProfile) -> tuple[Recap, AgentTrace]:
        allowed = allowed_numbers(payload)

        def check(r: Recap) -> list[str]:
            return check_text({"headline": (r.headline, 14), "summary": (r.summary, 130),
                               "tactical_story": (r.tactical_story, 80),
                               "key_moments": (" ".join(r.key_moments), 180)}, allowed)

        return await self._guarded(f"RecapWriter[{profile.id}]", prompts.RECAP,
                                   {**payload, "profile": profile.model_dump()}, Recap, check,
                                   lambda: fallbacks.recap(payload, profile))
