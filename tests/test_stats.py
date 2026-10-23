import itertools

import pytest

from itg.schema import EventType as E
from itg.stats import metrics as m
from itg.stats.engine import StatsEngine
from itg.synthetic.clubs import CLUBS
from itg.synthetic.simulator import simulate_match
from scripts.evaluate_detection import evaluate


@pytest.fixture(scope="module")
def run():
    match = simulate_match(CLUBS["expected-ls"], CLUBS["press-gang"], seed=42)
    engine = StatsEngine.for_match(match)
    signals = [s for e in match.events for s in engine.ingest(e)]
    return match, engine, signals


def test_expected_goals_is_sensible():
    assert m.expected_goals(99, 34) > m.expected_goals(94, 34) > m.expected_goals(80, 34)
    assert m.expected_goals(94, 34) > m.expected_goals(94, 5)
    assert m.expected_goals(94, 34, situation="penalty") == pytest.approx(0.76)
    assert m.expected_goals(99, 34, body_part="head") < m.expected_goals(99, 34)


def test_pass_difficulty_orders_passes():
    easy = m.pass_difficulty(8, 30, 32, 34, under_pressure=False, height="ground", cross=False)
    hard = m.pass_difficulty(35, 50, 92, 34, under_pressure=True, height="high", cross=False)
    assert 0 <= easy < 30 < 70 < hard <= 100
    assert m.expected_completion(easy) > m.expected_completion(hard)


def test_team_frame_flips_with_period():
    assert m.to_team_frame(10, 20, team_is_home=True, period=1) == (10, 20)
    assert m.to_team_frame(10, 20, team_is_home=True, period=2) == (95, 48)
    assert m.to_team_frame(10, 20, team_is_home=False, period=1) == (95, 48)


def test_counts_match_the_event_stream(run):
    match, engine, _ = run
    snap = engine.snapshot()
    for team_id, t in snap.teams.items():
        ev = [e for e in match.events if e.team_id == team_id]
        assert t.passes == sum(e.type == E.PASS for e in ev)
        assert t.shots == sum(e.type == E.SHOT for e in ev)
        assert t.goals == sum(e.type == E.GOAL for e in ev)
    assert snap.score == match.final_score
    assert sum(t.possession_pct for t in snap.teams.values()) == pytest.approx(100, abs=0.2)


def test_players_add_up_to_teams(run):
    _, engine, _ = run
    snap = engine.snapshot()
    for team_id, t in snap.teams.items():
        squad = [p for p in snap.players.values() if p.team_id == team_id]
        assert sum(p.passes for p in squad) == t.passes
        assert sum(p.goals for p in squad) == t.goals
        assert sum(p.on_pitch for p in squad) <= 11
        assert all(3 <= p.rating <= 10 for p in squad)
    starters_minutes = [p.minutes for p in snap.players.values() if p.on_pitch and p.position == "GK"]
    assert all(m_ >= 90 for m_ in starters_minutes)


def test_every_goal_produces_a_goal_signal(run):
    match, _, signals = run
    goals = [e for e in match.events if e.type == E.GOAL]
    goal_signals = [s for s in signals if s.type == "goal"]
    assert len(goal_signals) == len(goals)
    assert all(s.importance == 1.0 and s.data["context"] for s in goal_signals)
    assert signals[-1].type == "full_time"


def test_signals_reference_real_players_and_are_ordered(run):
    match, engine, signals = run
    for s in signals:
        assert all(pid in engine.players for pid in s.player_ids)
        assert 0 <= s.importance <= 1
    keys = [(s.period, s.clock_s) for s in signals]
    assert keys == sorted(keys)


def test_momentum_is_bounded(run):
    _, engine, _ = run
    assert engine.momentum_series
    assert all(-100 <= p.value <= 100 for p in engine.momentum_series)


def test_detects_hidden_tactical_shifts():
    result = evaluate(matches=12)
    assert result["recall"] >= 0.25
    assert result["unexplained_per_match"] <= 2.5
