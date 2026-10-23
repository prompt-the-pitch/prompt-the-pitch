# Win probabilities and "what if?"

Because the matches come from our own simulator, the future can be replayed. This is what real,
recorded match data cannot offer.

## Live win probability

Every minute the orchestrator turns what the stats engine knows into a simulator state (score,
time left, players on the pitch, red cards, momentum, and the tactical changes the engine has
detected) and plays the rest of the match 120 times (`itg/forecast.py`). The share of home wins,
draws and away wins is the win probability shown under the scorebug and in the chart.

The forecast only uses what has been observed: the hidden tactical changes of the simulation are
never given to it.

## How much a moment mattered

For goals, big chances, big saves and red cards, the moment is replayed both ways with the same
random seeds:

| Moment | Compared with |
|---|---|
| Goal | The same match without this goal |
| Big chance or big save | The same match had the chance been scored |
| Red card | The same match with eleven players |

The difference ("this goal lifted Press Gang's chances from 38% to 89%") is added to the moment's
evidence, so the Tactical Explainer and the Personalizers quantify why it mattered.

## What if?

The analyst chat has a `simulate_what_if` tool: a viewer asks "what if they had scored at 16'?",
"what if Press Gang sat deep now?" or "what if Teixeira came on?", the agent turns the question
into a scenario (goals, red cards, tactic, substitution, from now or from a past minute) and the
simulator replays the rest of the match 200 times with and without it.

Fans can also propose a substitution or a tactical change in the stands; every proposal is
simulated the same way and shown with its effect on the team's chances.

## Checks

`tests/test_forecast.py`: the score never goes down, probabilities add up to one, a late lead
holds, a goal raises a team's chances, a red card lowers them, substitutions and red cards
change the lineup, and the orchestrator measures a positive impact for goals.
