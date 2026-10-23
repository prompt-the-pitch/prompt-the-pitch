# Stats engine (Interpret)

`itg/stats/engine.py` turns the raw event stream into live football intelligence. It is
deterministic and does not use AI: numbers must be exact and instant, and the agents
explain them afterwards.

```python
engine = StatsEngine.for_match(match)
for event in stream:
    signals = engine.ingest(event)   # key-moment candidates raised by this event
snapshot = engine.snapshot()         # live team and player stats, momentum, phase
```

```bash
python -m itg.stats.run data/matches/exl-prg-42.json --min-importance 0.45
python -m itg.stats.run data/matches/exl-prg-42.json --at-minute 60
```

## Metrics

| Metric | Definition |
|---|---|
| Possession % | Share of in-play time with the ball (gaps over 6 s count as dead ball) |
| xG | Logistic model of distance and goal angle, lower for headers and direct free kicks; penalties 0.76 |
| Pass difficulty | 0-100 from length, pressure, height, cross, forward distance and destination (final third, box) |
| Pass value added | Completed passes minus the completion expected from their difficulty |
| Progressive pass/carry | Gains 30 m in the own half, 15 m across halfway, or 10 m in the opponent's half |
| Final-third / box entries | Counted at most once per possession |
| High turnover | Ball won within 40 m of the opponent's goal |
| PPDA | Opponent passes in their own 60% per defensive action there (lower = more intense press) |
| Speed and distance | From the per-minute physical feed: distance, top speed, sprints (≥ 25.2 km/h) |
| Player rating | 6.0 baseline plus goals, assists, xG, xA, progression, defending and pass value, minus cards; soft-capped below 10 |

## Rolling analysis (every minute)

- **Momentum** (-100 to +100): weighted difference in threat (xG, entries, corners, high
  turnovers) over the last 5 minutes.
- **Control vs chaos**: a chaos index from turnovers per minute, long-ball share and fouls,
  plus possession share. The phase is `control` (one team, at least 60% of the ball, calm
  game), `chaos` (index of 65 or more) or `balanced`.
- **Tactical shifts**: a change-point scan over pressing height and long-ball share. Each
  split between 8 and 20 minutes ago is compared with the 25 minutes before it, using a
  two-proportion z-test corrected for the natural minute-to-minute overdispersion.

## Signals

Each signal has a type, the match clock, the team and players involved, an importance
from 0 to 1, a factual headline and the evidence behind it in `data`:

`goal` (with context: opener, equaliser, go-ahead, comeback, late, brace),
`big_chance`, `big_save`, `penalty`, `card`, `substitution`, `thunderbolt`,
`defence_splitting_pass`, `pass_sequence`, `high_turnover`, `counter_attack`,
`speed_alert`, `distance_milestone`, `momentum_swing`, `dominance`, `phase_change`,
`tactical_shift`, `added_time`, `half_time`, `full_time`.

A typical match produces about 50 signals, around 20 of them with importance 0.45 or higher.

## Checked against the hidden ground truth

The simulator hides one or two tactical changes per match. `python -m scripts.evaluate_detection`
measures how many the engine finds (same team, matching behaviour, within 15 minutes).
On 60 matches:

| Hidden change | Detected |
|---|---|
| High press | 15 / 20 |
| Low block | 20 / 35 |
| Direct play | 5 / 22 |
| Possession control | 2 / 19 |

Overall recall is 44%, with 1.2 detections per match that match no hidden change. Some of
those are real behaviour changes caused by fatigue, red cards or the scoreline. Pressing
changes are found reliably. Changes in passing style are harder to prove: a team plays only
about two passes a minute from its own half, which is too little data in 15 minutes.
