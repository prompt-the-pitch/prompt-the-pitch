import math

from itg.synthetic.clubs import CLUBS
from itg.synthetic.simulator import simulate_match
from itg.tracking import build_tracking


def test_tracking_follows_the_ball_at_human_speeds():
    match = simulate_match(CLUBS["expected-ls"], CLUBS["press-gang"], 5)
    tracking = build_tracking(match)
    frames = tracking["frames"]
    assert len(tracking["ids"]) == 36
    times = [f[0] for f in frames]
    assert times == sorted(times)
    for f in frames:
        assert sum(v is not None for v in f[4]) <= 44  # At most 22 players on the pitch.
        assert -3 <= f[1] <= 108 and -3 <= f[2] <= 71
    speeds = []
    for a, b in zip(frames, frames[1:]):
        dt = b[0] - a[0]
        if dt <= 0.05:
            continue
        for i in range(0, len(a[4]), 2):
            if a[4][i] is not None and b[4][i] is not None:
                speeds.append(math.hypot(b[4][i] - a[4][i], b[4][i + 1] - a[4][i + 1]) / dt)
    speeds.sort()
    assert speeds[int(len(speeds) * 0.99)] < 10  # A sprint is about 9-10 m/s.
    goals = [e for e in tracking["events"] if e["type"] == "goal"]
    assert len(goals) == sum(match.final_score.values())
