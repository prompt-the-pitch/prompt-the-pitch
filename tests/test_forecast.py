import asyncio

import pytest

from itg.agents.briefs import default_profiles
from itg.agents.llm import LLM
from itg.agents.orchestrator import MatchOrchestrator
from itg.agents.team import AgentTeam
from itg.forecast import Scenario, apply_scenario, compare, forecast, state_at
from itg.synthetic.clubs import CLUBS
from itg.synthetic.simulator import MatchSimulator, SimState, simulate_match


@pytest.fixture(scope="module")
def match():
    return simulate_match(CLUBS["expected-ls"], CLUBS["press-gang"], seed=42)


def test_run_from_keeps_the_score_and_only_adds_goals():
    state = SimState(period=2, clock_s=40 * 60, home_goals=2, away_goals=1)
    for seed in range(20):
        home, away = MatchSimulator(CLUBS["expected-ls"], CLUBS["press-gang"], seed).run_from(state)
        assert home >= 2 and away >= 1


def test_probabilities_add_up_and_late_leads_hold():
    home, away = CLUBS["expected-ls"], CLUBS["press-gang"]
    f = forecast(home, away, SimState(period=2, clock_s=44 * 60, home_goals=2, away_goals=0), runs=40)
    assert f.home + f.draw + f.away == pytest.approx(1, abs=0.01)
    assert f.home > 0.9


def test_scenarios_move_the_forecast_the_right_way(match):
    _, state = state_at(match, 1, 20 * 60)
    base, scored = compare(match, state, Scenario(home_goals=1), runs=60)
    assert scored.home > base.home
    _, red = compare(match, state, Scenario(away_red_cards=1), runs=60)
    assert red.away < base.away


def test_apply_scenario_changes_the_lineup(match):
    _, state = state_at(match, 2, 2700 + 10 * 60)
    home = match.teams[0]
    bench = next(p.id for p in home.players if p.id not in state.home_on_pitch and p.position == "FW")
    off = next(pid for pid in state.home_on_pitch if pid.startswith("exl"))
    changed = apply_scenario(state, Scenario(substitutions=[(off, bench)], home_red_cards=1), match)
    assert bench in changed.home_on_pitch and off not in changed.home_on_pitch
    assert len(changed.home_on_pitch) == len(state.home_on_pitch) - 1


def test_orchestrator_forecasts_and_measures_goal_impact(match):
    async def run():
        o = MatchOrchestrator(match, default_profiles(match), AgentTeam(LLM(None)), forecast_runs=8)
        await o.run()
        return o

    o = asyncio.run(run())
    # Forecasts run in parallel and may finish out of order: read the latest in match time.
    assert o.forecasts and o.latest_forecast().draw == 1.0  # The match ended 2-2.
    goals = [m for m in o.memory if m.signal.type == "goal"]
    assert goals and all(m.signal.data["impact"]["delta"] >= 0 for m in goals if "impact" in m.signal.data)
    assert any("impact" in m.signal.data for m in goals)
