"""Replay a generated match as a live event stream.

    python -m itg.synthetic.replay data/matches/exl-prg-42.json --speed 60
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from itg.schema import Event, EventType as E, Match

HALF_TIME_PAUSE_S = 3.0


def load_match(path: str | Path) -> Match:
    return Match.model_validate_json(Path(path).read_text(encoding="utf-8"))


async def replay(match: Match, speed: float = 1.0, from_clock_s: float = 0.0) -> AsyncIterator[Event]:
    """Yield events with the same pacing as the match clock, `speed` times faster."""
    previous: Event | None = None
    for event in match.events:
        if (event.period, event.clock_s) < (1 if from_clock_s < 2700 else 2, from_clock_s):
            continue
        if previous is not None:
            if event.period == previous.period:
                delay = (event.clock_s - previous.clock_s) / speed
            else:
                delay = HALF_TIME_PAUSE_S
            if delay > 0:
                await asyncio.sleep(delay)
        previous = event
        yield event


def describe(event: Event, match: Match) -> str:
    """One-line human-readable description, used for logs and the CLI."""
    players = match.players_by_id()
    teams = {t.id: t.short_name for t in match.teams}
    who = players[event.player_id].short_name if event.player_id in players else ""
    team = f"[{teams[event.team_id]}]" if event.team_id else "     "
    d = event.details
    match event.type:
        case E.PASS:
            to = players[d["recipient_id"]].short_name
            kind = "cross" if d.get("cross") else f"{d['height']} pass"
            status = "offside" if d.get("offside") else ("ok" if event.outcome == "success" else "lost")
            text = f"{who} {kind} to {to}, {d['length_m']} m, {status}"
        case E.CARRY:
            text = f"{who} carries {d['distance_m']} m at {d['speed_kmh']} km/h"
        case E.SHOT:
            text = (f"{who} shot ({d['body_part']}, {d['situation']}) from {d['distance_m']} m "
                    f"at {d['speed_kmh']} km/h, {d['result']}")
        case E.GOAL:
            assist = f", assist {players[d['assist_id']].short_name}" if d.get("assist_id") else ""
            text = f"GOAL! {who}{assist}. Score {d['score']['home']}-{d['score']['away']}"
        case E.CARD:
            text = f"{who} {d['card'].replace('_', ' ')} card"
        case E.SUBSTITUTION:
            text = f"{players[d['player_in_id']].short_name} on for {who}"
        case E.ADDED_TIME:
            text = f"+{d['minutes']} minutes added"
        case E.POSSESSION_CHANGE:
            text = f"{who} wins the ball ({d['cause']})"
        case E.PHYSICAL_UPDATE:
            top = max(d["players"], key=lambda p: p["top_speed_kmh"])
            text = f"physical update, top speed {players[top['player_id']].short_name} {top['top_speed_kmh']} km/h"
        case E.PERIOD_END | E.MATCH_END:
            text = f"{event.type.value.replace('_', ' ')}, {d['score']['home']}-{d['score']['away']}"
        case _:
            suffix = f" ({event.outcome})" if event.outcome else ""
            text = f"{who} {event.type.value.replace('_', ' ')}{suffix}".strip()
    return f"{event.display_clock:>7} {team} {text}"


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("match", type=Path)
    parser.add_argument("--speed", type=float, default=30.0, help="Playback speed (1 = real time).")
    parser.add_argument("--from-minute", type=float, default=0.0)
    parser.add_argument("--only", help="Comma-separated event types to show, e.g. shot,goal,card.")
    args = parser.parse_args()

    match = load_match(args.match)
    only = set(args.only.split(",")) if args.only else None
    async for event in replay(match, args.speed, args.from_minute * 60):
        if only is None or event.type.value in only:
            print(describe(event, match), flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
