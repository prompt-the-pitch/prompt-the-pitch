"""Machine-readable overlay cues for a graphics rendering engine.

A cue says what to draw (template, texts, numbers, what to highlight), for which viewer
feed, and when: in match time, so it can be synchronised with the video feed.
"""

from __future__ import annotations

from typing import Any

from itg.agents.contracts import FanProfile, MomentResult

CUE_FORMAT_VERSION = "1.0"


def cue(moment: MomentResult, profile: FanProfile, match_id: str) -> dict[str, Any] | None:
    card = moment.overlays.get(profile.id)
    if card is None:
        return None
    signal = moment.signal
    return {
        "cue_id": f"{moment.moment_id}:{profile.id}",
        "match_id": match_id,
        "feed": profile.id,
        "language": profile.language,
        "period": signal.period,
        "match_clock_s": signal.clock_s,
        "display_clock": signal.display_clock,
        "duration_s": moment.display_seconds,
        "template": f"lower_third_{card.priority}",
        "priority": card.priority,
        "title": card.title,
        "body": card.body,
        "stat_chips": [chip.model_dump() for chip in card.stat_chips],
        "highlight": {"player_id": card.highlight_player_id, "team_id": card.highlight_team_id},
        "moment_type": signal.type,
        "commentary": moment.commentary.line,
    }


def cue_sheet(match_id: str, moments: list[MomentResult], profiles: list[FanProfile],
              feed: str | None = None) -> dict[str, Any]:
    selected = [p for p in profiles if feed is None or p.id == feed]
    cues = [c for m in moments for p in selected if (c := cue(m, p, match_id))]
    cues.sort(key=lambda c: (c["period"], c["match_clock_s"], c["feed"]))
    return {"format": "itg-overlay-cues", "version": CUE_FORMAT_VERSION, "match_id": match_id,
            "feeds": [p.model_dump() for p in selected], "cues": cues}
