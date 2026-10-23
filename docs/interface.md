# Interface and API (Render)

```bash
uvicorn itg.api.app:app --port 8000
# then open http://localhost:8000
```

## The broadcast companion

- **Scorebug**: score, broadcast clock and the live game phase (control, chaos, balanced).
- **Pitch**: ball position, pass and shot lines in club colours, goal flashes, the name of the
  player on the ball, and a ring around the followed player in player-focus mode.
- **Overlay**: lower-third cards written by the agents for the selected viewer, timed with the match.
  High-priority cards (goals, red cards, penalties) jump the queue; stale cards are dropped
  at high playback speeds.
- **Key moments**: the viewer's feed. Analysts also see why it matters, the analyst note and the
  commentary line.
- **Viewer and language**: four viewers (studio analyst, home fan, away fan, player focus) and,
  separately, the language (English, French, Spanish, Portuguese). Every combination is its own
  experience, written by the agents, and the whole interface switches language too. With live
  agents only the languages in use are written.
- **Follow a player**: any player of either team. A ring follows them on the pitch, their live
  stats are shown, and every 15 minutes the Player Analyst writes a check-in on them, shown in the
  feed and as an overlay. Recorded runs contain check-ins for every player; live sessions start
  them for a player from the next quarter-hour.
- **Win probability**: a bar under the scorebug and a chart over time, from simulating the rest of
  the match every minute; key moments show how much they changed it (see `forecast.md`).
- **Shots** and **passing network** panels, updated live.
- **Ask the analyst**: questions in any of the four languages, including "what if?" questions that
  replay the rest of the match. When a picture helps, the analyst puts a broadcast graphic on the
  pitch (shot map, passing network, a player's touches, a pressing map, a what-if comparison).
- **The stands** (watch rooms): "Invite friends" shares a link to the same match, in sync. Fans back
  a team and cheer (the battle of the stands), predict the result (the crowd against the AI
  forecast), propose substitutions or tactical changes that the others vote on (each one simulated
  with its effect on the team's chances), and rate the match at the end. The host controls playback.
- **What you missed**: a fan who joins late or comes back to the tab after a few minutes gets a
  catch-up written for their profile and language.
- **Listen**: spoken commentary of the overlays, recaps and answers in the fan's language with
  Azure AI Speech neural voices (the browser's voice is the fallback).
- **Match stats**, **momentum** chart and **player card** for the followed player.
- **How it's produced** (collapsed by default): the agent pipeline, how many calls were accepted,
  corrected by the Verifier or replaced by a template, and the latest moments' status.

The design is deliberately quiet: light background, one accent colour plus the club colours,
Barlow Condensed for the broadcast elements and Inter for text.

Two sources: **Live on Foundry** runs the agents as the match plays. **Recorded run**
replays a saved run perfectly in sync, without calling Foundry (`python -m itg.agents.run` records one).
A new match has no recorded run, so it always plays live. Live agents need about 7 seconds per
moment, so the interface switches to 10× (about one match minute of delay); live overlays are
only dropped if they have waited more than 20 seconds on screen time, recorded ones when they are
more than 150 match seconds old.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Server status and whether Foundry is connected |
| `GET /api/clubs` | Fictional clubs, styles and squads |
| `GET /api/matches` | Available matches and whether a recorded agent run exists |
| `POST /api/matches` | Generate a new synthetic match `{home, away, seed}` |
| `GET /api/matches/{id}/overlays?feed=` | Overlay cue sheet of a recorded run |
| `GET /api/rooms/{code}` | A watch room's match and number of fans |
| `GET /api/speech?text=&lang=&excited=` | Spoken version of a text (MP3, Azure AI Speech) |
| `WS /ws/matches/{id}?source=live\|cached&speed=&languages=en,fr&room=CODE&observer=1` | Join (or open) a watch room; `observer` follows it without taking part |
| `GET /broadcast.html?room=CODE&lang=&viewer=&player=` | Broadcast overlay for OBS (below) |
| `POST /mcp` | MCP server for GitHub Copilot, see [mcp.md](mcp.md) |

### WebSocket messages

Server to browser: `room`, `match`, `status`, `event`, `snapshot` (once a second), `signal`,
`moment` (agent output plus one cue per viewer), `recap`, `player_report`, `forecast`, `social`,
`catch_up`, `answer` (with an optional graphic), `profiles`, `notice`, `error`.

Browser to server: `pause`, `resume`, `speed` (host only), `ask`, `languages`, `follow`, `catch_up`,
`support`, `cheer`, `predict`, `propose`, `vote`, `rate`.

### Overlay cues for a graphics engine

Each cue is self-contained and timed in match time, so a rendering partner can synchronise it
with the video feed:

```json
{
  "cue_id": "exl-prg-42-sig-0022:home-fan.fr",
  "feed": "home-fan.fr",
  "language": "fr",
  "period": 2,
  "match_clock_s": 2775.3,
  "display_clock": "47'",
  "duration_s": 12,
  "template": "lower_third_high",
  "title": "But de Press Gang",
  "body": "…",
  "stat_chips": [{"label": "xG", "value": "0.09"}],
  "highlight": {"player_id": "prg-16-visser", "team_id": "press-gang"},
  "moment_type": "goal",
  "commentary": "…"
}
```

### Broadcast overlay for OBS

`broadcast.html` shows only the TV graphics on a transparent page: scorebug with the clock,
the win-probability bar, the lower thirds written for one viewer, the half-time and full-time
recaps and, with `player=`, the followed player's check-ins. A streamer or a club channel adds
it to **OBS Studio** as a *Browser* source (1920 × 1080) on top of the match video.

| Parameter | Meaning |
|---|---|
| `room=CODE` | Follow a watch room: the graphics play in sync with it. The page joins as an *observer*, so it is not counted as a fan and never becomes the host |
| `match=ID&source=cached&speed=1` | Or play a match on its own |
| `lang=en\|fr\|es\|pt` | Language of the graphics |
| `viewer=analyst\|home-fan\|away-fan\|player-focus` | Whose lower thirds to show |
| `player=ID` | Also show this player's check-ins |
| `preview=1` | Draw the pitch behind the graphics, to check them in a normal browser |

The **OBS overlay** link above the key moments opens the overlay of the current room, language,
viewer and followed player in preview mode. In a live room, the overlay asks the agents for its
language and player if nobody in the room has yet.

### 3D stadium

`stadium.html?match=exl-prg-42&speed=5&lang=en&viewer=analyst` plays a match in a 3D stadium
(three.js): stands with an animated crowd in the club colours, floodlights, advertising boards,
both teams and the referee. The players' positions come from `GET /api/matches/{id}/tracking`
(`itg/tracking.py`), rebuilt from the event stream after the simulation: the player on the ball
is where the event happened, the receiver where the pass arrives, everyone else at their formation
slot shifted with the ball and possession, and runs start early so that nobody moves faster than a
sprint. Matches and recorded runs are untouched.

Cameras: *Auto director* (TV view, cuts behind the goal on shots and corners, a replay camera
circling the scorer after a goal, a wide view at kick-off), *TV*, *Tactical*, *Behind the goal*,
*Follow* and *Free* (orbit with the mouse). The lower thirds of the recorded agent run appear at
their match time; the goal buttons jump to each goal, and *Sound* adds a crowd that roars on
shots and goals.
