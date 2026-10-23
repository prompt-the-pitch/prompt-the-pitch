"""Average per-team match statistics over many seeds, to check the simulator's realism.

    python -m scripts.calibrate --matches 30
"""

import argparse
import itertools
import statistics
from collections import defaultdict

from itg.schema import EventType as E
from itg.synthetic.clubs import CLUBS
from itg.synthetic.simulator import simulate_match

# Rough per-team averages in top-flight football.
REFERENCE = {
    "passes": 480, "pass_completion": 0.82, "shots": 13, "on_target": 4.5, "goals": 1.4,
    "corners": 5, "fouls": 11, "tackles": 17, "interceptions": 10, "pressures": 150,
    "possession_changes": 95, "events (match)": 2500,
}


def team_stats(match, team_id):
    ev = [e for e in match.events if e.team_id == team_id]
    passes = [e for e in ev if e.type == E.PASS]
    shots = [e for e in ev if e.type == E.SHOT]
    return {
        "passes": len(passes),
        "pass_completion": sum(e.outcome == "success" for e in passes) / max(1, len(passes)),
        "shots": len(shots),
        "on_target": sum(e.details["result"] in ("goal", "saved") for e in shots),
        "goals": sum(e.type == E.GOAL for e in ev),
        "corners": sum(e.type == E.CORNER for e in ev),
        "fouls": sum(e.type == E.FOUL for e in ev),
        "tackles": sum(e.type == E.TACKLE for e in ev),
        "interceptions": sum(e.type == E.INTERCEPTION for e in ev),
        "pressures": sum(e.type == E.PRESSURE for e in ev),
        "possession_changes": sum(e.type == E.POSSESSION_CHANGE for e in ev),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--matches", type=int, default=24)
    args = parser.parse_args()
    pairs = list(itertools.permutations(CLUBS, 2))
    values = defaultdict(list)
    for seed in range(args.matches):
        home, away = pairs[seed % len(pairs)]
        match = simulate_match(CLUBS[home], CLUBS[away], seed)
        values["events (match)"].append(len(match.events))
        for team in match.teams:
            for key, value in team_stats(match, team.id).items():
                values[key].append(value)
    print(f"{'stat (per team)':<20}{'simulated':>10}{'reference':>11}")
    for key, ref in REFERENCE.items():
        print(f"{key:<20}{statistics.mean(values[key]):>10.2f}{ref:>11}")


if __name__ == "__main__":
    main()
