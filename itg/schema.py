"""Data model shared by the whole pipeline: clubs, match metadata and match events.

Coordinates are in metres on a 105 x 68 pitch, in an absolute frame:
x = 0 is the left goal line, x = 105 the right one, y = 0 the bottom touchline.
The home team attacks towards x = 105 in the first half and towards x = 0 in the second.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

PITCH_LENGTH = 105.0
PITCH_WIDTH = 68.0

Position = Literal["GK", "DF", "MF", "FW"]


class EventType(StrEnum):
    PERIOD_START = "period_start"
    KICKOFF = "kickoff"
    PASS = "pass"
    CARRY = "carry"
    DRIBBLE = "dribble"
    PRESSURE = "pressure"
    TACKLE = "tackle"
    INTERCEPTION = "interception"
    CLEARANCE = "clearance"
    BLOCK = "block"
    BALL_RECOVERY = "ball_recovery"
    POSSESSION_CHANGE = "possession_change"
    SHOT = "shot"
    SAVE = "save"
    GOAL = "goal"
    FOUL = "foul"
    CARD = "card"
    PENALTY_AWARDED = "penalty_awarded"
    FREE_KICK = "free_kick"
    CORNER = "corner"
    THROW_IN = "throw_in"
    GOAL_KICK = "goal_kick"
    OFFSIDE = "offside"
    SUBSTITUTION = "substitution"
    ADDED_TIME = "added_time"
    PHYSICAL_UPDATE = "physical_update"
    PERIOD_END = "period_end"
    MATCH_END = "match_end"


class Attributes(BaseModel):
    """Player ratings from 1 to 99."""

    pace: int
    passing: int
    dribbling: int
    finishing: int
    defending: int
    goalkeeping: int
    stamina: int


class Player(BaseModel):
    id: str
    name: str
    short_name: str
    number: int
    position: Position
    nationality: str
    attributes: Attributes


class TeamStyle(BaseModel):
    """Tactical identity, each value between 0 and 1."""

    possession: float = Field(description="Preference for short, patient build-up.")
    pressing: float = Field(description="Intensity of pressure without the ball.")
    directness: float = Field(description="Preference for long, vertical passes.")


class Team(BaseModel):
    id: str
    name: str
    short_name: str
    colors: tuple[str, str]
    formation: str
    style: TeamStyle
    players: list[Player]


class LineupSlot(BaseModel):
    player_id: str
    role: str


class Lineup(BaseModel):
    team_id: str
    formation: str
    starting: list[LineupSlot]
    bench: list[str]


class MatchInfo(BaseModel):
    match_id: str
    competition: str
    kickoff: datetime
    venue: str
    weather: str
    referee: str
    home_team_id: str
    away_team_id: str
    lineups: list[Lineup]
    seed: int
    generator_version: str
    pitch: dict[str, float] = {"length": PITCH_LENGTH, "width": PITCH_WIDTH}


class Event(BaseModel):
    id: str
    seq: int
    match_id: str
    period: int
    clock_s: float = Field(description="Match clock in seconds; the second half starts at 2700.")
    display_clock: str = Field(description="Broadcast-style minute, e.g. \"23'\" or \"45+2'\".")
    type: EventType
    team_id: str | None = None
    player_id: str | None = None
    x: float | None = None
    y: float | None = None
    end_x: float | None = None
    end_y: float | None = None
    outcome: Literal["success", "fail"] | None = None
    details: dict[str, Any] = {}


class TacticalShift(BaseModel):
    team_id: str
    minute: int
    kind: str
    description: str


class MomentumSample(BaseModel):
    period: int
    minute: int
    home: float
    away: float


class GroundTruth(BaseModel):
    """Hidden drivers of the simulation, never streamed.

    Used to check whether the analysis pipeline recovers what really happened.
    """

    tactical_shifts: list[TacticalShift]
    momentum: list[MomentumSample]


class Match(BaseModel):
    info: MatchInfo
    teams: list[Team]
    events: list[Event]
    final_score: dict[str, int]
    ground_truth: GroundTruth

    def team(self, team_id: str) -> Team:
        return next(t for t in self.teams if t.id == team_id)

    def players_by_id(self) -> dict[str, Player]:
        return {p.id: p for t in self.teams for p in t.players}
