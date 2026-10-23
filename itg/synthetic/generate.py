"""Generate a synthetic match and save it as JSON.

    python -m itg.synthetic.generate --home expected-ls --away press-gang --seed 42
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from itg.schema import EventType as E, Match
from itg.synthetic.clubs import CLUBS
from itg.synthetic.simulator import simulate_match


def summary(match: Match) -> str:
    home, away = match.teams
    lines = [f"{home.name} {match.final_score['home']}-{match.final_score['away']} {away.name}"
             f"  ({len(match.events)} events)"]
    for team in (home, away):
        events = [e for e in match.events if e.team_id == team.id]
        passes = [e for e in events if e.type == E.PASS]
        completed = sum(e.outcome == "success" for e in passes)
        shots = Counter(e.details["result"] for e in events if e.type == E.SHOT)
        lines.append(
            f"  {team.short_name}: {len(passes)} passes ({completed / max(1, len(passes)):.0%} completed), "
            f"{sum(shots.values())} shots ({shots['goal'] + shots['saved']} on target), "
            f"{sum(e.type == E.PRESSURE for e in events)} pressures, "
            f"{sum(e.type == E.TACKLE for e in events)} tackles, {sum(e.type == E.FOUL for e in events)} fouls, "
            f"{sum(e.type == E.CORNER for e in events)} corners"
        )
    for shift in match.ground_truth.tactical_shifts:
        lines.append(f"  hidden: {shift.minute}' {shift.description}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", default="expected-ls", choices=sorted(CLUBS))
    parser.add_argument("--away", default="press-gang", choices=sorted(CLUBS))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("data/matches"))
    args = parser.parse_args()
    if args.home == args.away:
        parser.error("home and away must be different clubs")

    match = simulate_match(CLUBS[args.home], CLUBS[args.away], args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{match.info.match_id}.json"
    path.write_text(match.model_dump_json(indent=1), encoding="utf-8")
    print(summary(match))
    print(f"Saved to {path}")


if __name__ == "__main__":
    main()
