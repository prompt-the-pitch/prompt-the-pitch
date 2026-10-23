"""Run the full agent pipeline over a match and save the broadcast output.

    python -m itg.agents.run data/matches/exl-prg-42.json            # Foundry
    python -m itg.agents.run data/matches/exl-prg-42.json --offline  # templates only
    python -m itg.agents.run data/matches/exl-prg-42.json --until-minute 30 --ask "Who is pressing better?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import Counter
from pathlib import Path

from itg.agents.briefs import LANGUAGES, default_profiles
from itg.agents.chat import AnalystChat
from itg.agents.contracts import MomentResult, PlayerReportResult, RecapResult
from itg.forecast import ForecastPoint
from itg.agents.llm import open_llm
from itg.agents.orchestrator import MatchOrchestrator
from itg.agents.team import AgentTeam
from itg.synthetic.replay import load_match


def print_output(output: MomentResult | RecapResult | PlayerReportResult | ForecastPoint, verbose: bool) -> None:
    if isinstance(output, ForecastPoint):
        return
    if isinstance(output, PlayerReportResult):
        if verbose and "en" in output.reports:
            report = output.reports["en"]
            print(f"  {output.clock:>6} player check-in {output.player_id}: {report.headline}")
        return
    if isinstance(output, MomentResult):
        s = output.signal
        statuses = Counter(t.status for t in output.trace)
        print(f"\n{s.display_clock:>6} [{s.type}] {output.commentary.line}")
        print(f"        why: {output.insight.why_it_matters}")
        for profile_id, card in output.overlays.items():
            if not verbose and not profile_id.endswith(".en"):
                continue
            chips = ", ".join(f"{c.label} {c.value}" for c in card.stat_chips)
            print(f"        {profile_id:<16} {card.title} | {card.body}" + (f" [{chips}]" if verbose and chips else ""))
        print(f"        agents: {dict(statuses)}")
    else:
        print(f"\n===== {output.kind.replace('_', ' ').upper()} RECAPS ({output.clock}) =====")
        for profile_id, recap in output.recaps.items():
            if not verbose and not profile_id.endswith(".en"):
                continue
            print(f"  [{profile_id}] {recap.headline}\n    {recap.summary}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("match", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--until-minute", type=float, help="Stop the match early (useful for quick tests).")
    parser.add_argument("--ask", help="Question for the analyst chat at the end.")
    parser.add_argument("--out", type=Path, default=Path("data/runs"))
    parser.add_argument("--languages", default=",".join(LANGUAGES), help="Comma-separated, e.g. en,fr.")
    parser.add_argument("--no-player-reports", action="store_true", help="Skip the 15-minute player check-ins.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    match = load_match(args.match)
    if args.until_minute is not None:
        cut = args.until_minute * 60
        period = 1 if args.until_minute <= 45 else 2
        match.events = [e for e in match.events if (e.period, e.clock_s) < (period, cut)]
    profiles = default_profiles(match, args.languages.split(","))
    started = time.perf_counter()
    async with open_llm(offline=True if args.offline else None) as llm:
        print(f"Mode: {'Foundry (' + str(llm.client.model) + ')' if llm.online else 'offline'}; "
              f"viewers: {', '.join(dict.fromkeys(p.label for p in profiles))}; languages: {args.languages}")
        orchestrator = MatchOrchestrator(match, profiles, AgentTeam(llm), every_minute=True)
        if not args.no_player_reports:
            # A recorded run covers every player, so viewers can follow anyone without calling Foundry.
            orchestrator.report_players = {p.id for team in match.teams for p in team.players}
        orchestrator.listeners.append(lambda o: print_output(o, args.verbose))
        await orchestrator.run()
        if args.ask:
            reply = await AnalystChat(orchestrator, llm).ask(args.ask)
            print(f"\nQ: {args.ask}\nA: {reply.answer}" + (f"\n   [graphic: {reply.graphic['kind']}]" if reply.graphic else ""))

    traces = [t for r in [*orchestrator.memory, *orchestrator.recaps, *orchestrator.player_reports] for t in r.trace]
    statuses = Counter(t.status for t in traces)
    latencies = sorted(t.latency_ms for t in traces if t.status != "fallback" or llm.online)
    print(f"\n{len(orchestrator.memory)} moments, {len(orchestrator.recaps)} recaps, "
          f"{len(orchestrator.player_reports)} player check-ins, {len(traces)} agent calls "
          f"{dict(statuses)} in {time.perf_counter() - started:.0f} s"
          + (f"; median agent latency {latencies[len(latencies) // 2]} ms" if latencies else ""))

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{match.info.match_id}{'-offline' if not llm.online else ''}.json"
    moments = sorted(orchestrator.memory, key=lambda r: (r.signal.period, r.signal.clock_s))
    path.write_text(json.dumps({
        "match_id": match.info.match_id,
        "profiles": [p.model_dump() for p in profiles],
        "moments": [m.model_dump(mode="json") for m in moments],
        "recaps": [r.model_dump(mode="json") for r in orchestrator.recaps],
        "forecasts": [f.model_dump(mode="json") for f in sorted(orchestrator.forecasts, key=lambda f: (f.period, f.clock_s))],
        "player_reports": [r.model_dump(mode="json") for r in sorted(
            orchestrator.player_reports, key=lambda r: (r.period, r.clock_s, r.player_id))],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Saved to {path}")


if __name__ == "__main__":
    asyncio.run(main())
