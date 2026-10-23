import asyncio
import itertools
import statistics

import pytest

from itg.schema import PITCH_LENGTH, PITCH_WIDTH, EventType as E, Match
from itg.synthetic.clubs import CLUBS, starting_lineup
from itg.synthetic.replay import describe, replay
from itg.synthetic.simulator import simulate_match


@pytest.fixture(scope="module")
def match() -> Match:
    return simulate_match(CLUBS["expected-ls"], CLUBS["press-gang"], seed=42)


def test_same_seed_gives_same_match(match):
    again = simulate_match(CLUBS["expected-ls"], CLUBS["press-gang"], seed=42)
    assert again.model_dump_json() == match.model_dump_json()


def test_different_seeds_give_different_matches(match):
    other = simulate_match(CLUBS["expected-ls"], CLUBS["press-gang"], seed=43)
    assert other.events != match.events


def test_squads_are_complete_and_unique():
    names = [p.name.split(" ", 1)[1] for team in CLUBS.values() for p in team.players]
    assert len(names) == len(set(names))
    for team in CLUBS.values():
        starters, bench = starting_lineup(team)
        assert len(starters) == 11 and len(bench) == 7
        assert sum(p.position == "GK" for _, p in starters) == 1


def test_events_are_ordered(match):
    assert [e.seq for e in match.events] == list(range(1, len(match.events) + 1))
    for prev, cur in itertools.pairwise(match.events):
        assert (cur.period, cur.clock_s) >= (prev.period, prev.clock_s)
    assert match.events[0].type == E.PERIOD_START
    assert match.events[-1].type == E.MATCH_END


def test_coordinates_stay_on_the_pitch(match):
    for e in match.events:
        for x, y in ((e.x, e.y), (e.end_x, e.end_y)):
            if x is not None:
                assert 0 <= x <= PITCH_LENGTH and 0 <= y <= PITCH_WIDTH, e


def test_score_matches_goal_events(match):
    goals = [e for e in match.events if e.type == E.GOAL]
    home_id = match.info.home_team_id
    assert sum(e.team_id == home_id for e in goals) == match.final_score["home"]
    assert sum(e.team_id != home_id for e in goals) == match.final_score["away"]
    assert match.events[-1].details["score"] == match.final_score


def test_every_event_references_known_players(match):
    players = match.players_by_id()
    for e in match.events:
        if e.player_id:
            assert e.player_id in players
        for key in ("recipient_id", "assist_id", "player_in_id", "fouled_player_id"):
            if e.details.get(key):
                assert e.details[key] in players


def test_statistics_look_like_real_football():
    pairs = list(itertools.permutations(CLUBS, 2))
    per_team = {"passes": [], "shots": [], "goals": []}
    for seed in range(12):
        home, away = pairs[seed % len(pairs)]
        m = simulate_match(CLUBS[home], CLUBS[away], seed)
        for team in m.teams:
            ev = [e for e in m.events if e.team_id == team.id]
            per_team["passes"].append(sum(e.type == E.PASS for e in ev))
            per_team["shots"].append(sum(e.type == E.SHOT for e in ev))
            per_team["goals"].append(sum(e.type == E.GOAL for e in ev))
    assert 380 <= statistics.mean(per_team["passes"]) <= 650
    assert 8 <= statistics.mean(per_team["shots"]) <= 20
    assert 0.8 <= statistics.mean(per_team["goals"]) <= 2.5


def test_replay_streams_all_events_in_order(match):
    async def collect():
        return [e async for e in replay(match, speed=1e9)]

    streamed = asyncio.run(collect())
    assert streamed == match.events


def test_every_event_can_be_described(match):
    for e in match.events:
        assert describe(e, match)
