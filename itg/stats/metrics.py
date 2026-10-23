"""Stateless football metrics: pitch geometry, expected goals and pass difficulty.

These models are deliberately simple and transparent so that every number shown on
screen can be explained in one sentence.
"""

from __future__ import annotations

import math

from itg.schema import PITCH_LENGTH as L, PITCH_WIDTH as W

BOX_X = 88.5
BOX_Y = (13.84, 54.16)
FINAL_THIRD_X = 70.0
HIGH_REGAIN_X = 65.0  # Within 40 m of the opponent's goal.
LONG_PASS_M = 30.0


def to_team_frame(x: float, y: float, team_is_home: bool, period: int) -> tuple[float, float]:
    """Convert absolute coordinates to the frame where the team attacks towards x = 105."""
    attacks_right = team_is_home == (period == 1)
    return (x, y) if attacks_right else (L - x, W - y)


def in_box(x: float, y: float) -> bool:
    return x >= BOX_X and BOX_Y[0] <= y <= BOX_Y[1]


def distance_to_goal(x: float, y: float) -> float:
    return math.hypot(L - x, W / 2 - y)


def goal_angle(x: float, y: float) -> float:
    """Angle in radians under which the goal mouth is seen from (x, y)."""
    dx = L - x
    return abs(math.atan2(7.32 * dx, dx * dx + (y - W / 2) ** 2 - 3.66 ** 2))


def expected_goals(x: float, y: float, body_part: str = "foot", situation: str = "open_play") -> float:
    """Probability that a shot from (x, y), in the attacking frame, is scored."""
    if situation == "penalty":
        return 0.76
    logit = -1.2 - 0.13 * distance_to_goal(x, y) + 1.3 * goal_angle(x, y)
    if body_part == "head":
        logit -= 0.9
    if situation == "free_kick":
        logit -= 0.5
    return min(0.95, max(0.01, 1 / (1 + math.exp(-logit))))


def is_progressive(x: float, y: float, end_x: float, end_y: float) -> bool:
    """Brings the ball meaningfully closer to goal: 30 m within the own half,
    15 m when crossing halfway, 10 m inside the opponent's half."""
    gain = distance_to_goal(x, y) - distance_to_goal(end_x, end_y)
    if end_x < L / 2:
        return gain >= 30
    if x < L / 2:
        return gain >= 15
    return gain >= 10


def pass_difficulty(length_m: float, x: float, end_x: float, end_y: float, under_pressure: bool,
                    height: str, cross: bool) -> float:
    """Difficulty rating from 0 (trivial) to 100 (exceptional), in the attacking frame."""
    score = 10 + min(length_m, 50) * 0.9
    score += 15 if under_pressure else 0
    score += 10 if height == "high" else 0
    score += 15 if cross else 0
    score += max(0.0, end_x - x) * 0.5
    score += 12 if in_box(end_x, end_y) else (8 if end_x >= FINAL_THIRD_X else 0)
    return round(min(100.0, score), 1)


# Completion rate observed per difficulty level (calibrated on simulated matches).
_COMPLETION_CURVE = [(0, 0.97), (15, 0.96), (25, 0.93), (35, 0.87), (45, 0.79), (55, 0.71),
                     (65, 0.57), (75, 0.49), (85, 0.45), (100, 0.40)]


def expected_completion(difficulty: float) -> float:
    """Completion probability implied by a difficulty rating."""
    for (d0, p0), (d1, p1) in zip(_COMPLETION_CURVE, _COMPLETION_CURVE[1:]):
        if difficulty <= d1:
            return p0 + (p1 - p0) * (max(difficulty, d0) - d0) / (d1 - d0)
    return _COMPLETION_CURVE[-1][1]
