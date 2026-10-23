"""Incremental stats engine: feed events one at a time, read live stats and signals.

`ingest(event)` updates every counter and returns the signals (key-moment candidates)
that this event produced. Once per match minute the engine also looks at rolling
windows to compute momentum, the control-vs-chaos phase and tactical shifts.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from itg.schema import Event, EventType as E, Match, MatchInfo, Team
from itg.stats import metrics as m
from itg.stats.models import (
    GamePhase,
    MomentumPoint,
    NetworkEdge,
    NetworkNode,
    PassNetwork,
    PlayerStats,
    ShotPoint,
    Signal,
    SignalType,
    Snapshot,
    TeamStats,
)

POSSESSION_GAP_CAP_S = 6.0  # Longer gaps are dead-ball time and do not count as possession.
MOMENTUM_WINDOW = 5  # minutes
MOMENTUM_SCALE = 0.05  # Threat difference per minute that maps to ~76/100.
PHASE_WINDOW = 5  # minutes
# Tactical shift detection: minutes after / before a candidate change point.
SHIFT_MIN_AFTER, SHIFT_MAX_AFTER, SHIFT_BEFORE, SHIFT_MIN_BEFORE = 8, 20, 25, 10
SHIFT_MIN_Z, SHIFT_MIN_CHANGE = 2.8, 0.35
SHIFT_COOLDOWN_MIN = 20
SPEED_ALERT_KMH = 33.5
DISTANCE_MILESTONE_KM = 10.0

# Threat added to the momentum of the team in possession.
THREAT = {"final_third_entry": 0.015, "box_entry": 0.04, "corner": 0.03, "high_turnover": 0.03}


@dataclass
class _Bucket:
    period: int
    minute: int
    counts: dict[str, Counter] = field(default_factory=dict)


@dataclass
class _Possession:
    team_id: str
    period: int
    start_clock: float
    regain_x: float | None = None
    cause: str | None = None
    passes: int = 0
    last_pass: tuple[str, float] | None = None  # (passer id, clock) of the last completed pass
    sequence_flagged: bool = False
    entered_final_third: bool = False
    entered_box: bool = False
    attack_flagged: bool = False  # A counter or pressing-win signal was already raised.


_SHIFT_KINDS = {
    "pressing": ("pressing_up", "pressing_down"),
    "directness": ("more_direct", "less_direct"),
}
_SHIFT_PHRASES = {
    "pressing_up": "have been pressing much higher",
    "pressing_down": "have dropped off and stopped pressing high",
    "more_direct": "have gone more direct",
    "less_direct": "have cut out the long balls",
}


def _dispersion(series: list[tuple[float, float]]) -> float:
    """Overdispersion of per-minute rates relative to a binomial (at least 1).

    Football rates drift with momentum and fatigue, so minute-to-minute variance is
    larger than pure chance; scaling the standard error by it avoids false alarms.
    """
    k = sum(a for a, _ in series)
    n = sum(b for _, b in series)
    used = [(a, b) for a, b in series if b > 0]
    if n == 0 or k == 0 or k == n or len(used) < 3:
        return 1.0
    p = k / n
    chi2 = sum((a - b * p) ** 2 / (b * p * (1 - p)) for a, b in used)
    return max(1.0, chi2 / (len(used) - 1))


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


class StatsEngine:
    def __init__(self, info: MatchInfo, teams: list[Team]):
        self.info = info
        self.home_id, self.away_id = info.home_team_id, info.away_team_id
        self.teams = {t.id: TeamStats(team_id=t.id, name=t.name, short_name=t.short_name) for t in teams}
        self.players: dict[str, PlayerStats] = {}
        for t in teams:
            for p in t.players:
                self.players[p.id] = PlayerStats(
                    player_id=p.id, name=p.name, short_name=p.short_name, number=p.number,
                    team_id=t.id, position=p.position)
        self._on_since: dict[str, float] = {}
        self._played_s: Counter = Counter()
        for lineup in info.lineups:
            for slot in lineup.starting:
                self.players[slot.player_id].on_pitch = True
                self._on_since[slot.player_id] = 0.0

        self.buckets: list[_Bucket] = []
        self.possession: _Possession | None = None
        self.last: Event | None = None
        self.signals: list[Signal] = []
        self.momentum_series: list[MomentumPoint] = []
        self.phase: GamePhase | None = None
        self._pending: list[Signal] = []
        self._elapsed = 0.0  # Continuous playing time across both halves, in seconds.
        self._p1_length = 0.0
        self._cooldown: dict[str, float] = {}
        self._difficulty_sum: Counter = Counter()
        self._ppda_passes: Counter = Counter()  # Opponent passes in their own 60%, per pressing team.
        self._ppda_actions: Counter = Counter()  # Defensive actions in the opponent's 60%.
        self._conceded: Counter = Counter()  # Goals conceded while on the pitch (goalkeepers).
        self._max_deficit: Counter = Counter()
        self._last_shot: dict[str, Any] | None = None
        self._speed_alerted: set[str] = set()
        self._distance_flagged: set[str] = set()
        self._speed_record = 0.0
        self._shots: list[ShotPoint] = []
        self._on_ball: dict[str, list[float]] = {}  # player id -> [sum x, sum y, count] on completed passes
        self._links: Counter = Counter()  # (passer, receiver) -> completed passes

    @classmethod
    def for_match(cls, match: Match) -> StatsEngine:
        return cls(match.info, match.teams)

    # ------------------------------------------------------------------ API

    def ingest(self, event: Event) -> list[Signal]:
        self._pending = []
        self._advance_time(event)
        handler = getattr(self, f"_on_{event.type.value}", None)
        if handler is not None:
            handler(event)
        self.last = event
        return self._pending

    def snapshot(self, recent_signals: int = 20) -> Snapshot:
        total_poss = sum(t.possession_s for t in self.teams.values()) or 1.0
        for tid, t in self.teams.items():
            t.possession_pct = round(100 * t.possession_s / total_poss, 1)
            t.pass_accuracy = round(t.passes_completed / t.passes, 3) if t.passes else 0.0
            t.avg_pass_difficulty = round(self._difficulty_sum[tid] / t.passes, 1) if t.passes else 0.0
            t.xg = round(t.xg, 2)
            t.ppda = round(self._ppda_passes[tid] / self._ppda_actions[tid], 1) if self._ppda_actions[tid] else None
            squad = [p for p in self.players.values() if p.team_id == tid]
            t.distance_km = round(sum(p.distance_km for p in squad), 1)
            t.sprints = sum(p.sprints for p in squad)
        for pid, p in self.players.items():
            played = self._played_s[pid] + (self._elapsed - self._on_since[pid] if p.on_pitch else 0.0)
            p.minutes = round(played / 60)
            p.pass_accuracy = round(p.passes_completed / p.passes, 3) if p.passes else 0.0
            p.avg_pass_difficulty = round(self._difficulty_sum[pid] / p.passes, 1) if p.passes else 0.0
            p.xg, p.xa = round(p.xg, 2), round(p.xa, 2)
            p.pass_value_added = round(p.pass_value_added, 2)
            p.rating = self._rating(p)
        last = self.last
        return Snapshot(
            match_id=self.info.match_id,
            period=last.period if last else 1,
            clock_s=last.clock_s if last else 0.0,
            display_clock=last.display_clock if last else "0'",
            score=self.score,
            teams={k: v.model_copy() for k, v in self.teams.items()},
            players={k: v.model_copy() for k, v in self.players.items()},
            momentum=self.momentum_series[-1].value if self.momentum_series else 0.0,
            momentum_series=list(self.momentum_series),
            phase=self.phase,
            tempo_passes_per_min=self._tempo(),
            signals=self.signals[-recent_signals:] if recent_signals else [],
            shots=list(self._shots),
            pass_networks={tid: self._pass_network(tid) for tid in self.teams},
        )

    def _pass_network(self, team_id: str, min_passes: int = 3, max_edges: int = 30) -> PassNetwork:
        """Average on-ball positions and the most used passing links of a team."""
        nodes = [NetworkNode(player_id=pid, x=round(sx / n, 1), y=round(sy / n, 1), touches=int(n))
                 for pid, (sx, sy, n) in self._on_ball.items() if self.players[pid].team_id == team_id and n >= 3]
        ids = {node.player_id for node in nodes}
        links = [(a, b, c) for (a, b), c in self._links.items()
                 if c >= min_passes and a in ids and b in ids and self.players[a].team_id == team_id]
        links.sort(key=lambda link: -link[2])
        return PassNetwork(nodes=nodes, edges=[NetworkEdge(source=a, target=b, passes=c) for a, b, c in links[:max_edges]])

    def _on_ball_at(self, player_id: str, x: float, y: float) -> None:
        acc = self._on_ball.setdefault(player_id, [0.0, 0.0, 0.0])
        acc[0] += x
        acc[1] += y
        acc[2] += 1

    @property
    def score(self) -> dict[str, int]:
        return {"home": self.teams[self.home_id].goals, "away": self.teams[self.away_id].goals}

    # -------------------------------------------------------------- helpers

    def _opp(self, team_id: str) -> str:
        return self.away_id if team_id == self.home_id else self.home_id

    def _frame(self, e: Event, x: float | None, y: float | None, team_id: str | None = None):
        team_id = team_id or e.team_id
        return m.to_team_frame(x, y, team_id == self.home_id, e.period)

    def _count(self, team_id: str, key: str, value: float = 1) -> None:
        self.buckets[-1].counts[team_id][key] += value

    def _name(self, player_id: str | None) -> str:
        return self.players[player_id].short_name if player_id in self.players else "?"

    def _team_name(self, team_id: str | None) -> str:
        return self.teams[team_id].name if team_id in self.teams else "?"

    def _ready(self, key: str, minutes: float) -> bool:
        """Rate-limit a family of signals; True when the key may fire again."""
        if self._elapsed < self._cooldown.get(key, -1.0):
            return False
        self._cooldown[key] = self._elapsed + minutes * 60
        return True

    def _signal(self, type_: SignalType, e: Event, importance: float, headline: str,
                team_id: str | None = None, player_ids: list[str] | None = None,
                data: dict[str, Any] | None = None) -> None:
        signal = Signal(
            id=f"{self.info.match_id}-sig-{len(self.signals) + 1:04d}", type=type_,
            period=e.period, clock_s=e.clock_s, display_clock=e.display_clock,
            team_id=team_id, player_ids=[p for p in (player_ids or []) if p],
            importance=round(min(1.0, importance), 2), headline=headline,
            data=data or {}, event_ids=[e.id],
        )
        self.signals.append(signal)
        self._pending.append(signal)

    def _touch(self, player_id: str | None) -> None:
        if player_id in self.players:
            self.players[player_id].touches += 1

    def _ensure_possession(self, e: Event) -> _Possession:
        if self.possession is None or self.possession.team_id != e.team_id or self.possession.period != e.period:
            self.possession = _Possession(e.team_id, e.period, e.clock_s)
            self._count(e.team_id, "possessions")
        return self.possession

    def _tempo(self) -> float:
        window = self.buckets[-PHASE_WINDOW:]
        if not window:
            return 0.0
        passes = sum(b.counts[t]["passes"] for b in window for t in b.counts)
        return round(passes / len(window), 1)

    def _rating(self, p: PlayerStats) -> float:
        if p.minutes == 0 and p.touches == 0:
            return 6.0
        r = (6.0 + 1.0 * p.goals + 0.7 * p.assists + 0.5 * p.xg + 0.5 * p.xa + 0.12 * p.key_passes
             + 0.015 * (p.progressive_passes + p.progressive_carries) + 0.04 * p.dribbles_won
             + 0.06 * (p.tackles + p.interceptions) + 0.015 * p.recoveries + 0.05 * p.pass_value_added
             + 0.25 * p.saves - 0.4 * self._conceded[p.player_id]
             - 0.3 * p.yellow_cards - 1.5 * p.red_cards - 0.03 * p.fouls)
        # Soft cap so that only truly exceptional games approach 10.
        return round(6.0 + 3.5 * math.tanh((r - 6.0) / 3.5), 1)

    # ----------------------------------------------------------------- time

    def _advance_time(self, e: Event) -> None:
        elapsed = e.clock_s if e.period == 1 else self._p1_length + (e.clock_s - 2700)
        if self.last is not None and self.possession is not None and e.period == self.last.period and self.buckets:
            gap = min(elapsed - self._elapsed, POSSESSION_GAP_CAP_S)
            if gap > 0:
                self.teams[self.possession.team_id].possession_s += gap
                self._count(self.possession.team_id, "possession_s", gap)
        self._elapsed = elapsed
        minute = int(e.clock_s // 60)
        if not self.buckets or (self.buckets[-1].period, self.buckets[-1].minute) != (e.period, minute):
            if self.buckets:
                self._close_minute(e)
            self.buckets.append(_Bucket(e.period, minute, {self.home_id: Counter(), self.away_id: Counter()}))

    # ------------------------------------------------------- event handlers

    def _on_kickoff(self, e: Event) -> None:
        self.possession = None
        self._ensure_possession(e)

    def _on_possession_change(self, e: Event) -> None:
        x, _ = self._frame(e, e.x, e.y)
        self.possession = _Possession(e.team_id, e.period, e.clock_s, regain_x=x, cause=e.details["cause"])
        self._count(e.team_id, "possessions")
        self._count(e.team_id, "turnovers_won")
        if x >= m.HIGH_REGAIN_X and e.details["cause"] in ("interception", "tackle", "loose_ball"):
            self.teams[e.team_id].high_turnovers += 1
            self._count(e.team_id, "threat", THREAT["high_turnover"])

    def _on_pass(self, e: Event) -> None:
        t, p, d = self.teams[e.team_id], self.players[e.player_id], e.details
        x, y = self._frame(e, e.x, e.y)
        ex, ey = self._frame(e, e.end_x, e.end_y)
        success = e.outcome == "success"
        poss = self._ensure_possession(e)
        difficulty = m.pass_difficulty(d["length_m"], x, ex, ey, d["under_pressure"], d["height"], d["cross"])
        t.passes += 1
        p.passes += 1
        p.touches += 1
        self._difficulty_sum[e.team_id] += difficulty
        self._difficulty_sum[e.player_id] += difficulty
        p.pass_value_added += (1.0 if success else 0.0) - m.expected_completion(difficulty)
        long_ball = d["length_m"] >= m.LONG_PASS_M and not d["cross"]
        self._count(e.team_id, "passes")
        self._count(e.team_id, "long_passes", long_ball)
        if x <= 52.5:
            self._count(e.team_id, "actions_own_half")
            self._count(e.team_id, "passes_own_half")
            self._count(e.team_id, "long_passes_own_half", long_ball)
        if x <= 63:
            self._ppda_passes[self._opp(e.team_id)] += 1
        t.crosses += d["cross"]
        t.long_passes += long_ball
        if not success:
            return
        t.passes_completed += 1
        p.passes_completed += 1
        recipient = d["recipient_id"]
        self._on_ball_at(e.player_id, x, y)
        self._on_ball_at(recipient, ex, ey)
        self._links[(e.player_id, recipient)] += 1
        if m.is_progressive(x, y, ex, ey):
            t.progressive_passes += 1
            p.progressive_passes += 1
        self._entries(e, x, y, ex, ey)
        poss.passes += 1
        poss.last_pass = (e.player_id, e.clock_s)
        if poss.passes == 20 and not poss.sequence_flagged:
            poss.sequence_flagged = True
            self._signal("pass_sequence", e, 0.35,
                         f"{t.name} string together 20 consecutive passes.",
                         team_id=e.team_id, data={"passes": 20, "duration_s": round(e.clock_s - poss.start_clock)})
        if difficulty >= 85 and m.in_box(ex, ey) and self._ready(f"dsp-{e.team_id}", 8):
            recipient = d["recipient_id"]
            self._signal("defence_splitting_pass", e, 0.4,
                         f"{p.short_name} threads a pass into the box for {self._name(recipient)}.",
                         team_id=e.team_id, player_ids=[e.player_id, recipient],
                         data={"difficulty": difficulty, "length_m": d["length_m"],
                               "under_pressure": d["under_pressure"]})

    def _entries(self, e: Event, x: float, y: float, ex: float, ey: float) -> None:
        """Count final-third and box entries, at most once each per possession."""
        t, poss = self.teams[e.team_id], self._ensure_possession(e)
        if not poss.entered_final_third and x < m.FINAL_THIRD_X <= ex:
            poss.entered_final_third = True
            t.final_third_entries += 1
            self._count(e.team_id, "threat", THREAT["final_third_entry"])
        if not poss.entered_box and not m.in_box(x, y) and m.in_box(ex, ey):
            poss.entered_box = True
            t.box_entries += 1
            self._count(e.team_id, "threat", THREAT["box_entry"])

    def _on_carry(self, e: Event) -> None:
        p = self.players[e.player_id]
        self._ensure_possession(e)
        x, y = self._frame(e, e.x, e.y)
        ex, ey = self._frame(e, e.end_x, e.end_y)
        p.carries += 1
        p.touches += 1
        if x <= 52.5:
            self._count(e.team_id, "actions_own_half")
        if m.is_progressive(x, y, ex, ey):
            p.progressive_carries += 1
        self._entries(e, x, y, ex, ey)

    def _on_dribble(self, e: Event) -> None:
        p = self.players[e.player_id]
        p.dribbles += 1
        p.dribbles_won += e.outcome == "success"
        p.touches += 1

    def _on_pressure(self, e: Event) -> None:
        x, _ = self._frame(e, e.x, e.y)
        self.teams[e.team_id].pressures += 1
        self.players[e.player_id].pressures += 1
        if x >= 52.5:
            self.teams[e.team_id].high_pressures += 1
            self._count(e.team_id, "pressures_high")

    def _defensive_action(self, e: Event, stat: str) -> None:
        setattr(self.teams[e.team_id], stat, getattr(self.teams[e.team_id], stat) + 1)
        p = self.players[e.player_id]
        if hasattr(p, stat):
            setattr(p, stat, getattr(p, stat) + 1)
        self._touch(e.player_id)
        x, _ = self._frame(e, e.x, e.y)
        if x >= 42:
            self._ppda_actions[e.team_id] += 1

    def _on_tackle(self, e: Event) -> None:
        self._defensive_action(e, "tackles")

    def _on_interception(self, e: Event) -> None:
        self._defensive_action(e, "interceptions")

    def _on_ball_recovery(self, e: Event) -> None:
        self._defensive_action(e, "recoveries")

    def _on_clearance(self, e: Event) -> None:
        self._defensive_action(e, "clearances")

    def _on_block(self, e: Event) -> None:
        self._defensive_action(e, "blocks")

    def _on_shot(self, e: Event) -> None:
        t, p, d = self.teams[e.team_id], self.players[e.player_id], e.details
        x, y = self._frame(e, e.x, e.y)
        poss = self._ensure_possession(e)
        xg = m.expected_goals(x, y, d["body_part"], d["situation"])
        t.shots += 1
        p.shots += 1
        p.touches += 1
        t.xg += xg
        p.xg += xg
        self._count(e.team_id, "threat", xg)
        self._count(e.team_id, "shots")
        on_target = d["result"] in ("goal", "saved")
        t.shots_on_target += on_target
        p.shots_on_target += on_target
        if xg >= 0.3 and d["situation"] != "penalty":
            t.big_chances += 1
        self._shots.append(ShotPoint(team_id=e.team_id, player_id=e.player_id, clock=e.display_clock,
                                     x=round(x, 1), y=round(y, 1), xg=round(xg, 2), result=d["result"],
                                     body_part=d["body_part"]))
        p.fastest_shot_kmh = max(p.fastest_shot_kmh, d["speed_kmh"])

        key_passer = None
        if poss.last_pass and poss.last_pass[0] != e.player_id and e.clock_s - poss.last_pass[1] <= 10:
            key_passer = poss.last_pass[0]
            self.players[key_passer].key_passes += 1
            self.players[key_passer].xa += xg
        quick = poss.regain_x is not None and e.clock_s - poss.start_clock <= 15
        counter = quick and poss.regain_x < 52.5 and d["situation"] == "open_play"
        pressing_win = quick and poss.regain_x >= m.HIGH_REGAIN_X and poss.cause in ("interception", "tackle", "loose_ball")
        self._last_shot = dict(
            shooter=e.player_id, team_id=e.team_id, xg=round(xg, 2), speed_kmh=d["speed_kmh"],
            distance_m=d["distance_m"], situation=d["situation"], body_part=d["body_part"],
            key_passer=key_passer, counter=counter, pressing_win=pressing_win,
            build_up_passes=poss.passes, build_up_s=round(e.clock_s - poss.start_clock),
        )
        if d["result"] == "goal":
            return  # The goal signal carries the full context.
        flag_attack = (pressing_win or counter) and not poss.attack_flagged
        poss.attack_flagged = poss.attack_flagged or pressing_win or counter
        if pressing_win and flag_attack:
            self._signal("high_turnover", e, 0.45,
                         f"{_possessive(t.name)} press pays off: ball won high up and a shot within "
                         f"{self._last_shot['build_up_s']} seconds.",
                         team_id=e.team_id, player_ids=[e.player_id], data=self._last_shot)
        elif counter and flag_attack:
            self._signal("counter_attack", e, 0.45,
                         f"{t.name} break at speed from their own half; {p.short_name} gets a shot away.",
                         team_id=e.team_id, player_ids=[e.player_id], data=self._last_shot)
        if xg >= 0.3 and d["result"] in ("off_target", "blocked"):
            how = "fires wide" if d["result"] == "off_target" else "is blocked"
            self._signal("big_chance", e, 0.5 + 0.3 * xg,
                         f"Big chance! {p.short_name} {how} from {d['distance_m']:.0f} m (xG {xg:.2f}).",
                         team_id=e.team_id, player_ids=[e.player_id, key_passer], data=self._last_shot)
        if d["distance_m"] >= 20 and d["speed_kmh"] >= 105:
            self._signal("thunderbolt", e, 0.3,
                         f"{p.short_name} unleashes a {d['speed_kmh']:.0f} km/h strike from {d['distance_m']:.0f} m.",
                         team_id=e.team_id, player_ids=[e.player_id], data=self._last_shot)

    def _on_save(self, e: Event) -> None:
        self.teams[e.team_id].saves += 1
        self.players[e.player_id].saves += 1
        shot = self._last_shot
        if shot and shot["xg"] >= 0.25:
            self._signal("big_save", e, 0.5 + 0.3 * shot["xg"],
                         f"Huge save! {self._name(e.player_id)} denies {self._name(shot['shooter'])} "
                         f"(xG {shot['xg']:.2f}, {shot['speed_kmh']:.0f} km/h).",
                         team_id=e.team_id, player_ids=[e.player_id, shot["shooter"]], data=shot)

    def _on_goal(self, e: Event) -> None:
        team, opp = e.team_id, self._opp(e.team_id)
        scorer = self.players[e.player_id]
        self.teams[team].goals += 1
        scorer.goals += 1
        assist = e.details.get("assist_id")
        if assist:
            self.players[assist].assists += 1
        for p in self.players.values():
            if p.team_id == opp and p.on_pitch and p.position == "GK":
                self._conceded[p.player_id] += 1
        tg, og = self.teams[team].goals, self.teams[opp].goals
        if tg + og == 1:
            context = "opener"
        elif tg == og:
            context = "equaliser"
        elif tg == og + 1:
            context = "go_ahead"
        elif tg > og:
            context = "lead_extended"
        else:
            context = "pulls_one_back"
        comeback = self._max_deficit[team] >= 2 and tg >= og
        for tid in (team, opp):
            self._max_deficit[tid] = max(self._max_deficit[tid], self.teams[self._opp(tid)].goals - self.teams[tid].goals)
        late = e.period == 2 and e.clock_s >= 85 * 60
        shot = self._last_shot if self._last_shot and self._last_shot["shooter"] == e.player_id else {}
        home, away = self.teams[self.home_id], self.teams[self.away_id]
        tag = {2: " It's a brace!", 3: " Hat-trick!"}.get(scorer.goals, "")
        self._signal(
            "goal", e, 1.0,
            f"GOAL! {scorer.short_name} scores for {self.teams[team].name}.{tag} "
            f"{home.name} {home.goals}-{away.goals} {away.name}.",
            team_id=team, player_ids=[e.player_id, assist],
            data={**shot, "context": context, "comeback": comeback, "late": late,
                  "scorer_goals": scorer.goals, "assist_id": assist, "score": self.score},
        )

    def _on_foul(self, e: Event) -> None:
        self.teams[e.team_id].fouls += 1
        self.players[e.player_id].fouls += 1
        self._count(e.team_id, "fouls")
        x, _ = self._frame(e, e.x, e.y)
        if x >= 42:
            self._ppda_actions[e.team_id] += 1

    def _on_card(self, e: Event) -> None:
        card, p, t = e.details["card"], self.players[e.player_id], self.teams[e.team_id]
        if card == "yellow":
            t.yellow_cards += 1
            p.yellow_cards += 1
            self._signal("card", e, 0.25, f"{p.short_name} ({t.short_name}) is booked.",
                         team_id=e.team_id, player_ids=[e.player_id], data={"card": card})
            return
        t.red_cards += 1
        p.red_cards += 1
        self._leave_pitch(p.player_id)
        reason = "a second yellow card" if card == "second_yellow" else "a straight red card"
        self._signal("card", e, 0.85, f"{p.short_name} is sent off with {reason}! {t.name} are down to ten.",
                     team_id=e.team_id, player_ids=[e.player_id], data={"card": card})

    def _on_penalty_awarded(self, e: Event) -> None:
        self._signal("penalty", e, 0.7, f"Penalty to {self._team_name(e.team_id)}! {self._name(e.player_id)} is brought down.",
                     team_id=e.team_id, player_ids=[e.player_id])

    def _leave_pitch(self, player_id: str) -> None:
        p = self.players[player_id]
        if p.on_pitch:
            self._played_s[player_id] += self._elapsed - self._on_since[player_id]
            p.on_pitch = False

    def _on_substitution(self, e: Event) -> None:
        incoming = e.details["player_in_id"]
        self._leave_pitch(e.player_id)
        self.players[incoming].on_pitch = True
        self._on_since[incoming] = self._elapsed
        self._signal("substitution", e, 0.15,
                     f"{self._name(incoming)} replaces {self._name(e.player_id)} for {self._team_name(e.team_id)}.",
                     team_id=e.team_id, player_ids=[incoming, e.player_id])

    def _on_corner(self, e: Event) -> None:
        self.teams[e.team_id].corners += 1
        self._count(e.team_id, "threat", THREAT["corner"])

    def _on_offside(self, e: Event) -> None:
        self.teams[e.team_id].offsides += 1

    def _on_physical_update(self, e: Event) -> None:
        for row in e.details["players"]:
            p = self.players[row["player_id"]]
            previous_km = p.distance_km
            p.distance_km = round(row["distance_m"] / 1000, 2)
            p.top_speed_kmh = row["top_speed_kmh"]
            p.sprints = row["sprints"]
            if p.top_speed_kmh >= SPEED_ALERT_KMH and p.player_id not in self._speed_alerted:
                self._speed_alerted.add(p.player_id)
                record = p.top_speed_kmh > self._speed_record
                self._speed_record = max(self._speed_record, p.top_speed_kmh)
                self._signal("speed_alert", e, 0.35 if record else 0.25,
                             f"{p.short_name} hits {p.top_speed_kmh:.1f} km/h"
                             + (", the fastest sprint of the match." if record else "."),
                             team_id=p.team_id, player_ids=[p.player_id],
                             data={"top_speed_kmh": p.top_speed_kmh, "match_record": record})
            if previous_km < DISTANCE_MILESTONE_KM <= p.distance_km and p.team_id not in self._distance_flagged:
                self._distance_flagged.add(p.team_id)
                self._signal("distance_milestone", e, 0.2,
                             f"{p.short_name} is the first {self.teams[p.team_id].name} player "
                             f"to pass {DISTANCE_MILESTONE_KM:.0f} km covered.",
                             team_id=p.team_id, player_ids=[p.player_id],
                             data={"distance_km": p.distance_km, "sprints": p.sprints})

    def _on_added_time(self, e: Event) -> None:
        self._signal("added_time", e, 0.1, f"{e.details['minutes']} minutes of added time.",
                     data={"minutes": e.details["minutes"]})

    def _on_period_end(self, e: Event) -> None:
        if e.period == 1:
            self._p1_length = self._elapsed
            snap = self.snapshot()
            self._signal("half_time", e, 0.6, f"Half-time: {self._score_line()}.", data=self._summary(snap))

    def _on_match_end(self, e: Event) -> None:
        snap = self.snapshot()
        best = max(snap.players.values(), key=lambda p: p.rating)
        self._signal("full_time", e, 1.0, f"Full-time: {self._score_line()}. Player of the match: {best.short_name}.",
                     player_ids=[best.player_id], data={**self._summary(snap), "player_of_the_match": best.player_id})

    def _score_line(self) -> str:
        home, away = self.teams[self.home_id], self.teams[self.away_id]
        return f"{home.name} {home.goals}-{away.goals} {away.name}"

    def _summary(self, snap: Snapshot) -> dict[str, Any]:
        keys = ("goals", "possession_pct", "shots", "shots_on_target", "xg", "passes", "pass_accuracy", "ppda")
        return {"score": snap.score, "teams": {tid: {k: getattr(t, k) for k in keys} for tid, t in snap.teams.items()}}

    # ------------------------------------------------------ minute analysis

    def _close_minute(self, e: Event) -> None:
        """Run the rolling-window analysis on completed minutes."""
        self._update_momentum(e)
        self._update_phase(e)
        if len(self.buckets) >= SHIFT_MIN_AFTER + SHIFT_MIN_BEFORE:
            for team_id in (self.home_id, self.away_id):
                self._detect_shifts(e, team_id)

    def _update_momentum(self, e: Event) -> None:
        window = self.buckets[-MOMENTUM_WINDOW:]
        weights = [0.75 ** k for k in range(len(window))][::-1]
        diff = sum(w * (b.counts[self.home_id]["threat"] - b.counts[self.away_id]["threat"])
                   for w, b in zip(weights, window))
        value = round(100 * math.tanh(diff / sum(weights) / MOMENTUM_SCALE), 1)
        closed = self.buckets[-1]
        self.momentum_series.append(MomentumPoint(period=closed.period, minute=closed.minute + 1, value=value))

        recent = [pt.value for pt in self.momentum_series[-7:-1]]
        if abs(value) >= 50 and any(v * value < 0 and abs(v) >= 50 for v in recent) and self._ready("swing", 10):
            team = self.home_id if value > 0 else self.away_id
            self._signal("momentum_swing", e, 0.5, f"Momentum swings towards {self._team_name(team)}.",
                         team_id=team, data={"momentum": value, "previous": recent})
        last5 = [pt.value for pt in self.momentum_series[-5:]]
        if len(last5) == 5 and (all(v >= 55 for v in last5) or all(v <= -55 for v in last5)):
            team = self.home_id if value > 0 else self.away_id
            if self._ready(f"dominance-{team}", 15):
                window_stats = self._window_stats(team, 5)
                self._signal("dominance", e, 0.4,
                             f"{self._team_name(team)} are dominating: {_plural(window_stats['shots'], 'shot')} and "
                             f"{window_stats['possession_pct']:.0f}% possession in the last 5 minutes.",
                             team_id=team, data={"momentum": value, **window_stats})

    def _window_stats(self, team_id: str, minutes: int) -> dict[str, Any]:
        window = self.buckets[-minutes:]
        own = sum((b.counts[team_id] for b in window), Counter())
        opp = sum((b.counts[self._opp(team_id)] for b in window), Counter())
        total = own["possession_s"] + opp["possession_s"]
        return {"shots": int(own["shots"]), "passes": int(own["passes"]),
                "possession_pct": round(100 * own["possession_s"] / total, 1) if total else 50.0}

    def _update_phase(self, e: Event) -> None:
        window = self.buckets[-PHASE_WINDOW:]
        if len(window) < 3:
            return
        n = len(window)
        both = sum((b.counts[t] for b in window for t in b.counts), Counter())
        turnovers = both["turnovers_won"] / n
        passes_per_poss = both["passes"] / max(1, both["possessions"])
        long_share = both["long_passes"] / max(1, both["passes"])
        fouls = both["fouls"] / n
        z = 0.5 * turnovers / 2.5 + 0.3 * long_share / 0.13 + 0.2 * fouls / 0.23
        chaos = round(max(0.0, min(100.0, 50 * z)), 1)
        shares = {t: self._window_stats(t, n)["possession_pct"] for t in (self.home_id, self.away_id)}
        leader = max(shares, key=shares.get)
        if chaos >= 65:
            phase, team = "chaos", None
        elif shares[leader] >= 60 and chaos <= 55:
            phase, team = "control", leader
        else:
            phase, team = "balanced", None
        previous = self.phase
        self.phase = GamePhase(phase=phase, team_id=team, chaos_index=chaos,
                               turnovers_per_min=round(turnovers, 2),
                               passes_per_possession=round(passes_per_poss, 1),
                               long_ball_share=round(long_share, 3))
        if previous is None or (previous.phase, previous.team_id) == (phase, team) or phase == "balanced":
            return
        if not self._ready("phase", 12):
            return
        if phase == "chaos":
            headline = (f"The game turns chaotic: {turnovers:.1f} turnovers a minute and "
                        f"{long_share:.0%} long balls over the last {n} minutes.")
        else:
            headline = (f"{self._team_name(team)} take control: {shares[team]:.0f}% possession and "
                        f"{passes_per_poss:.1f} passes per possession over the last {n} minutes.")
        self._signal("phase_change", e, 0.35, headline, team_id=team, data=self.phase.model_dump())

    def _detect_shifts(self, e: Event, team_id: str) -> None:
        """Look for a lasting change in a team's behaviour.

        For each metric, every split point between SHIFT_MIN_AFTER and SHIFT_MAX_AFTER
        minutes ago is tested: the rate after the split is compared with the rate over
        the SHIFT_BEFORE minutes before it, and the strongest significant split wins.
        """
        opp = self._opp(team_id)
        # Passes per possession is too heavy-tailed to test reliably; the control-vs-chaos
        # phase covers it instead.
        families = [
            ("pressing", "pressures_high", team_id, "actions_own_half", opp,
             "share of opponent build-up actions pressed in their own half"),
            ("directness", "long_passes_own_half", team_id, "passes_own_half", team_id,
             "share of long balls played from their own half"),
        ]
        n = len(self.buckets)
        for family, num_key, num_team, exp_key, exp_team, label in families:
            key = f"shift-{team_id}-{family}"
            if self._elapsed < self._cooldown.get(key, -1.0):
                continue
            series = [(b.counts[num_team][num_key], b.counts[exp_team][exp_key]) for b in self.buckets]
            best = None
            for after_len in range(SHIFT_MIN_AFTER, min(SHIFT_MAX_AFTER, n - SHIFT_MIN_BEFORE) + 1):
                split = n - after_len
                before, after = series[max(0, split - SHIFT_BEFORE):split], series[split:]
                k0, n0 = sum(k for k, _ in before), sum(x for _, x in before)
                k1, n1 = sum(k for k, _ in after), sum(x for _, x in after)
                if n0 < 20 or n1 < 15 or k0 + k1 < 10 or k0 == 0:
                    continue
                r0, r1 = k0 / n0, k1 / n1
                pooled = (k0 + k1) / (n0 + n1)
                dispersion = _dispersion(before)
                se = math.sqrt(dispersion * max(pooled * (1 - pooled), 1e-9) * (1 / n0 + 1 / n1))
                z = (r1 - r0) / se if se else 0.0
                if abs(z) >= SHIFT_MIN_Z and abs(r1 - r0) / r0 >= SHIFT_MIN_CHANGE:
                    if best is None or abs(z) > abs(best[0]):
                        best = (z, r0, r1, split)
            if best is None:
                continue
            z, r0, r1, split = best
            self._cooldown[key] = self._elapsed + SHIFT_COOLDOWN_MIN * 60
            kind = _SHIFT_KINDS[family][0 if r1 > r0 else 1]
            since = self.buckets[split].minute + 1
            self._signal(
                "tactical_shift", e, 0.55,
                f"Since the {_ordinal(since)} minute, {self._team_name(team_id)} {_SHIFT_PHRASES[kind]}: "
                f"{label} went from {r0:.0%} to {r1:.0%}.",
                team_id=team_id,
                data={"kind": kind, "family": family, "metric": label, "since_minute": since,
                      "before": round(r0, 3), "after": round(r1, 3), "z": round(z, 2)},
            )
