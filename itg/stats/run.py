"""Run the stats engine over a match file and print the signals and final stats.

    python -m itg.stats.run data/matches/exl-prg-42.json
    python -m itg.stats.run data/matches/exl-prg-42.json --min-importance 0.4 --at-minute 60
"""

from __future__ import annotations

import argparse
from pathlib import Path

from itg.stats.engine import StatsEngine
from itg.stats.models import Snapshot
from itg.synthetic.replay import load_match


def format_snapshot(snap: Snapshot) -> str:
    home, away = snap.teams.values()
    rows = [
        ("Possession %", "possession_pct"), ("Shots (on target)", None), ("xG", "xg"),
        ("Passes", "passes"), ("Pass accuracy", "pass_accuracy"), ("Avg pass difficulty", "avg_pass_difficulty"),
        ("Progressive passes", "progressive_passes"), ("Box entries", "box_entries"),
        ("High turnovers", "high_turnovers"), ("PPDA", "ppda"), ("Pressures", "pressures"),
        ("Corners", "corners"), ("Fouls", "fouls"), ("Distance (km)", "distance_km"), ("Sprints", "sprints"),
    ]
    lines = [f"{snap.display_clock}  {home.name} {snap.score['home']}-{snap.score['away']} {away.name}",
             f"{'':<22}{home.short_name:>8}{away.short_name:>8}"]
    for label, key in rows:
        if key is None:
            values = [f"{t.shots} ({t.shots_on_target})" for t in (home, away)]
        else:
            values = [getattr(t, key) for t in (home, away)]
            values = [f"{v:.0%}" if key == "pass_accuracy" else ("-" if v is None else str(v)) for v in values]
        lines.append(f"{label:<22}{values[0]:>8}{values[1]:>8}")
    phase = snap.phase
    if phase:
        owner = f" ({snap.teams[phase.team_id].short_name})" if phase.team_id else ""
        lines.append(f"Phase: {phase.phase}{owner}, chaos index {phase.chaos_index}, momentum {snap.momentum:+.0f}")
    top = sorted(snap.players.values(), key=lambda p: p.rating, reverse=True)[:5]
    lines.append("Top ratings: " + ", ".join(f"{p.short_name} {p.rating}" for p in top))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("match", type=Path)
    parser.add_argument("--min-importance", type=float, default=0.3)
    parser.add_argument("--at-minute", type=float, help="Stop the stream at this match minute.")
    args = parser.parse_args()

    match = load_match(args.match)
    engine = StatsEngine.for_match(match)
    for event in match.events:
        if args.at_minute is not None and event.clock_s >= args.at_minute * 60 and event.period == (1 if args.at_minute <= 45 else 2):
            break
        for signal in engine.ingest(event):
            if signal.importance >= args.min_importance:
                print(f"{signal.display_clock:>7}  [{signal.type}] {signal.headline}")
    print()
    print(format_snapshot(engine.snapshot()))
    print()
    print("Hidden truth: " + "; ".join(f"{s.minute}' {s.description}" for s in match.ground_truth.tactical_shifts))


if __name__ == "__main__":
    main()
