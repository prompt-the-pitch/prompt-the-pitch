"""Score the stats engine against the simulator's hidden ground truth.

For every hidden tactical shift, a detection counts when the engine raises a
`tactical_shift` signal for the same team, of a matching kind, between 2 minutes
before and 15 minutes after the change.

    python -m scripts.evaluate_detection --matches 40
"""

import argparse
import itertools
from collections import Counter

from itg.stats.engine import StatsEngine
from itg.synthetic.clubs import CLUBS
from itg.synthetic.simulator import simulate_match

MATCHING_KINDS = {
    "high_press": {"pressing_up"},
    "low_block": {"pressing_down"},
    "go_direct": {"more_direct"},
    "possession_control": {"less_direct"},
}


def match_minute(signal) -> float:
    return signal.clock_s / 60


def evaluate(matches: int, first_seed: int = 1000) -> dict:
    pairs = list(itertools.permutations(CLUBS, 2))
    found, total = Counter(), Counter()
    detections = unexplained = 0
    for i in range(matches):
        home, away = pairs[i % len(pairs)]
        match = simulate_match(CLUBS[home], CLUBS[away], first_seed + i)
        engine = StatsEngine.for_match(match)
        signals = [s for e in match.events for s in engine.ingest(e) if s.type == "tactical_shift"]
        detections += len(signals)
        explained = set()
        for shift in match.ground_truth.tactical_shifts:
            total[shift.kind] += 1
            for s in signals:
                if (s.team_id == shift.team_id and s.data["kind"] in MATCHING_KINDS[shift.kind]
                        and shift.minute - 2 <= match_minute(s) <= shift.minute + 15):
                    found[shift.kind] += 1
                    explained.add(s.id)
                    break
        unexplained += len([s for s in signals if s.id not in explained])
    return {
        "recall_by_kind": {k: f"{found[k]}/{total[k]}" for k in total},
        "recall": round(sum(found.values()) / max(1, sum(total.values())), 2),
        "detections_per_match": round(detections / matches, 2),
        "unexplained_per_match": round(unexplained / matches, 2),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--matches", type=int, default=40)
    args = parser.parse_args()
    for key, value in evaluate(args.matches).items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
