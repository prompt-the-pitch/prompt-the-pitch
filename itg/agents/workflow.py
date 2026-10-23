"""Per-moment multi-agent workflow, built with Microsoft Agent Framework.

    Dispatch ──┬──> TacticalExplainer ──> Personalizers (one per viewer, in parallel) ──┐
               └──> Commentator ────────────────────────────────────────────────────────┴──> Assemble

The Commentator does not need the explanation, so it runs alongside the Explainer and
the commentary line is ready as soon as possible.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Never

from agent_framework import Executor, Workflow, WorkflowBuilder, WorkflowContext, handler

from itg.agents.contracts import (
    AgentTrace,
    Commentary,
    FanProfile,
    Insight,
    MomentBrief,
    MomentResult,
    OverlayCard,
)
from itg.agents.team import AgentTeam
from itg.stats.models import Signal

DISPLAY_SECONDS = {"high": 12, "normal": 8, "low": 5}


@dataclass
class MomentJob:
    signal: Signal
    brief: MomentBrief
    profiles: list[FanProfile]


@dataclass
class Explained:
    job: MomentJob
    insight: Insight
    trace: AgentTrace


@dataclass
class BranchResult:
    job: MomentJob
    commentary: Commentary | None = None
    insight: Insight | None = None
    overlays: dict[str, OverlayCard] = field(default_factory=dict)
    trace: list[AgentTrace] = field(default_factory=list)


class Dispatch(Executor):
    @handler
    async def start(self, job: MomentJob, ctx: WorkflowContext[MomentJob]) -> None:
        await ctx.send_message(job)


class Explain(Executor):
    def __init__(self, team: AgentTeam):
        super().__init__(id="explain")
        self.team = team

    @handler
    async def run(self, job: MomentJob, ctx: WorkflowContext[Explained]) -> None:
        insight, trace = await self.team.explain(job.brief)
        await ctx.send_message(Explained(job, insight, trace))


class Narrate(Executor):
    def __init__(self, team: AgentTeam):
        super().__init__(id="narrate")
        self.team = team

    @handler
    async def run(self, job: MomentJob, ctx: WorkflowContext[BranchResult]) -> None:
        commentary, trace = await self.team.narrate(job.brief)
        await ctx.send_message(BranchResult(job, commentary=commentary, trace=[trace]))


class Personalize(Executor):
    def __init__(self, team: AgentTeam):
        super().__init__(id="personalize")
        self.team = team

    @handler
    async def run(self, explained: Explained, ctx: WorkflowContext[BranchResult]) -> None:
        job = explained.job
        results = await asyncio.gather(*(
            self.team.personalize(job.brief, explained.insight, profile, job.signal.team_id)
            for profile in job.profiles
        ))
        await ctx.send_message(BranchResult(
            job, insight=explained.insight,
            overlays={p.id: card for p, (card, _) in zip(job.profiles, results)},
            trace=[explained.trace, *(trace for _, trace in results)],
        ))


class Assemble(Executor):
    @handler
    async def run(self, branches: list[BranchResult], ctx: WorkflowContext[Never, MomentResult]) -> None:
        job = branches[0].job
        commentary = next(b.commentary for b in branches if b.commentary is not None)
        explained = next(b for b in branches if b.insight is not None)
        priorities = [card.priority for card in explained.overlays.values()] or ["normal"]
        top = "high" if "high" in priorities else ("normal" if "normal" in priorities else "low")
        await ctx.yield_output(MomentResult(
            moment_id=job.signal.id, signal=job.signal, insight=explained.insight, commentary=commentary,
            overlays=explained.overlays, display_seconds=DISPLAY_SECONDS[top],
            trace=[t for b in branches for t in b.trace],
        ))


def build_moment_workflow(team: AgentTeam) -> Workflow:
    dispatch, assemble = Dispatch(id="dispatch"), Assemble(id="assemble")
    explain, narrate, personalize = Explain(team), Narrate(team), Personalize(team)
    return (
        WorkflowBuilder(name="moment", start_executor=dispatch)
        .add_fan_out_edges(dispatch, [explain, narrate])
        .add_edge(explain, personalize)
        .add_fan_in_edges([narrate, personalize], assemble)
        .build()
    )


async def run_moment(team: AgentTeam, job: MomentJob) -> MomentResult:
    # A workflow instance handles one run at a time, so each moment gets its own.
    result = await build_moment_workflow(team).run(job)
    outputs = result.get_outputs()
    if not outputs:
        raise RuntimeError(f"moment workflow produced no output for {job.signal.id}")
    return outputs[0]
