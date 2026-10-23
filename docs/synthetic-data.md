# Synthetic match data

All match data in this project is synthetic. Clubs, players and the league are invented;
no real football data or footage is used.

## Generate and replay a match

```bash
python -m itg.synthetic.generate --home expected-ls --away press-gang --seed 42
python -m itg.synthetic.replay data/matches/exl-prg-42.json --speed 60
python -m itg.synthetic.replay data/matches/exl-prg-42.json --speed 1e9 --only goal,card,substitution
```

The same seed always produces the same match. `python -m scripts.calibrate` compares
average statistics over many matches with real top-flight averages.

## Clubs

| Club | Formation | Identity |
|---|---|---|
| Expected Ls (EXL) | 4-3-3 | Patient possession, moderate pressing |
| Press Gang (PRG) | 4-2-3-1 | Aggressive high press |
| Carb Loaders (CRB) | 4-4-2 | Direct, long balls |
| Benchwarmers FC (BEN) | 4-2-3-1 | Balanced |

Each club has 18 players with ratings (pace, passing, dribbling, finishing, defending,
goalkeeping, stamina) generated from a fixed seed.

## How the simulation works

The simulator follows the ball. The player in possession passes, carries, dribbles or
shoots; the defending team presses, tackles, intercepts and blocks. Outcomes depend on
player ratings, team style, fatigue and a hidden momentum that drifts minute by minute.
Late in the match, trailing teams push forward and leading teams sit deeper.

Each match also contains one or two hidden **tactical shifts** (high press, low block,
direct play, possession control) starting at a random minute. They are never streamed,
but they are saved in `ground_truth` so the analysis agents can be scored on whether they
detect them.

## File format

A match file is a JSON document (`itg/schema.py` defines it):

- `info`: match id, competition, kickoff, venue, weather, referee, lineups, seed.
- `teams`: both squads with player ratings and team style.
- `events`: the ordered event stream (about 2,500 per match).
- `final_score`: `{"home": 2, "away": 1}`.
- `ground_truth`: hidden tactical shifts and the per-minute momentum of each team.

### Events

| Field | Meaning |
|---|---|
| `seq`, `id` | Order in the stream and unique id |
| `period`, `clock_s` | Half (1 or 2) and match clock in seconds (second half starts at 2700) |
| `display_clock` | Broadcast minute, e.g. `23'` or `45+2'` |
| `type` | See below |
| `team_id`, `player_id` | Acting team and player |
| `x`, `y`, `end_x`, `end_y` | Metres on a 105 x 68 pitch; home attacks towards x = 105 in the first half |
| `outcome` | `success` or `fail` when relevant |
| `details` | Type-specific data |

Event types and their main `details`:

| Type | Details |
|---|---|
| `pass` | `recipient_id`, `length_m`, `ball_speed_kmh`, `height`, `cross`, `under_pressure`, `offside` |
| `carry` | `distance_m`, `speed_kmh` |
| `dribble`, `tackle` | `opponent_id` |
| `pressure` | `target_player_id`, `distance_m` |
| `shot` | `result` (goal, saved, blocked, off_target), `situation`, `body_part`, `distance_m`, `speed_kmh`, `keeper_id` |
| `goal` | `assist_id`, `situation`, `score` |
| `possession_change` | `cause`, `from_team_id` |
| `foul`, `card` | `fouled_player_id`; `card` (yellow, second_yellow, red) |
| `substitution` | `player_in_id`, `role` (`player_id` is the player going off) |
| `physical_update` | Every minute, per player: `distance_m`, `top_speed_kmh`, `sprints` (≥ 25.2 km/h) |
| `interception`, `ball_recovery`, `clearance`, `block`, `save` | Defensive actions |
| `kickoff`, `free_kick`, `corner`, `throw_in`, `goal_kick`, `penalty_awarded`, `offside` | Restarts |
| `period_start`, `added_time`, `period_end`, `match_end` | Match flow |
