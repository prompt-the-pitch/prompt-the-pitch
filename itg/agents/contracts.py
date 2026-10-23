"""Data exchanged between the agents: viewer profiles, moment briefs and agent outputs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from itg.stats.models import Signal


class FanProfile(BaseModel):
    """Who is watching, in which language. The same moment becomes a different overlay for each.

    A role (analyst, home fan, ...) is written in several languages; each (role, language)
    pair is one profile, with id "<role>.<language>".
    """

    id: str
    role: str = ""
    label: str
    mode: Literal["analyst", "casual"]
    language: str = Field("en", description="Language code, e.g. en, fr, es, pt, ar.")
    favorite_team_id: str | None = None
    favorite_player_id: str | None = None
    player_focus: bool = Field(False, description="Follow the favourite player throughout the match.")


class PlayerBrief(BaseModel):
    id: str
    name: str
    team: str
    position: str
    stats: dict[str, Any]


class MomentBrief(BaseModel):
    """Everything an agent may use to talk about one moment. Agents must not go beyond it."""

    moment_id: str
    type: str
    importance: float
    clock: str
    headline: str
    evidence: dict[str, Any]
    home_team: str
    away_team: str
    score: str
    team: str | None
    players: list[PlayerBrief]
    team_stats: dict[str, dict[str, Any]]
    momentum: float = Field(description="-100 (away dominates) to +100 (home dominates).")
    win_probability: dict[str, float] | None = Field(
        None, description="Chances of each outcome from simulating the rest of the match, keyed by team name and 'draw'.")
    phase: dict[str, Any] | None
    recent_moments: list[str]


class KeyNumber(BaseModel):
    label: str
    value: str


Theme = Literal["control", "chaos", "pressure", "rhythm", "individual_quality", "set_piece", "game_state", "physical"]


class Insight(BaseModel):
    why_it_matters: str = Field(description="One or two sentences, at most 45 words.")
    tactical_theme: Theme
    key_numbers: list[KeyNumber] = Field(description="One to three numbers taken from the brief.")
    analyst_note: str = Field(description="One or two sentences of deeper tactical detail.")


class Commentary(BaseModel):
    line: str = Field(description="One live commentary line, at most 30 words.")
    tone: Literal["electric", "tense", "measured", "analytical", "light"]


class OverlayCard(BaseModel):
    title: str = Field(description="At most 6 words.")
    body: str = Field(description="At most 30 words.")
    why: str = Field("", description="Why the moment matters, for this viewer, at most 35 words.")
    analyst_note: str = Field("", description="Deeper tactical detail, analysts only, at most 40 words.")
    stat_chips: list[KeyNumber] = Field(description="Up to three numbers from the brief.")
    highlight_player_id: str | None = None
    highlight_team_id: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"


class Recap(BaseModel):
    headline: str = Field(description="At most 12 words.")
    summary: str = Field(description="Three to five sentences.")
    key_moments: list[str] = Field(description="Three to five items, each starting with the minute.")
    tactical_story: str = Field(description="Two sentences on how the game was won or lost tactically.")
    player_of_the_match: str


class PlayerReport(BaseModel):
    headline: str = Field(description="At most 8 words.")
    summary: str = Field(description="At most 45 words: what the player has done and how well.")
    key_stats: list[KeyNumber] = Field(description="Up to three numbers from the input.")


class CatchUp(BaseModel):
    headline: str = Field(description="At most 10 words.")
    summary: str = Field(description="At most 60 words: what happened while the viewer was away.")
    key_points: list[str] = Field(description="Up to three short items, each starting with the minute.")


class AgentTrace(BaseModel):
    agent: str
    status: Literal["ok", "retried", "fallback", "skipped"]
    latency_ms: int
    detail: str | None = None


class MomentResult(BaseModel):
    moment_id: str
    signal: Signal
    insight: Insight
    commentary: Commentary
    overlays: dict[str, OverlayCard] = Field(description="Overlay card per profile id.")
    display_seconds: int
    trace: list[AgentTrace]


class PlayerReportResult(BaseModel):
    """A check-in on one player, every 15 minutes, in each language served."""

    player_id: str
    period: int
    clock_s: float
    clock: str
    reports: dict[str, PlayerReport] = Field(description="Report per language code.")
    trace: list[AgentTrace]


class RecapResult(BaseModel):
    kind: Literal["half_time", "full_time"]
    clock: str
    recaps: dict[str, Recap] = Field(description="Recap per profile id.")
    trace: list[AgentTrace]
