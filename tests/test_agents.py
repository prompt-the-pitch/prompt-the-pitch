import asyncio
import json

import pytest

from itg.agents.briefs import build_brief, default_profiles
from itg.agents.chat import AnalystChat, _AskContext
from itg.agents.contracts import Commentary, Insight, KeyNumber, MomentResult, OverlayCard, RecapResult
from itg.agents.llm import LLM
from itg.agents.orchestrator import MatchOrchestrator
from itg.agents.team import AgentTeam
from itg.agents.verifier import allowed_numbers, check_text, unknown_numbers
from itg.agents.workflow import MomentJob, run_moment
from itg.stats.engine import StatsEngine
from itg.synthetic.clubs import CLUBS
from itg.synthetic.simulator import simulate_match


@pytest.fixture(scope="module")
def match():
    return simulate_match(CLUBS["expected-ls"], CLUBS["press-gang"], seed=42)


@pytest.fixture(scope="module")
def goal_job(match):
    engine = StatsEngine.for_match(match)
    for event in match.events:
        goals = [s for s in engine.ingest(event) if s.type == "goal"]
        if goals:
            profiles = default_profiles(match, ["en", "fr"])
            return MomentJob(goals[0], build_brief(goals[0], engine.snapshot(), []), profiles)
    raise AssertionError("no goal in the test match")


class FakeLLM(LLM):
    """Stands in for Foundry: answers from a script so failures can be simulated."""

    def __init__(self, script):
        super().__init__(client=object())
        self.script = script
        self.calls = []

    async def structured(self, name, instructions, payload, output):
        self.calls.append((name, payload))
        return self.script(name, payload, output, len([c for c in self.calls if c[0] == name]))


def good_answer(output):
    if output is Insight:
        return Insight(why_it_matters="It breaks the deadlock.", tactical_theme="game_state",
                       key_numbers=[KeyNumber(label="xG", value="0.09")], analyst_note="Opener.")
    if output is Commentary:
        return Commentary(line="Goal for Press Gang!", tone="electric")
    return OverlayCard(title="Goal", body="Press Gang lead.", stat_chips=[], priority="high")


def test_verifier_flags_invented_numbers_and_long_text():
    allowed = allowed_numbers({"xg": 0.42, "possession_pct": 61.3, "before": 0.21})
    assert unknown_numbers("xG 0.42, 61 % du ballon, 21% avant", allowed) == []
    assert unknown_numbers("possession de 38,7 %, 63,9 %", allowed) == ["63,9"]
    assert unknown_numbers("l'adversaire a 38,7 % du ballon", allowed) == []  # 100 - 61.3
    assert unknown_numbers("momentum 63.9 towards the away side", allowed_numbers({"momentum": -63.9})) == []
    assert unknown_numbers("ce raté coûte 17,5 points", allowed_numbers({"delta": -0.175})) == []
    issues = check_text({"title": ("one two three four five six seven eight", 6)}, allowed)
    assert issues and "8 words" in issues[0]


def test_brief_uses_names_not_ids(goal_job):
    brief = goal_job.brief
    text = json.dumps(brief.evidence)
    assert all(p.id not in text for p in brief.players)
    assert brief.players and brief.score.count("-") == 1


def test_offline_workflow_produces_one_card_per_profile(goal_job):
    result = asyncio.run(run_moment(AgentTeam(LLM(None)), goal_job))
    assert set(result.overlays) == {p.id for p in goal_job.profiles}
    assert result.commentary.line and result.insight.why_it_matters
    assert {t.status for t in result.trace} == {"fallback"}
    assert result.display_seconds == 12  # Goals are high priority.


def test_workflow_runs_agents_in_parallel_branches(goal_job):
    llm = FakeLLM(lambda name, payload, output, n: good_answer(output))
    result = asyncio.run(run_moment(AgentTeam(llm), goal_job))
    names = [name for name, _ in llm.calls]
    assert names.count("TacticalExplainer") == 1 and names.count("Commentator") == 1
    assert sum(n.startswith("Personalizer") for n in names) == len(goal_job.profiles)
    # Personalizers receive the Explainer's insight: the handoff happened.
    assert all(p["insight"]["why_it_matters"] == "It breaks the deadlock."
               for n, p in llm.calls if n.startswith("Personalizer"))
    assert {t.status for t in result.trace} == {"ok"}


def test_invented_number_is_sent_back_then_fixed(goal_job):
    def script(name, payload, output, n):
        if output is Commentary and n == 1:
            return Commentary(line="A 173 km/h rocket!", tone="electric")  # 173 is not in the brief.
        return good_answer(output)

    llm = FakeLLM(script)
    result = asyncio.run(run_moment(AgentTeam(llm), goal_job))
    retry = [p for n, p in llm.calls if n == "Commentator"][1]
    assert "173" in retry["verifier_feedback"]
    assert result.commentary.line == "Goal for Press Gang!"
    assert next(t for t in result.trace if t.agent == "Commentator").status == "retried"


def test_failing_agent_falls_back_without_breaking_the_moment(goal_job):
    def script(name, payload, output, n):
        if output is Insight:
            raise TimeoutError("Foundry did not answer")
        return good_answer(output)

    result = asyncio.run(run_moment(AgentTeam(FakeLLM(script)), goal_job))
    explainer = next(t for t in result.trace if t.agent == "TacticalExplainer")
    assert explainer.status == "fallback" and "TimeoutError" in explainer.detail
    assert result.insight.why_it_matters  # Template explanation.
    assert all(card.title == "Goal" for card in result.overlays.values())  # Personalizers still ran.


