"""Event-level match simulator producing synthetic, football-realistic data.

The simulation follows the ball: whoever has it passes, carries, dribbles or shoots,
and the defending team presses, tackles and intercepts. Outcomes depend on player
attributes, team style, fatigue and a hidden momentum that drifts during the match.
Hidden drivers (momentum, tactical shifts) are recorded in `GroundTruth` so the
analysis pipeline can be checked against what really happened.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from itg.schema import (
    PITCH_LENGTH as L,
    PITCH_WIDTH as W,
    Event,
    EventType as E,
    GroundTruth,
    Lineup,
    LineupSlot,
    Match,
    MatchInfo,
    MomentumSample,
    Player,
    TacticalShift,
    Team,
    TeamStyle,
)
from itg.synthetic.clubs import FORMATIONS, starting_lineup

GENERATOR_VERSION = "0.1.0"
HALF_S = 45 * 60
SPRINT_KMH = 25.2  # Common tracking-data threshold for a sprint.

_VENUES = ["The xG Dome", "Pressure Cooker Park", "Pasta Bowl Stadium", "Splinter Lane"]
_WEATHER = ["clear, 14°C", "light rain, 9°C", "overcast, 11°C", "windy, 8°C", "clear evening, 17°C"]
_REFEREES = ["A. Whitcombe", "M. Laurent", "J. Okoro", "S. Brandt", "R. Castell"]

# A coach's instruction is a clear change of behaviour, not a nudge.
TACTIC_CHANGES = {
    "high_press": ("starts pressing much higher up the pitch", dict(pressing=+0.4)),
    "low_block": ("drops into a deep defensive block", dict(pressing=-0.4, possession=-0.2)),
    "go_direct": ("switches to long, direct balls forward", dict(directness=+0.5, possession=-0.2)),
    "possession_control": ("slows the game down to keep the ball", dict(possession=+0.35, directness=-0.35)),
}


@dataclass
class _OnPitch:
    info: Player
    role: str
    base_x: float
    base_y: float
    group: str
    fatigue: float = 0.0  # 0 = fresh, 1 = exhausted.
    distance_m: float = 0.0
    top_speed_kmh: float = 0.0
    sprints: int = 0
    yellow: bool = False

    def attr(self, name: str) -> float:
        return getattr(self.info.attributes, name) * (1 - 0.12 * self.fatigue)

    def register_speed(self, kmh: float) -> None:
        self.top_speed_kmh = max(self.top_speed_kmh, kmh)
        if kmh >= SPRINT_KMH:
            self.sprints += 1


@dataclass
class _Side:
    team: Team
    is_home: bool
    on_pitch: list[_OnPitch]
    bench: list[Player]
    style: TeamStyle
    momentum: float = 0.0
    goals: int = 0
    sub_minutes: list[int] = field(default_factory=list)

    @property
    def missing(self) -> int:
        """Players short of eleven (red cards)."""
        return max(0, 11 - len(self.on_pitch))

    @property
    def goalkeeper(self) -> _OnPitch:
        return next((p for p in self.on_pitch if p.group == "GK"), self.on_pitch[0])


@dataclass
class SimState:
    """A match situation to continue simulating from (forecasts and what-if scenarios)."""

    period: int
    clock_s: float  # Seconds into the current period.
    home_goals: int = 0
    away_goals: int = 0
    home_style: TeamStyle | None = None  # None keeps the club's usual style.
    away_style: TeamStyle | None = None
    home_on_pitch: list[str] | None = None  # Player ids; None keeps the starting XI.
    away_on_pitch: list[str] | None = None
    home_momentum: float = 0.0
    away_momentum: float = 0.0
    home_in_possession: bool = True


@dataclass
class _LastPass:
    passer: _OnPitch
    clock: float
    success: bool
    cross: bool


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _mirror(x: float, y: float) -> tuple[float, float]:
    return L - x, W - y


class MatchSimulator:
    def __init__(self, home: Team, away: Team, seed: int, kickoff: datetime | None = None):
        self.rng = random.Random(seed)
        self.seed = seed
        self.match_id = f"{home.short_name}-{away.short_name}-{seed}".lower()
        self.kickoff = kickoff or datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc) + timedelta(days=seed % 60)
        self.home = self._make_side(home, is_home=True)
        self.away = self._make_side(away, is_home=False)
        self.events: list[Event] = []
        self.seq = 0
        self.period = 1
        self.clock = 0.0  # Seconds since the start of the current period.
        self.side = self.home
        self.carrier = self.home.on_pitch[-1]
        self.bx, self.by = L / 2, W / 2  # Ball, in the frame of the team in possession.
        self.last_pass: _LastPass | None = None
        self.set_piece: tuple[str, float] | None = None
        self.shifts = self._plan_tactical_shifts()
        self.fast = False  # Fast mode skips event records: used for thousands of forecast runs.
        self.applied_shifts: dict[str, set[str]] = {}
        self.momentum_log: list[MomentumSample] = []

    # ------------------------------------------------------------------ setup

    def _make_side(self, team: Team, is_home: bool) -> _Side:
        starters, bench = starting_lineup(team)
        roles = {role: (x, y, group) for role, x, y, group in FORMATIONS[team.formation]}
        on_pitch = [_OnPitch(p, role, *roles[role]) for role, p in starters]
        return _Side(team, is_home, on_pitch, bench, team.style.model_copy())

    def _plan_tactical_shifts(self) -> list[TacticalShift]:
        shifts = []
        for _ in range(self.rng.choice([1, 2, 2])):
            side = self.rng.choice([self.home, self.away])
            # A coach only asks for something the team is not already doing.
            style = side.team.style
            options = [kind for kind, ok in (
                ("high_press", style.pressing < 0.75), ("low_block", style.pressing > 0.35),
                ("go_direct", style.directness < 0.6), ("possession_control", style.directness > 0.3),
            ) if ok]
            kind = self.rng.choice(options)
            minute = self.rng.randint(25, 75)
            shifts.append(TacticalShift(
                team_id=side.team.id, minute=minute, kind=kind,
                description=f"{side.team.name} {TACTIC_CHANGES[kind][0]}.",
            ))
        return sorted(shifts, key=lambda s: s.minute)

    # ---------------------------------------------------------------- helpers

    @property
    def opp(self) -> _Side:
        return self.away if self.side is self.home else self.home

    def other(self, side: _Side) -> _Side:
        return self.away if side is self.home else self.home

    @property
    def minute(self) -> int:
        return int(self.clock // 60) + (45 if self.period == 2 else 0)

    def _abs(self, side: _Side, x: float, y: float) -> tuple[float, float]:
        """Convert a point from a team's attacking frame to the absolute pitch frame."""
        attacks_right = side.is_home == (self.period == 1)
        return (x, y) if attacks_right else _mirror(x, y)

    def _display_clock(self) -> str:
        base = 45 if self.period == 2 else 0
        if self.clock < HALF_S:
            return f"{base + int(self.clock // 60) + 1}'"
        return f"{base + 45}+{int((self.clock - HALF_S) // 60) + 1}'"

    def _emit(self, type_: E, side: _Side | None = None, player: _OnPitch | None = None,
              xy: tuple[float, float] | None = None, end: tuple[float, float] | None = None,
              outcome: str | None = None, **details) -> None:
        """Record an event. `xy` and `end` are given in the acting team's frame."""
        if self.fast:
            return
        self.seq += 1
        x = y = ex = ey = None
        if xy is not None and side is not None:
            x, y = (round(v, 1) for v in self._abs(side, *xy))
        if end is not None and side is not None:
            ex, ey = (round(v, 1) for v in self._abs(side, *end))
        self.events.append(Event(
            id=f"{self.match_id}-{self.seq:05d}", seq=self.seq, match_id=self.match_id,
            period=self.period, clock_s=round(self.clock + (HALF_S if self.period == 2 else 0), 1),
            display_clock=self._display_clock(), type=type_,
            team_id=side.team.id if side else None, player_id=player.info.id if player else None,
            x=x, y=y, end_x=ex, end_y=ey, outcome=outcome, details=details,
        ))

    def _pos(self, side: _Side, p: _OnPitch, ball_x: float) -> tuple[float, float]:
        """Approximate position of a player: the team block shifts with the ball."""
        if p.group == "GK":
            return 5 + max(0.0, (ball_x - L / 2) * 0.12), W / 2
        return _clamp(p.base_x + (ball_x - L / 2) * 0.45, 2, L - 2), p.base_y

    def _nearest(self, side: _Side, x: float, y: float, ball_x: float,
                 exclude: _OnPitch | None = None, groups: tuple[str, ...] | None = None,
                 jitter: float = 3.0) -> _OnPitch:
        pool = [p for p in side.on_pitch if p is not exclude and (groups is None or p.group in groups)]
        pool = pool or [p for p in side.on_pitch if p is not exclude] or side.on_pitch

        def score(p: _OnPitch) -> float:
            px, py = self._pos(side, p, ball_x)
            return math.hypot(px - x, py - y) + self.rng.uniform(0, jitter)

        return min(pool, key=score)

    def _intent(self, side: _Side) -> float:
        """Attacking intent from momentum and scoreline (late chasers push, late leaders sit)."""
        diff = side.goals - self.other(side).goals
        state = 0.0
        if diff < 0 and self.minute >= 65:
            state = 0.4
        elif diff > 0 and self.minute >= 75:
            state = -0.3
        return _clamp(0.6 * side.momentum + state, -1, 1)

    # ------------------------------------------------------------ match flow

    def run(self) -> Match:
        first = self.rng.choice([self.home, self.away])
        for period in (1, 2):
            self._play_period(period, first if period == 1 else self.other(first))
        self._emit(E.MATCH_END, score={"home": self.home.goals, "away": self.away.goals})
        return self._build_match()

    def run_from(self, state: SimState) -> tuple[int, int]:
        """Play the rest of a match from `state`, without event records. Returns the final score."""
        self.fast = True
        self.shifts = []  # The future holds no scripted surprise: only the state as it is.
        minute = state.clock_s / 60 + (45 if state.period == 2 else 0)
        for side, goals, style, on_pitch, momentum in (
            (self.home, state.home_goals, state.home_style, state.home_on_pitch, state.home_momentum),
            (self.away, state.away_goals, state.away_style, state.away_on_pitch, state.away_momentum),
        ):
            side.goals, side.momentum = goals, momentum
            if style is not None:
                side.style = style.model_copy()
            if on_pitch is not None:
                self._set_lineup(side, on_pitch)
            for p in side.on_pitch:
                p.fatigue = min(1.0, minute * 0.0105 * (1.3 - p.info.attributes.stamina / 100))
        first = self.home if state.home_in_possession else self.away
        if state.period == 1:
            self._play_period(1, first, start_clock=state.clock_s)
            self._play_period(2, self.other(first))
        else:
            self._play_period(2, first, start_clock=state.clock_s)
        return self.home.goals, self.away.goals

    def _set_lineup(self, side: _Side, player_ids: list[str]) -> None:
        """Put exactly these players on the pitch; newcomers take a departed player's role."""
        current = {p.info.id: p for p in side.on_pitch}
        squad = {p.id: p for p in side.team.players}
        free = [p for p in side.on_pitch if p.info.id not in player_ids]
        lineup = [current[pid] for pid in player_ids if pid in current]
        for pid in player_ids:
            if pid in current or pid not in squad or not free:
                continue
            info = squad[pid]
            slot = next((p for p in free if p.group == info.position), free[0])
            free.remove(slot)
            lineup.append(_OnPitch(info, slot.role, slot.base_x, slot.base_y, slot.group))
        side.on_pitch = lineup or side.on_pitch
        side.bench = [p for p in side.bench if p.id not in player_ids]

    def _play_period(self, period: int, kickoff_side: _Side, start_clock: float = 0.0) -> None:
        self.period, self.clock = period, start_clock
        added = self.rng.randint(1, 3) if period == 1 else self.rng.randint(3, 6)
        end = HALF_S + added * 60 + self.rng.uniform(0, 40)
        if period == 2:
            first_sub = max(56, 46 + int(start_clock // 60))
            for side in (self.home, self.away):
                count = self.rng.randint(2, 3) if first_sub <= 88 else 0
                side.sub_minutes = sorted(self.rng.randint(first_sub, 88) for _ in range(count))
        self._emit(E.PERIOD_START, home_attacks="right" if period == 1 else "left")
        self.side = kickoff_side
        self._kickoff(kickoff_side)
        announced, next_tick = start_clock >= HALF_S - 20, (start_clock // 60 + 1) * 60
        while self.clock < end:
            if not announced and self.clock >= HALF_S - 20:
                self._emit(E.ADDED_TIME, minutes=added)
                announced = True
            while self.clock >= next_tick:
                self._minute_tick()
                next_tick += 60
            self._step()
        self._emit(E.PERIOD_END, score={"home": self.home.goals, "away": self.away.goals})

    def _minute_tick(self) -> None:
        minute = self.minute
        for side in (self.home, self.away):
            side.momentum = _clamp(side.momentum * 0.88 + self.rng.gauss(0, 0.13), -1, 1)
        if not self.fast:
            self.momentum_log.append(MomentumSample(
                period=self.period, minute=minute, home=round(self.home.momentum, 3), away=round(self.away.momentum, 3)))
        for shift in self.shifts:
            if shift.minute == minute and shift.kind not in self.applied_shifts.get(shift.team_id, ()):
                self.applied_shifts.setdefault(shift.team_id, set()).add(shift.kind)
                side = self.home if shift.team_id == self.home.team.id else self.away
                for key, delta in TACTIC_CHANGES[shift.kind][1].items():
                    setattr(side.style, key, _clamp(getattr(side.style, key) + delta, 0.05, 0.95))
        for side in (self.home, self.away):
            self._physical_update(side)
            while side.sub_minutes and side.sub_minutes[0] <= minute:
                side.sub_minutes.pop(0)
                self._substitution(side)

    def _physical_update(self, side: _Side) -> None:
        rates = {"GK": 0.95, "DF": 1.78, "MF": 2.02, "FW": 1.88}  # metres per second
        for p in side.on_pitch:
            rate = rates[p.group] * (1 + 0.06 * side.momentum + 0.05 * side.style.pressing) * (1 - 0.15 * p.fatigue)
            metres = 60 * rate * self.rng.uniform(0.9, 1.1)
            p.distance_m += metres
            p.fatigue = min(1.0, p.fatigue + 0.0105 * (1.3 - p.info.attributes.stamina / 100) * metres / 110)
            if self.fast:
                continue
            if p.group != "GK" and self.rng.random() < 0.35:
                p.register_speed(self.rng.gauss(22 + 0.08 * p.attr("pace"), 2))
        if self.fast:
            return
        self._emit(E.PHYSICAL_UPDATE, side, players=[
            {"player_id": p.info.id, "distance_m": round(p.distance_m),
             "top_speed_kmh": round(p.top_speed_kmh, 1), "sprints": p.sprints}
            for p in side.on_pitch
        ])

    def _substitution(self, side: _Side) -> None:
        outfield = [p for p in side.on_pitch if p.group != "GK"]
        if not side.bench or not outfield:
            return
        out = max(outfield, key=lambda p: p.fatigue + self.rng.uniform(0, 0.1))
        incoming = next((b for b in side.bench if b.position == out.group),
                        next((b for b in side.bench if b.position != "GK"), None))
        if incoming is None:
            return
        side.bench.remove(incoming)
        new = _OnPitch(incoming, out.role, out.base_x, out.base_y, out.group)
        side.on_pitch[side.on_pitch.index(out)] = new
        if self.carrier is out:
            self.carrier = new
        self._emit(E.SUBSTITUTION, side, out, player_in_id=incoming.id, role=out.role)

    # --------------------------------------------------------------- restarts

    def _change_possession(self, side: _Side, player: _OnPitch, x: float, y: float, cause: str) -> None:
        previous = self.side
        self.side, self.carrier, self.bx, self.by = side, player, x, y
        self.last_pass = None
        self.set_piece = None
        if previous is not side:
            self._emit(E.POSSESSION_CHANGE, side, player, (x, y), cause=cause, from_team_id=previous.team.id)

    def _kickoff(self, side: _Side) -> None:
        taker = self._nearest(side, L / 2, W / 2, L / 2, groups=("FW",), jitter=0)
        self._change_possession(side, taker, L / 2, W / 2, "kickoff")
        self._emit(E.KICKOFF, side, taker, (L / 2, W / 2))

    def _restart(self, kind: E, side: _Side, x: float, y: float, delay: tuple[float, float]) -> None:
        self.clock += self.rng.uniform(*delay)
        if kind == E.GOAL_KICK:
            taker = side.goalkeeper
        elif kind == E.CORNER:
            taker = max((p for p in side.on_pitch if p.group in ("MF", "FW")),
                        key=lambda p: p.attr("passing") + self.rng.uniform(0, 10), default=side.on_pitch[-1])
        else:
            taker = self._nearest(side, x, y, x, groups=("DF", "MF", "FW") if kind == E.THROW_IN else None)
        self._change_possession(side, taker, x, y, kind.value)
        self._emit(kind, side, taker, (x, y))
        if kind in (E.CORNER, E.FREE_KICK):
            self.set_piece = (kind.value, self.clock)

    def _out_of_play(self, side: _Side, x: float, y: float) -> None:
        """Ball went out after `side` touched it last; (x, y) in `side`'s frame."""
        opp = self.other(side)
        if x >= L - 1:
            self._restart(E.GOAL_KICK, opp, 5.5, W / 2, (15, 30))
        elif x <= 1:
            corner_y = 0.0 if y < W / 2 else W
            self._restart(E.CORNER, opp, *_mirror(0.0, corner_y), (20, 40))
        else:
            touchline = 0.0 if y < W / 2 else W
            self._restart(E.THROW_IN, opp, *_mirror(x, touchline), (8, 20))

    def _corner_for(self, side: _Side, near_y: float) -> None:
        self._restart(E.CORNER, side, L, 0.0 if near_y < W / 2 else W, (20, 40))

    # ---------------------------------------------------------------- actions

    def _step(self) -> None:
        if self.set_piece and self.clock - self.set_piece[1] < 1 and self.set_piece[0] == "corner":
            self._pass(pressured=False, cross=True)
            return
        if self.set_piece and self.set_piece[0] == "free_kick" and self.clock - self.set_piece[1] < 1 \
                and self.bx >= 75 and 20 <= self.by <= 48 and self.rng.random() < 0.35:
            self._shot("free_kick")
            return
        pressured = self._maybe_pressure()
        if pressured is None:
            return  # The pressure ended in a foul and play restarted.
        c, bx, by = self.carrier, self.bx, self.by
        in_box = bx >= 88.5 and 13.84 <= by <= 54.16
        intent = self._intent(self.side)
        shot_p = 0.0
        if c.group != "GK":
            if in_box:
                lp = self.last_pass
                cross_bonus = 0.2 if lp and lp.cross and lp.success else 0.0
                shot_p = 0.08 + 0.04 * intent + cross_bonus + 0.02 * self.opp.missing
            elif bx >= 75 and 18 <= by <= 50:
                shot_p = 0.018 + 0.015 * intent + (c.attr("finishing") - 70) / 2000
        carry_p = 0.05 if c.group == "GK" else 0.16 + (c.attr("dribbling") - 60) / 300 + (-0.04 if pressured else 0.08)
        r = self.rng.random()
        if r < shot_p:
            situation = "open_play"
            if self.set_piece and self.clock - self.set_piece[1] < 12:
                situation = self.set_piece[0]
            self._shot(situation)
        elif r < shot_p + carry_p:
            self._carry(pressured)
        else:
            self._pass(pressured)

    def _maybe_pressure(self) -> bool | None:
        """Maybe press the ball carrier. Returns None when the pressure ends in a foul."""
        opp, bx = self.opp, self.bx
        pressing = opp.style.pressing
        # Pressing teams press disproportionately more, and above all high up the pitch.
        zone = 1.0 + pressing if bx < 35 else 0.6 if bx < 70 else 0.85
        avg_fatigue = sum(p.fatigue for p in opp.on_pitch) / len(opp.on_pitch)
        p = 0.04 + 0.42 * pressing ** 1.5 * zone + 0.08 * opp.momentum - 0.15 * avg_fatigue
        p *= (1 - 0.12 * opp.missing)  # A team down to ten cannot press as often.
        if self.rng.random() >= p:
            return False
        dx, dy = _mirror(bx, self.by)
        presser = self._nearest(opp, dx, dy, dx)
        presser.fatigue = min(1.0, presser.fatigue + 0.002)
        self._emit(E.PRESSURE, opp, presser, (dx, dy),
                   target_player_id=self.carrier.info.id, distance_m=round(self.rng.uniform(0.5, 3.5), 1))
        in_box = bx >= 88.5 and 13.84 <= self.by <= 54.16
        if self.rng.random() < (0.005 if in_box else 0.07):
            self._foul(opp, presser, self.carrier)
            return None
        return True

    def _pass(self, pressured: bool, cross: bool = False) -> None:
        side, opp, c = self.side, self.opp, self.carrier
        bx, by, st = self.bx, self.by, side.style
        if not cross and c.group != "GK" and bx >= 76 and (by < 15 or by > 53) and self.rng.random() < 0.4:
            cross = True
        if cross:
            tx, ty = self.rng.uniform(91, 101), self.rng.uniform(24, 44)
            recv = self._nearest(side, tx, ty, bx, exclude=c, groups=("MF", "FW"), jitter=6)
            ex, ey = tx, ty
        else:
            long_p = (0.02 + 0.4 * st.directness ** 1.5 * (1.0 if bx < 55 else 0.4)
                      + (0.2 if pressured and bx < 30 else 0) + (0.35 if c.group == "GK" else 0))
            long_ball = self.rng.random() < long_p
            if long_ball:
                tx, ty = bx + self.rng.uniform(28, 50), by + self.rng.gauss(0, 18)
            else:
                tx, ty = bx + self.rng.gauss(3 + 7 * (1 - st.possession), 9), by + self.rng.gauss(0, 13)
            tx, ty = _clamp(tx, 2, L - 2), _clamp(ty, 1, W - 1)
            recv = self._nearest(side, tx, ty, bx, exclude=c, groups=None if tx < 25 else ("DF", "MF", "FW"), jitter=4)
            rx, ry = self._pos(side, recv, bx)
            # Short passes go to feet; on long balls the receiver runs onto the ball.
            pull = 0.2 if long_ball else 0.5
            ex = _clamp(tx + (rx - tx) * pull + self.rng.gauss(0, 2), 1, L - 0.5)
            ey = _clamp(ty + (ry - ty) * pull + self.rng.gauss(0, 2), 0.5, W - 0.5)
        length = math.hypot(ex - bx, ey - by)
        height = "high" if cross or length >= 30 else ("low" if length >= 18 and self.rng.random() < 0.3 else "ground")
        speed = (45 + 0.6 * length if height == "high" else 28 + 0.9 * length) + self.rng.gauss(0, 4)
        # Momentum, and the extra space when the opponent is a player short.
        momentum_edge = 0.04 * (side.momentum - opp.momentum) + 0.035 * (opp.missing - side.missing)
        if cross:
            p = 0.30 + (c.attr("passing") - 72) / 250 + momentum_edge
        else:
            p = (0.975 - max(0.0, length - 12) * 0.0065 - (0.13 if pressured else 0)
                 - max(0.0, ex - bx) * 0.004 - (0.18 if ex > 88 else 0)
                 - (0.05 if height == "high" else 0)
                 + (c.attr("passing") - 72) / 220 + momentum_edge)
        success = self.rng.random() < _clamp(p, 0.05, 0.98)
        travel = length / (speed / 3.6)
        details = dict(recipient_id=recv.info.id, length_m=round(length, 1), ball_speed_kmh=round(speed, 1),
                       height=height, cross=cross, under_pressure=pressured)

        if success and not cross and ex - bx > 15 and ex > 78 and self.rng.random() < 0.10:
            self._emit(E.PASS, side, c, (bx, by), (ex, ey), "fail", **details, offside=True)
            self.clock += travel
            self._emit(E.OFFSIDE, side, recv, (ex, ey))
            self._restart(E.FREE_KICK, opp, *_mirror(ex, ey), (12, 25))
            return

        self._emit(E.PASS, side, c, (bx, by), (ex, ey), "success" if success else "fail", **details)
        self.clock += travel
        self.last_pass = _LastPass(c, self.clock, success, cross)
        if success:
            self.carrier, self.bx, self.by = recv, ex, ey
            self.clock += self.rng.uniform(1.0, 3.2)
            return

        mx, my = _mirror(ex, ey)
        if cross:
            r = self.rng.random()
            if r < 0.45:
                defender = self._nearest(opp, mx, my, mx)
                cx, cy = self.rng.uniform(25, 55), self.rng.uniform(5, W - 5)
                self._emit(E.CLEARANCE, opp, defender, (mx, my), (cx, cy))
                self.clock += 2
                follow = self.rng.random()
                if follow < 0.22:
                    self._corner_for(side, ey)
                elif follow < 0.6:
                    self._change_possession(opp, self._nearest(opp, cx, cy, cx), cx, cy, "clearance")
                else:
                    sx, sy = _mirror(cx, cy)
                    winner = self._nearest(side, sx, sy, sx)
                    self.carrier, self.bx, self.by = winner, sx, sy
                    self._emit(E.BALL_RECOVERY, side, winner, (sx, sy))
            elif r < 0.75:
                gk = opp.goalkeeper
                self._emit(E.INTERCEPTION, opp, gk, (mx, my), method="claim")
                self.clock += self.rng.uniform(3, 8)
                self._change_possession(opp, gk, 8.0, W / 2, "claim")
            elif r < 0.88:
                self._corner_for(side, ey)  # Deflected behind by a defender.
            else:
                self._out_of_play(side, L, ey)
            return

        if ex >= L - 1.5 or self.rng.random() < 0.16:
            self._out_of_play(side, ex, ey)
            return
        # A misplaced pass is either cut out on its way or picked up as a loose ball.
        if self.rng.random() < 0.12:
            f = self.rng.uniform(0.35, 0.95)
            ix, iy = _mirror(bx + (ex - bx) * f, by + (ey - by) * f)
            kind, cause = E.INTERCEPTION, "interception"
        else:
            ix, iy = _mirror(_clamp(ex + self.rng.gauss(0, 4), 1, L - 1), _clamp(ey + self.rng.gauss(0, 4), 1, W - 1))
            kind, cause = E.BALL_RECOVERY, "loose_ball"
        defender = self._nearest(opp, ix, iy, ix)
        self._emit(kind, opp, defender, (ix, iy))
        self._change_possession(opp, defender, ix, iy, cause)

    def _carry(self, pressured: bool) -> None:
        side, opp, c = self.side, self.opp, self.carrier
        bx, by = self.bx, self.by
        if (pressured and self.rng.random() < 0.6) or self.rng.random() < 0.15:
            dx, dy = _mirror(bx, by)
            defender = self._nearest(opp, dx, dy, dx)
            p = (0.52 + (c.attr("dribbling") - defender.attr("defending")) / 140
                 + 0.05 * (side.momentum - opp.momentum) + 0.05 * (opp.missing - side.missing))
            if self.rng.random() < p:
                self._emit(E.DRIBBLE, side, c, (bx, by), outcome="success", opponent_id=defender.info.id)
            else:
                self._emit(E.DRIBBLE, side, c, (bx, by), outcome="fail", opponent_id=defender.info.id)
                in_box = bx >= 88.5 and 13.84 <= by <= 54.16
                # Defenders are far more careful inside their own box.
                if self.rng.random() < (0.02 if in_box else 0.18):
                    self._foul(opp, defender, c)
                    return
                self._emit(E.TACKLE, opp, defender, (dx, dy), outcome="success", opponent_id=c.info.id)
                self._change_possession(opp, defender, dx, dy, "tackle")
                return
        if c.group == "GK":
            dist = self.rng.uniform(2, 6)
        else:
            dist = self.rng.uniform(3, 10) if pressured else self.rng.uniform(5, 22)
        angle = self.rng.gauss(0, 0.9)
        ex = _clamp(bx + dist * math.cos(angle), 1, L - 1)
        ey = _clamp(by + dist * math.sin(angle), 1, W - 1)
        dist = math.hypot(ex - bx, ey - by)
        speed = _clamp(10 + 0.18 * c.attr("pace") + self.rng.gauss(0, 2.5) + (3 if dist > 15 else 0), 7, 36)
        self._emit(E.CARRY, side, c, (bx, by), (ex, ey), distance_m=round(dist, 1), speed_kmh=round(speed, 1))
        c.register_speed(speed)
        self.clock += dist / (speed / 3.6)
        self.bx, self.by = ex, ey

    def _foul(self, fouler_side: _Side, fouler: _OnPitch, victim: _OnPitch) -> None:
        victim_side = self.other(fouler_side)
        # The ball position is in the frame of the team in possession, which is the victim's.
        vx, vy = self.bx, self.by
        self._emit(E.FOUL, fouler_side, fouler, _mirror(vx, vy), fouled_player_id=victim.info.id)
        self._maybe_card(fouler_side, fouler)
        if vx >= 88.5 and 13.84 <= vy <= 54.16:
            self._emit(E.PENALTY_AWARDED, victim_side, victim, (vx, vy))
            self.clock += self.rng.uniform(45, 90)
            taker = max(victim_side.on_pitch, key=lambda p: p.attr("finishing"))
            self.side, self.carrier, self.bx, self.by = victim_side, taker, 94.0, W / 2
            self._shot("penalty")
            return
        self._restart(E.FREE_KICK, victim_side, vx, vy, (15, 35))

    def _maybe_card(self, side: _Side, player: _OnPitch) -> None:
        r = self.rng.random()
        if r < 0.006 or (r < 0.14 and player.yellow):
            card = "red" if r < 0.006 else "second_yellow"
        elif r < 0.14:
            card, player.yellow = "yellow", True
        else:
            return
        self._emit(E.CARD, side, player, card=card)
        if card != "yellow" and len(side.on_pitch) > 7 and player.group != "GK":
            side.on_pitch.remove(player)

    def _shot(self, situation: str) -> None:
        side, opp, c = self.side, self.opp, self.carrier
        bx, by = self.bx, self.by
        lp = self.last_pass
        header = (situation != "penalty" and lp is not None and lp.cross and lp.success
                  and self.clock - lp.clock < 3)
        dist = math.hypot(L - bx, W / 2 - by)
        dx = L - bx
        angle = abs(math.atan2(7.32 * dx, dx * dx + (by - W / 2) ** 2 - 3.66 ** 2))
        if situation == "penalty":
            base = 0.76
        else:
            base = 1 / (1 + math.exp(-(-2.25 - 0.11 * dist + 1.6 * angle - (0.9 if header else 0))))
        gk = opp.goalkeeper
        p_goal = _clamp(base * (1 + (c.attr("finishing") - gk.attr("goalkeeping")) / 90), 0.01, 0.95)
        if header:
            speed = _clamp(self.rng.gauss(55, 8), 35, 80)
        else:
            speed = _clamp(self.rng.gauss(92 + (c.attr("finishing") - 70) * 0.4 - max(0.0, 12 - dist), 11), 45, 132)

        r = self.rng.random()
        if r < p_goal:
            result = "goal"
        else:
            rest = (r - p_goal) / (1 - p_goal)
            if situation != "penalty" and rest < 0.30:
                result = "blocked"
            elif rest < 0.55:
                result = "saved"
            else:
                result = "off_target"
        if result == "goal":
            end = (L, W / 2 + self.rng.uniform(-3.4, 3.4))
        elif result == "saved":
            end = (L, W / 2 + self.rng.uniform(-3.2, 3.2))
        elif result == "off_target":
            end = (L, W / 2 + self.rng.choice([-1, 1]) * self.rng.uniform(4, 14))
        else:
            end = (bx + (L - bx) * 0.25, by + (W / 2 - by) * 0.25)
        self._emit(E.SHOT, side, c, (bx, by), end, "success" if result == "goal" else "fail",
                   result=result, situation=situation, body_part="head" if header else "foot",
                   distance_m=round(dist, 1), speed_kmh=round(speed, 1), keeper_id=gk.info.id)
        self.clock += math.hypot(end[0] - bx, end[1] - by) / (speed / 3.6)
        c.fatigue = min(1.0, c.fatigue + 0.003)

        if result == "goal":
            side.goals += 1
            side.momentum = _clamp(side.momentum + 0.3, -1, 1)
            opp.momentum = _clamp(opp.momentum - 0.15, -1, 1)
            assist = lp.passer.info.id if lp and lp.success and lp.passer is not c and self.clock - lp.clock < 12 else None
            self._emit(E.GOAL, side, c, (bx, by), situation=situation, assist_id=assist,
                       score={"home": self.home.goals, "away": self.away.goals})
            self.clock += self.rng.uniform(50, 80)
            self._kickoff(opp)
        elif result == "saved":
            self._emit(E.SAVE, opp, gk, _mirror(*end), shot_speed_kmh=round(speed, 1))
            if self.rng.random() < 0.35:
                self._corner_for(side, end[1])
            else:
                self.clock += self.rng.uniform(3, 8)
                self._change_possession(opp, gk, 6.0, W / 2, "save")
        elif result == "blocked":
            mx, my = _mirror(*end)
            blocker = self._nearest(opp, mx, my, mx, exclude=gk)
            self._emit(E.BLOCK, opp, blocker, (mx, my))
            follow = self.rng.random()
            if follow < 0.35:
                self._corner_for(side, by)
            elif follow < 0.7:
                self._change_possession(opp, blocker, mx, my, "block")
            else:
                self.bx = _clamp(end[0] - 6, 60, L - 10)
                winner = self._nearest(side, self.bx, self.by, self.bx)
                self.carrier = winner
                self._emit(E.BALL_RECOVERY, side, winner, (self.bx, self.by))
        else:
            self._restart(E.GOAL_KICK, opp, 5.5, W / 2, (15, 30))
        self.set_piece = None

    # ----------------------------------------------------------------- output

    def _build_match(self) -> Match:
        lineups = []
        for side in (self.home, self.away):
            starters, bench = starting_lineup(side.team)
            lineups.append(Lineup(
                team_id=side.team.id, formation=side.team.formation,
                starting=[LineupSlot(player_id=p.id, role=role) for role, p in starters],
                bench=[p.id for p in bench],
            ))
        info = MatchInfo(
            match_id=self.match_id,
            competition="Prompt the Pitch League",
            kickoff=self.kickoff,
            venue=self.rng.choice(_VENUES),
            weather=self.rng.choice(_WEATHER),
            referee=self.rng.choice(_REFEREES),
            home_team_id=self.home.team.id,
            away_team_id=self.away.team.id,
            lineups=lineups,
            seed=self.seed,
            generator_version=GENERATOR_VERSION,
        )
        return Match(
            info=info,
            teams=[self.home.team, self.away.team],
            events=self.events,
            final_score={"home": self.home.goals, "away": self.away.goals},
            ground_truth=GroundTruth(tactical_shifts=self.shifts, momentum=self.momentum_log),
        )


def simulate_match(home: Team, away: Team, seed: int = 42) -> Match:
    return MatchSimulator(home, away, seed).run()
