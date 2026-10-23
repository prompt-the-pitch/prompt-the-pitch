"""Live statistics and detected signals produced by the stats engine."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class TeamStats(BaseModel):
    team_id: str
    name: str
    short_name: str
    goals: int = 0
    possession_s: float = 0.0
    possession_pct: float = 50.0
    passes: int = 0
    passes_completed: int = 0
    pass_accuracy: float = 0.0
    avg_pass_difficulty: float = 0.0
    progressive_passes: int = 0
    long_passes: int = 0
    crosses: int = 0
    final_third_entries: int = 0
    box_entries: int = 0
    shots: int = 0
    shots_on_target: int = 0
    xg: float = 0.0
    big_chances: int = 0
    corners: int = 0
    fouls: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    offsides: int = 0
    pressures: int = 0
    high_pressures: int = 0
    tackles: int = 0
    interceptions: int = 0
    recoveries: int = 0
    clearances: int = 0
    blocks: int = 0
    saves: int = 0
    high_turnovers: int = 0
    ppda: float | None = None
    distance_km: float = 0.0
    sprints: int = 0


class PlayerStats(BaseModel):
    player_id: str
    name: str
    short_name: str
    number: int
    team_id: str
    position: str
    on_pitch: bool = False
    minutes: int = 0
    touches: int = 0
    passes: int = 0
    passes_completed: int = 0
    pass_accuracy: float = 0.0
    avg_pass_difficulty: float = 0.0
    pass_value_added: float = 0.0
    key_passes: int = 0
    progressive_passes: int = 0
    carries: int = 0
    progressive_carries: int = 0
    dribbles: int = 0
    dribbles_won: int = 0
    shots: int = 0
    shots_on_target: int = 0
    xg: float = 0.0
    xa: float = 0.0
    goals: int = 0
    assists: int = 0
    tackles: int = 0
    interceptions: int = 0
    recoveries: int = 0
    pressures: int = 0
    fouls: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    saves: int = 0
    distance_km: float = 0.0
    top_speed_kmh: float = 0.0
    sprints: int = 0
    fastest_shot_kmh: float = 0.0
    rating: float = 6.0


class MomentumPoint(BaseModel):
    period: int
    minute: int
    value: float  # -100 (away dominates) to +100 (home dominates).


Phase = Literal["control", "chaos", "balanced"]


class GamePhase(BaseModel):
    phase: Phase
    team_id: str | None = None  # The controlling team, for "control".
    chaos_index: float  # 0 = calm, 50 = typical, 100 = frantic.
    turnovers_per_min: float
    passes_per_possession: float
    long_ball_share: float


SignalType = Literal[
    "goal", "big_chance", "big_save", "penalty", "card", "substitution", "thunderbolt",
    "defence_splitting_pass", "pass_sequence", "high_turnover", "counter_attack",
    "speed_alert", "distance_milestone", "momentum_swing", "dominance", "phase_change",
    "tactical_shift", "added_time", "half_time", "full_time",
]


class Signal(BaseModel):
    """A moment worth surfacing, with the evidence behind it.

    The headline is a plain factual sentence; agents turn signals into explanations,
    narratives and personalized overlays.
    """

    id: str
    type: SignalType
    period: int
    clock_s: float
    display_clock: str
    team_id: str | None = None
    player_ids: list[str] = []
    importance: float  # 0 to 1.
    headline: str
    data: dict[str, Any] = {}
    event_ids: list[str] = []


class ShotPoint(BaseModel):
    """A shot in its team's attacking frame (always towards x = 105)."""

    team_id: str
    player_id: str
    clock: str
    x: float
    y: float
    xg: float
    result: str
    body_part: str


class NetworkNode(BaseModel):
    player_id: str
    x: float  # Average position on the ball, in the team's attacking frame.
    y: float
    touches: int


class NetworkEdge(BaseModel):
    source: str
    target: str
    passes: int


class PassNetwork(BaseModel):
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]


class Snapshot(BaseModel):
    match_id: str
    period: int
    clock_s: float
    display_clock: str
    score: dict[str, int]
    teams: dict[str, TeamStats]
    players: dict[str, PlayerStats]
    momentum: float
    momentum_series: list[MomentumPoint]
    phase: GamePhase | None
    tempo_passes_per_min: float
    signals: list[Signal]
    shots: list[ShotPoint] = []
    pass_networks: dict[str, PassNetwork] = {}