def test_triage_depends_on_the_viewer(match, goal_job):
    orchestrator = MatchOrchestrator(match, default_profiles(match), AgentTeam(LLM(None)), forecast_runs=0)
    goal = goal_job.signal
    assert len(orchestrator.audience(goal)) == len(orchestrator.profiles)
    minor = goal.model_copy(update={"type": "speed_alert", "importance": 0.3, "team_id": None,
                                    "player_ids": [orchestrator.profiles[3].favorite_player_id]})
    assert [p.id for p in orchestrator.audience(minor)] == ["player-focus.en"]
    sub = goal.model_copy(update={"type": "substitution"})
    assert orchestrator.audience(sub) == []


def test_profiles_are_roles_times_languages(match):
    profiles = default_profiles(match, ["en", "fr", "es", "pt"])
    assert len(profiles) == 16
    assert {p.role for p in profiles} == {"analyst", "home-fan", "away-fan", "player-focus"}
    assert next(p for p in profiles if p.id == "home-fan.pt").language == "pt"


def test_full_match_offline(match):
    async def run():
        orchestrator = MatchOrchestrator(match, default_profiles(match), AgentTeam(LLM(None)), forecast_runs=0)
        outputs = []
        orchestrator.listeners.append(outputs.append)
        await orchestrator.run()
        answer = await AnalystChat(orchestrator, LLM(None)).ask("Who is winning?")
        return orchestrator, outputs, answer

    orchestrator, outputs, answer = asyncio.run(run())
    moments = [o for o in outputs if isinstance(o, MomentResult)]
    recaps = [o for o in outputs if isinstance(o, RecapResult)]
    assert len(moments) >= 10
    assert [r.kind for r in recaps] == ["half_time", "full_time"]
    assert all(len(r.recaps) == 4 for r in recaps)
    goals = [m for m in moments if m.signal.type == "goal"]
    assert len(goals) == sum(match.final_score.values())
    assert "Expected Ls" in answer.answer


def test_chat_tools_answer_from_live_state(match):
    orchestrator = MatchOrchestrator(match, default_profiles(match), AgentTeam(LLM(None)), forecast_runs=0)
    for event in match.events[:1500]:
        orchestrator.engine.ingest(event)
    ctx = _AskContext()
    tools = {t.__name__: t for t in AnalystChat(orchestrator, LLM(None)).tools(ctx)}
    assert "Expected Ls" in tools["get_team_stats"]("EXL")
    assert json.loads(tools["get_top_players"]("passes", 3))[0]["passes"] > 0
    assert "Unknown metric" in tools["get_top_players"]("nonsense")
    assert json.loads(tools["get_match_state"]())["clock"]
    assert json.loads(tools["show_shot_map"]("Press Gang"))["shots"] >= 0
    assert ctx.graphic["kind"] == "shot_map"
    assert json.loads(tools["show_player_touches"]("Arrieta"))["touches"] > 0
    assert ctx.graphic["kind"] == "touches" and ctx.graphic["points"]
    assert json.loads(tools["show_pass_network"]("EXL"))["top_links"]
    result = json.loads(asyncio.run(tools["simulate_what_if"](minute=20, goals_home=1)))
    assert set(result["with_scenario"]) == {"Expected Ls", "draw", "Press Gang"}
    assert ctx.graphic["kind"] == "what_if" and len(ctx.graphic["before"]) == 3
    # A scenario never starts after the moment the viewers have reached (no spoilers).
    now = orchestrator.engine.last.display_clock
    assert json.loads(asyncio.run(tools["simulate_what_if"](minute=88)))["from"] == now


def test_rate_limited_calls_wait_and_retry(monkeypatch):
    import itg.agents.llm as llm_module

    monkeypatch.setattr(llm_module, "RATE_LIMIT_BACKOFF_S", (0.01, 0.02))

    class FlakyAgent:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt, options):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("Error code: 429 - rate_limit_exceeded")
            return "ok"

    agent = FlakyAgent()
    assert asyncio.run(LLM(client=object())._run(agent, "prompt", {})) == "ok"
    assert agent.calls == 3

    class BrokenAgent:
        async def run(self, prompt, options):
            raise ValueError("bad request")

    with pytest.raises(ValueError):  # Other errors are not retried here; the guard handles them.
        asyncio.run(LLM(client=object())._run(BrokenAgent(), "prompt", {}))


def test_player_check_ins_every_quarter_hour(match):
    async def run():
        orchestrator = MatchOrchestrator(match, default_profiles(match, ["en", "pt"]), AgentTeam(LLM(None)), forecast_runs=0)
        followed = next(p for p in orchestrator.profiles if p.player_focus).favorite_player_id
        orchestrator.report_players = {followed}
        await orchestrator.run()
        return orchestrator, followed

    orchestrator, followed = asyncio.run(run())
    reports = orchestrator.player_reports
    assert {r.player_id for r in reports} == {followed}
    assert [r.clock for r in reports][:2] == ["15'", "30'"]
    assert 4 <= len(reports) <= 6  # 15', 30', half-time, 60', 75', full-time (unless substituted).
    assert all(set(r.reports) == {"en", "pt"} for r in reports)


def test_personalized_card_carries_its_own_explanation(goal_job):
    def script(name, payload, output, n):
        if output is OverlayCard:
            return OverlayCard(title="But !", body="Press Gang mène.", why="Le premier but oblige l'adversaire à attaquer.",
                               analyst_note="", stat_chips=[], priority="high")
        return good_answer(output)

    result = asyncio.run(run_moment(AgentTeam(FakeLLM(script)), goal_job))
    assert result.overlays["home-fan.fr"].why.startswith("Le premier but")
