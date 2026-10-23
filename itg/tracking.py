"""Approximate tracking data for the 3D view, rebuilt from the event stream.

The simulator records events (who did what, where), not where the other 21 players were.
This module places every player on the pitch at each event: the actor on the ball, the
receiver where the pass arrives, and everyone else at their formation slot, shifted with the
ball and with possession, moving no faster than a sprinting footballer. It runs after the
simulation, so the matches, statistics and recorded agent runs are left untouched.
"""

from __future__ import annotations

import math
from typing import Any

from itg.schema import PITCH_LENGTH as L, PITCH_WIDTH as W, Event, EventType as E, Match
from itg.synthetic.clubs import FORMATIONS

HALF_TIME_BREAK_S = 30.0  # Continuous playback time between the two halves.
RUN_SPEED = 7.5  # m/s for players off the ball.
SPRINT_SPEED = 9.5  # m/s when a player runs to reach the ball in time.
SMOOTHING_PASSES = 3
ON_BALL = {E.PASS, E.CARRY, E.DRIBBLE, E.SHOT, E.KICKOFF, E.FREE_KICK, E.THROW_IN, E.CORNER, E.GOAL_KICK,
           E.POSSESSION_CHANGE}
KEY_EVENTS = {E.GOAL, E.SHOT, E.SAVE, E.CARD, E.SUBSTITUTION, E.CORNER, E.KICKOFF, E.PERIOD_START,
              E.PERIOD_END, E.MATCH_END, E.PENALTY_AWARDED, E.FOUL, E.OFFSIDE}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class _Tracker:
    def __init__(self, match: Match):
        self.match = match
        self.home_id = match.info.home_team_id
        self.team_of: dict[str, str] = {}
        self.slot: dict[str, tuple[float, float, str]] = {}  # player id -> base x, base y, group
        self.ids: list[str] = []
        self.on_pitch: set[str] = set()
        for lineup in match.info.lineups:
            roles = {role: (x, y, group) for role, x, y, group in FORMATIONS[lineup.formation]}
            for s in lineup.starting:
                self.slot[s.player_id] = roles[s.role]
                self.on_pitch.add(s.player_id)
                self.ids.append(s.player_id)
            self.ids += lineup.bench
            for pid in [s.player_id for s in lineup.starting] + lineup.bench:
                self.team_of[pid] = lineup.team_id
        self.pos: dict[str, tuple[float, float]] = {}
        self.ball = (L / 2, W / 2)
        self.possession: str | None = None
        self.period = 1
        self.p1_end = 45 * 60.0
        self.last_t = 0.0
        self.frames: list[tuple[float, tuple[float, float, float], dict[str, tuple[float, float]]]] = []
        self.hard: list[set[str]] = []  # Players who must be exactly where the frame puts them (on the ball).

    # ---------------------------------------------------------------- frames

    def attacks_right(self, team_id: str) -> bool:
        return (team_id == self.home_id) == (self.period == 1)

    def to_abs(self, team_id: str, x: float, y: float) -> tuple[float, float]:
        return (x, y) if self.attacks_right(team_id) else (L - x, W - y)

    def to_team(self, team_id: str, x: float, y: float) -> tuple[float, float]:
        return self.to_abs(team_id, x, y)  # The mirror is its own inverse.

    def time(self, e: Event) -> float:
        if e.period == 1:
            return e.clock_s
        return self.p1_end + HALF_TIME_BREAK_S + (e.clock_s - 45 * 60)

    # --------------------------------------------------------------- targets

    def shape(self, kickoff: bool = False) -> dict[str, tuple[float, float]]:
        """Where every player would stand given the ball and possession."""
        targets = {}
        for pid in self.on_pitch:
            team = self.team_of[pid]
            bx, by = self.to_team(team, *self.ball)
            base_x, base_y, group = self.slot[pid]
            if kickoff:
                x, y = (5.0, W / 2) if group == "GK" else (min(base_x * 0.55, 49.0), base_y)
            elif group == "GK":
                x, y = 5 + max(0.0, (bx - L / 2) * 0.12), W / 2 + (by - W / 2) * 0.2
            else:
                push = 5.0 if team == self.possession else -5.0
                x = _clamp(base_x + (bx - L / 2) * 0.45 + push, 3, L - 3)
                y = _clamp(base_y + (by - base_y) * 0.25, 2, W - 2)
            targets[pid] = self.to_abs(team, x, y)
        return targets

    def step(self, t: float, targets: dict[str, tuple[float, float]], fast: set[str], arc: float = 0.0) -> None:
        dt = max(0.05, t - self.last_t)
        for pid, (tx, ty) in targets.items():
            px, py = self.pos.get(pid, (tx, ty))
            dist = math.hypot(tx - px, ty - py)
            reach = RUN_SPEED * dt
            if dist > reach and pid not in fast:
                k = reach / dist
                tx, ty = px + (tx - px) * k, py + (ty - py) * k
            self.pos[pid] = (tx, ty)
        self.last_t = t
        self.frames.append((round(t, 2), (round(self.ball[0], 1), round(self.ball[1], 1), round(arc, 1)),
                            {pid: self.pos[pid] for pid in self.on_pitch}))
        self.hard.append(set(fast))

    def smooth(self) -> None:
        """Make the runs physical: players start early to reach the ball on time, and no one
        moves faster than a sprint between two frames unless they are on the ball."""
        frames = self.frames

        def limit(a: dict, b: dict, pid: str, dt: float, move_a: bool) -> None:
            ax, ay = a[pid]
            bx, by = b[pid]
            dist = math.hypot(bx - ax, by - ay)
            reach = SPRINT_SPEED * dt
            if dist <= reach:
                return
            k = reach / dist
            if move_a:  # Pull the earlier position towards the later one: an earlier start.
                a[pid] = (bx + (ax - bx) * k, by + (ay - by) * k)
            else:
                b[pid] = (ax + (bx - ax) * k, ay + (by - ay) * k)

        for _ in range(SMOOTHING_PASSES):
            for i in range(len(frames) - 2, -1, -1):
                a, b = frames[i][2], frames[i + 1][2]
                dt = max(0.05, frames[i + 1][0] - frames[i][0])
                for pid in a.keys() & b.keys():
                    if pid not in self.hard[i]:
                        limit(a, b, pid, dt, move_a=True)
            for i in range(len(frames) - 1):
                a, b = frames[i][2], frames[i + 1][2]
                dt = max(0.05, frames[i + 1][0] - frames[i][0])
                for pid in a.keys() & b.keys():
                    if pid not in self.hard[i + 1]:
                        limit(a, b, pid, dt, move_a=False)

    # ---------------------------------------------------------------- events

    def run(self) -> dict[str, Any]:
        events = [e for e in self.match.events if e.type != E.PHYSICAL_UPDATE]
        key_events = []
        for i, e in enumerate(events):
            if e.type == E.PERIOD_END and e.period == 1:
                self.p1_end = e.clock_s
            self.period = e.period
            t = self.time(e)
            if e.type == E.SUBSTITUTION:
                incoming = e.details["player_in_id"]
                self.slot[incoming] = self.slot[e.player_id]
                self.on_pitch.discard(e.player_id)
                self.on_pitch.add(incoming)
                self.pos[incoming] = (L / 2, W + 1)  # Comes on from the touchline.
            elif e.type == E.CARD and e.details.get("card") != "yellow":
                self.on_pitch.discard(e.player_id)
            if e.type in KEY_EVENTS:
                key_events.append({"t": round(t, 2), "clock": e.display_clock, "period": e.period,
                                   "type": e.type.value, "team_id": e.team_id, "player_id": e.player_id,
                                   "result": e.details.get("result"), "card": e.details.get("card"),
                                   "player_in_id": e.details.get("player_in_id")})
            if e.type in ON_BALL and e.team_id:
                self.possession = e.team_id
            if e.x is not None and e.type not in (E.GOAL, E.PERIOD_END, E.MATCH_END):
                self.ball = (e.x, e.y)
            kickoff = e.type in (E.KICKOFF, E.PERIOD_START)
            targets = self.shape(kickoff=kickoff)
            fast: set[str] = set()
            if e.player_id in self.on_pitch and e.x is not None:
                if e.type == E.PRESSURE:  # The presser closes down from its own side of the ball.
                    gx = -1.5 if self.attacks_right(e.team_id) else 1.5
                    targets[e.player_id] = (e.x + gx, e.y)
                else:
                    targets[e.player_id] = (e.x, e.y)
                    fast.add(e.player_id)
            if kickoff and e.type == E.KICKOFF and e.player_id in self.on_pitch:
                targets[e.player_id] = (L / 2, W / 2)
            self.step(t, targets, fast)

            # The ball travels: add a frame when it arrives (unless the next event comes first).
            if e.end_x is not None and e.type in (E.PASS, E.CARRY, E.SHOT):
                travel = self._travel(e)
                nxt = self.time(events[i + 1]) if i + 1 < len(events) else t + travel + 1
                if travel and t + travel < nxt - 0.05:
                    arc = 0.0
                    if e.type == E.PASS and e.details.get("height") == "high":
                        arc = min(14.0, 2 + e.details["length_m"] * 0.22)
                    elif e.type == E.SHOT and e.details.get("body_part") != "head":
                        arc = 1.2
                    self.frames[-1] = (*self.frames[-1][:1], (*self.frames[-1][1][:2], arc), self.frames[-1][2])
                    self.ball = (e.end_x, e.end_y)
                    if e.type == E.SHOT and e.details.get("result") == "goal":  # Into the back of the net.
                        self.ball = (e.end_x + (1.4 if e.end_x > L / 2 else -1.4), e.end_y)
                    targets = self.shape()
                    fast = set()
                    receiver = e.player_id if e.type == E.CARRY else e.details.get("recipient_id")
                    if receiver in self.on_pitch and e.outcome != "fail":
                        targets[receiver] = (e.end_x, e.end_y)
                        fast.add(receiver)
                    self.step(t + travel, targets, fast)
        self.smooth()
        return self._export(key_events)

    @staticmethod
    def _travel(e: Event) -> float:
        d = e.details
        if e.type == E.CARRY:
            return d.get("distance_m", 0) / max(1.0, d.get("speed_kmh", 15) / 3.6)
        length = d.get("length_m") or math.hypot(e.end_x - e.x, e.end_y - e.y)
        speed = d.get("ball_speed_kmh") or d.get("speed_kmh") or 60
        return length / max(1.0, speed / 3.6)

    def _export(self, key_events: list[dict[str, Any]]) -> dict[str, Any]:
        index = {pid: i for i, pid in enumerate(self.ids)}
        frames = []
        for t, ball, positions in self.frames:
            flat: list[float | None] = [None] * (2 * len(self.ids))
            for pid, (x, y) in positions.items():
                flat[2 * index[pid]], flat[2 * index[pid] + 1] = round(x, 1), round(y, 1)
            frames.append([t, *ball, flat])
        players = {p.id: p for t in self.match.teams for p in t.players}
        return {
            "match_id": self.match.info.match_id,
            "home_team_id": self.home_id,
            "teams": [{"id": t.id, "name": t.name, "short_name": t.short_name, "colors": list(t.colors)}
                      for t in self.match.teams],
            "players": {pid: {"number": players[pid].number, "name": players[pid].short_name} for pid in self.ids},
            "ids": self.ids,
            "team_of": [self.team_of[pid] for pid in self.ids],
            "goalkeepers": [pid for pid in self.ids if self.slot.get(pid, (0, 0, ""))[2] == "GK"],
            "period_offsets": {"1": 0.0, "2": round(self.p1_end + HALF_TIME_BREAK_S - 45 * 60, 2)},
            "frames": frames,
            "events": key_events,
        }


def build_tracking(match: Match) -> dict[str, Any]:
    """Frames of [t, ball x, ball y, ball arc height, [x0, y0, x1, y1, ...]] in pitch metres."""
    return _Tracker(match).run()
