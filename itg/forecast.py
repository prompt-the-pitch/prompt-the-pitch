"""Win probabilities and what-if scenarios, by simulating the rest of the match many times.

Because the match comes from our own simulator, the future can be replayed: from the current
state (score, time left, players on the pitch, observed tactics, momentum) the simulator plays
the remaining minutes a few hundred times. Scenarios ("what if they had scored?", "what if they
pressed higher?") change the state and replay with the same random seeds, so the difference
between the two forecasts comes from the scenario, not from luck.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from itg.schema import Match, Team, TeamStyle
from itg.stats.engine import StatsEngine
from itg.synthetic.simulator import TACTIC_CHANGES, MatchSimulator, SimState

DEFAULT_RUNS = 150
# Tactical changes detected by the stats engine, mapped to the simulator's tactics.
DETECTED_TACTICS = {"pressing_up": "high_press", "pressing_down": "low_block",
                    "more_direct": "go_direct", "less_direct": "possession_control"}
Tactic = Literal["high_press", "low_block", "go_direct", "possession_control"]


class Forecast(BaseModel):
    home: float = Field(description="Probability that the home team wins.")
    draw: float
    away: float
    home_goals: float = Field(description="Expected final goals of the home team.")
    away_goals: float
    runs: int


class ForecastPoint(BaseModel):
    """Win probabilities at one minute of the match."""

    period: int
    minute: int
    clock_s: float
    clock: str
    home: float
    draw: float
    away: float


class Scenario(BaseModel):
    """Changes applied to the current state before replaying the rest of the match."""

    home_goals: int = Field(0, description="Goals added to (or, if negative, removed from) the home team.")
    away_goals: int = 0
    home_red_cards: int = Field(0, ge=0, le=3)
    away_red_cards: int = Field(0, ge=0, le=3)
    home_tactic: Tactic | None = None
    away_tactic: Tactic | None = None
    substitutions: list[tuple[str, str]] = Field(default_factory=list, description="(player off id, player on id)")
    reinstate: list[str] = Field(default_factory=list, description="Sent-off player ids put back on the pitch.")

    def is_empty(self) -> bool:
        return self == Scenario()


def with_tactic(style: TeamStyle, tactic: str) -> TeamStyle:
    changed = style.model_copy()
    for key, delta in TACTIC_CHANGES[tactic][1].items():
        setattr(changed, key, min(0.95, max(0.05, getattr(changed, key) + delta)))
    return changed


def forecast(home: Team, away: Team, state: SimState, runs: int = DEFAULT_RUNS, seed: int = 0) -> Forecast:
    results = [MatchSimulator(home, away, seed + i).run_from(state) for i in range(runs)]
    return Forecast(
        home=round(sum(h > a for h, a in results) / runs, 3),
        draw=round(sum(h == a for h, a in results) / runs, 3),
        away=round(sum(h < a for h, a in results) / runs, 3),
        home_goals=round(sum(h for h, _ in results) / runs, 2),
        away_goals=round(sum(a for _, a in results) / runs, 2),
        runs=runs,
    )


def state_from_engine(engine: StatsEngine, match: Match) -> SimState:
    """What the stats engine knows right now, as a simulator state."""
    home, away = match.teams
    last = engine.last
    period = last.period if last else 1
    clock = (last.clock_s - (2700 if period == 2 else 0)) if last else 0.0
    styles = {home.id: home.style.model_copy(), away.id: away.style.model_copy()}
    for signal in engine.signals:
        tactic = DETECTED_TACTICS.get(signal.data.get("kind", "")) if signal.type == "tactical_shift" else None
        if tactic and signal.team_id in styles:
            styles[signal.team_id] = with_tactic(styles[signal.team_id], tactic)
    on_pitch = {tid: [p.player_id for p in engine.players.values() if p.team_id == tid and p.on_pitch]
                for tid in styles}
    momentum = engine.momentum_series[-1].value / 250 if engine.momentum_series else 0.0
    return SimState(
        period=period, clock_s=clock,
        home_goals=engine.score["home"], away_goals=engine.score["away"],
        home_style=styles[home.id], away_style=styles[away.id],
        home_on_pitch=on_pitch[home.id], away_on_pitch=on_pitch[away.id],
        home_momentum=momentum, away_momentum=-momentum,
        home_in_possession=engine.possession is None or engine.possession.team_id == home.id,
    )


def state_at(match: Match, period: int, clock_s: float) -> tuple[StatsEngine, SimState]:
    """Replay the stats engine up to a past moment and return its state then."""
    engine = StatsEngine.for_match(match)
    for event in match.events:
        if (event.period, event.clock_s) > (period, clock_s):
            break
        engine.ingest(event)
    return engine, state_from_engine(engine, match)


def apply_scenario(state: SimState, scenario: Scenario, match: Match) -> SimState:
    home, away = match.teams
    changed = SimState(**{**state.__dict__})
    changed.home_goals = max(0, state.home_goals + scenario.home_goals)
    changed.away_goals = max(0, state.away_goals + scenario.away_goals)
    for side, team in (("home", home), ("away", away)):
        tactic = getattr(scenario, f"{side}_tactic")
        style = getattr(state, f"{side}_style") or team.style
        if tactic:
            setattr(changed, f"{side}_style", with_tactic(style, tactic))
        lineup = list(getattr(state, f"{side}_on_pitch") or [p.id for p in team.players][:11])
        squad = {p.id: p for p in team.players}
        for off, on in scenario.substitutions:
            if off in lineup and on in squad and on not in lineup:
                lineup[lineup.index(off)] = on
        lineup += [pid for pid in scenario.reinstate if pid in squad and pid not in lineup]
        for _ in range(getattr(scenario, f"{side}_red_cards")):
            outfield = [pid for pid in lineup if squad[pid].position != "GK"]
            if len(outfield) > 6:
                # A midfielder is usually sacrificed to keep the defensive shape.
                lineup.remove(next((pid for pid in outfield if squad[pid].position == "MF"), outfield[-1]))
        setattr(changed, f"{side}_on_pitch", lineup)
    return changed


def compare(match: Match, state: SimState, scenario: Scenario, runs: int = DEFAULT_RUNS,
            seed: int = 0) -> tuple[Forecast, Forecast]:
    """(baseline, scenario) forecasts with the same random seeds."""
    home, away = match.teams
    baseline = forecast(home, away, state, runs, seed)
    return baseline, forecast(home, away, apply_scenario(state, scenario, match), runs, seed)
