"""Build the briefs given to the agents and the default viewer profiles."""

from __future__ import annotations

from typing import Any

from itg.agents.contracts import FanProfile, MomentBrief, MomentResult, PlayerBrief
from itg.schema import Match, Team
from itg.stats.models import Signal, Snapshot

TEAM_KEYS = ("goals", "possession_pct", "shots", "shots_on_target", "xg", "passes", "pass_accuracy",
             "high_turnovers", "pressures", "ppda", "corners", "distance_km")
PLAYER_KEYS = ("minutes", "goals", "assists", "shots", "xg", "xa", "key_passes", "passes", "pass_accuracy",
               "dribbles_won", "tackles", "interceptions", "distance_km", "top_speed_kmh", "sprints", "rating")


def _names(value: Any, players: dict[str, str]) -> Any:
    """Replace player ids by player names inside evidence, recursively."""
    if isinstance(value, str):
        return players.get(value, value)
    if isinstance(value, dict):
        return {k: _names(v, players) for k, v in value.items()}
    if isinstance(value, list):
        return [_names(v, players) for v in value]
    return value


def score_line(snap: Snapshot, home: str, away: str) -> str:
    return f"{home} {snap.score['home']}-{snap.score['away']} {away}"


def build_brief(signal: Signal, snap: Snapshot, recent: list[MomentResult],
                win_probability: dict[str, float] | None = None) -> MomentBrief:
    teams = list(snap.teams.values())
    home, away = teams[0], teams[1]
    names = {pid: p.short_name for pid, p in snap.players.items()}
    team_names = {t.team_id: t.name for t in teams}
    phase = None
    if snap.phase:
        phase = snap.phase.model_dump()
        phase["team_id"] = team_names.get(phase["team_id"])
    return MomentBrief(
        moment_id=signal.id,
        type=signal.type,
        importance=signal.importance,
        clock=signal.display_clock,
        headline=signal.headline,
        evidence=_names({k: v for k, v in signal.data.items() if v is not None}, {**names, **team_names}),
        home_team=home.name,
        away_team=away.name,
        score=score_line(snap, home.name, away.name),
        team=team_names.get(signal.team_id),
        players=[
            PlayerBrief(id=pid, name=snap.players[pid].short_name, team=team_names[snap.players[pid].team_id],
                        position=snap.players[pid].position,
                        stats={k: getattr(snap.players[pid], k) for k in PLAYER_KEYS})
            for pid in signal.player_ids if pid in snap.players
        ],
        team_stats={t.name: {k: getattr(t, k) for k in TEAM_KEYS} for t in teams},
        momentum=snap.momentum,
        win_probability=win_probability,
        phase=phase,
        recent_moments=[f"{r.signal.display_clock} {r.signal.headline}" for r in recent[-5:]],
    )


REPORT_KEYS = PLAYER_KEYS + ("passes_completed", "avg_pass_difficulty", "progressive_passes", "progressive_carries",
                             "dribbles", "recoveries", "pressures", "fouls", "yellow_cards", "red_cards", "saves")


def player_payload(player_id: str, snap: Snapshot, involvement: list[Signal]) -> dict[str, Any]:
    """Facts for a player check-in: live stats, the moments that involved them, the match context."""
    p = snap.players[player_id]
    team = snap.teams[p.team_id]
    home, away = snap.teams.values()
    return {
        "clock": snap.display_clock,
        "score": score_line(snap, home.name, away.name),
        "player": {"name": p.name, "short_name": p.short_name, "number": p.number, "position": p.position,
                   "team": team.name, "on_pitch": p.on_pitch, "stats": {k: getattr(p, k) for k in REPORT_KEYS}},
        "team_context": {k: getattr(team, k) for k in ("possession_pct", "shots", "xg", "pass_accuracy")},
        "moments_involving_player": [f"{s.display_clock} {s.headline}" for s in involvement][-6:],
    }


LANGUAGES = ("en", "fr", "es", "pt")


def default_roles(match: Match) -> list[FanProfile]:
    """The four default viewers, before choosing a language."""
    home, away = match.teams
    starters = {slot.player_id for lineup in match.info.lineups for slot in lineup.starting}
    star = max((p for p in home.players if p.id in starters and p.position == "FW"),
               key=lambda p: p.attributes.finishing)
    return [
        FanProfile(id="analyst", role="analyst", label="Studio analyst", mode="analyst"),
        FanProfile(id="home-fan", role="home-fan", label=f"{home.name} fan", mode="casual",
                   favorite_team_id=home.id),
        FanProfile(id="away-fan", role="away-fan", label=f"{away.name} fan", mode="casual",
                   favorite_team_id=away.id),
        FanProfile(id="player-focus", role="player-focus", label=f"Following {star.short_name}", mode="casual",
                   favorite_team_id=home.id, favorite_player_id=star.id, player_focus=True),
    ]


def with_languages(roles: list[FanProfile], languages: list[str] | tuple[str, ...]) -> list[FanProfile]:
    """One profile per (role, language)."""
    return [role.model_copy(update={"id": f"{role.role or role.id}.{lang}", "role": role.role or role.id,
                                    "language": lang})
            for role in roles for lang in languages]


def default_profiles(match: Match, languages: list[str] | tuple[str, ...] = ("en",)) -> list[FanProfile]:
    return with_languages(default_roles(match), languages)


def team_by_id(teams: list[Team], team_id: str | None) -> Team | None:
    return next((t for t in teams if t.id == team_id), None)
