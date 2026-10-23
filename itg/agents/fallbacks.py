"""Deterministic outputs used when an agent fails, times out or runs offline.

They are plainer than the agents' work but always factual, so the broadcast never
shows an empty or wrong overlay.
"""

from __future__ import annotations

from typing import Any

from itg.agents.contracts import (
    CatchUp,
    Commentary,
    FanProfile,
    Insight,
    KeyNumber,
    MomentBrief,
    OverlayCard,
    PlayerReport,
    Recap,
    Theme,
)

THEMES: dict[str, Theme] = {
    "goal": "game_state", "penalty": "game_state", "card": "game_state", "substitution": "game_state",
    "big_chance": "individual_quality", "big_save": "individual_quality", "thunderbolt": "individual_quality",
    "defence_splitting_pass": "individual_quality", "speed_alert": "physical", "distance_milestone": "physical",
    "high_turnover": "pressure", "tactical_shift": "pressure", "counter_attack": "chaos",
    "momentum_swing": "rhythm", "dominance": "control", "pass_sequence": "control", "phase_change": "control",
}

WHY: dict[str, str] = {
    "goal": "Goals change the game state: the trailing team must now take more risks.",
    "big_chance": "Chances this clear rarely come twice; missing it keeps the game open.",
    "big_save": "The goalkeeper kept the score intact against a high-quality chance.",
    "tactical_shift": "A lasting change in behaviour usually reflects an instruction from the bench.",
    "high_turnover": "Winning the ball high up gives the defence no time to reorganise.",
    "counter_attack": "Fast transitions punish teams that commit players forward.",
    "momentum_swing": "The balance of threat has tipped; the other side must respond.",
    "dominance": "Sustained pressure tends to produce chances and set pieces.",
    "phase_change": "The rhythm of the game has changed, and with it which team is comfortable.",
}

TITLES: dict[str, dict[str, str]] = {
    "en": {"goal": "Goal!", "big_chance": "Big chance", "big_save": "Big save", "penalty": "Penalty",
           "card": "Card", "tactical_shift": "Tactical change", "high_turnover": "Press pays off",
           "counter_attack": "Counter-attack", "momentum_swing": "Momentum shift", "dominance": "Dominance",
           "phase_change": "Game rhythm", "speed_alert": "Top speed", "thunderbolt": "Thunderbolt"},
    "fr": {"goal": "But !", "big_chance": "Grosse occasion", "big_save": "Parade décisive", "penalty": "Penalty",
           "card": "Carton", "tactical_shift": "Changement tactique", "high_turnover": "Pressing payant",
           "counter_attack": "Contre-attaque", "momentum_swing": "Bascule du match", "dominance": "Domination",
           "phase_change": "Rythme du match", "speed_alert": "Pointe de vitesse", "thunderbolt": "Frappe surpuissante"},
    "es": {"goal": "¡Gol!", "big_chance": "Ocasión clara", "big_save": "Gran parada", "penalty": "Penalti",
           "card": "Tarjeta", "tactical_shift": "Cambio táctico", "high_turnover": "Presión eficaz",
           "counter_attack": "Contraataque", "momentum_swing": "Cambio de dominio", "dominance": "Dominio",
           "phase_change": "Ritmo del partido", "speed_alert": "Velocidad punta", "thunderbolt": "Cañonazo"},
    "pt": {"goal": "Golo!", "big_chance": "Grande oportunidade", "big_save": "Grande defesa", "penalty": "Penálti",
           "card": "Cartão", "tactical_shift": "Mudança tática", "high_turnover": "Pressão eficaz",
           "counter_attack": "Contra-ataque", "momentum_swing": "Viragem no jogo", "dominance": "Domínio",
           "phase_change": "Ritmo do jogo", "speed_alert": "Velocidade máxima", "thunderbolt": "Remate potente"},
}

_NUMBER_KEYS = [("xg", "xG"), ("speed_kmh", "km/h"), ("distance_m", "m"), ("top_speed_kmh", "km/h"),
                ("possession_pct", "% possession"), ("shots", "shots"), ("difficulty", "difficulty"),
                ("before", "before"), ("after", "after"), ("passes", "passes"), ("build_up_s", "s build-up")]


def key_numbers(evidence: dict[str, Any]) -> list[KeyNumber]:
    out = []
    for key, label in _NUMBER_KEYS:
        value = evidence.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = f"{value:.0%}" if key in ("before", "after") and value <= 1 else f"{value:g}"
            out.append(KeyNumber(label=label, value=text))
    return out[:3]


def insight(brief: MomentBrief) -> Insight:
    why = WHY.get(brief.type, brief.headline)
    return Insight(why_it_matters=why, tactical_theme=THEMES.get(brief.type, "game_state"),
                   key_numbers=key_numbers(brief.evidence), analyst_note=brief.headline)


def commentary(brief: MomentBrief) -> Commentary:
    tone = "electric" if brief.importance >= 0.8 else "measured"
    return Commentary(line=brief.headline, tone=tone)


def overlay(brief: MomentBrief, ins: Insight, profile: FanProfile, team_id: str | None) -> OverlayCard:
    titles = TITLES.get(profile.language, TITLES["en"])
    words = brief.headline.split()
    body = " ".join(words[:30])
    priority = "high" if brief.importance >= 0.8 else ("low" if brief.importance < 0.4 else "normal")
    return OverlayCard(
        title=titles.get(brief.type, TITLES["en"].get(brief.type, brief.type.replace("_", " ").title())),
        body=body, why=ins.why_it_matters if profile.language == "en" else "",
        analyst_note=ins.analyst_note if profile.mode == "analyst" and profile.language == "en" else "",
        stat_chips=ins.key_numbers[:3],
        highlight_player_id=brief.players[0].id if brief.players else None,
        highlight_team_id=team_id, priority=priority,
    )


def catch_up(payload: dict[str, Any]) -> CatchUp:
    moments = payload.get("moments_missed", [])
    return CatchUp(headline=payload["score_now"], summary=" ".join(m["what"] for m in moments[-3:]) or payload["score_now"],
                   key_points=[f"{m['clock']} {m['what']}" for m in moments[-3:]])


def player_report(payload: dict[str, Any]) -> PlayerReport:
    player, stats = payload["player"], payload["player"]["stats"]
    return PlayerReport(
        headline=f"{player['short_name']}: rating {stats['rating']}",
        summary=f"{stats['passes']} passes, {stats['shots']} shots, {stats['tackles']} tackles, "
                f"{stats['distance_km']} km covered.",
        key_stats=[KeyNumber(label="Rating", value=str(stats["rating"])),
                   KeyNumber(label="Passes", value=str(stats["passes"])),
                   KeyNumber(label="Distance (km)", value=str(stats["distance_km"]))],
    )


def recap(payload: dict[str, Any], profile: FanProfile) -> Recap:
    moments = payload.get("key_moments", [])
    return Recap(
        headline=payload["score"],
        summary=f"{payload['score']}. " + " ".join(m["headline"] for m in moments[:3]),
        key_moments=[f"{m['clock']} {m['headline']}" for m in moments[:5]],
        tactical_story="; ".join(m["why"] for m in moments[:2]) or payload["score"],
        player_of_the_match=payload.get("player_of_the_match", ""),
    )
