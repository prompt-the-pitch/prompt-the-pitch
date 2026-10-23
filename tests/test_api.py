import asyncio
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from itg.agents.briefs import default_profiles
from itg.agents.llm import LLM
from itg.agents.orchestrator import MatchOrchestrator
from itg.agents.team import AgentTeam
from itg.api.overlays import cue_sheet
from itg.synthetic.clubs import CLUBS
from itg.synthetic.simulator import simulate_match


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    mp = pytest.MonkeyPatch()
    mp.setenv("ITG_OFFLINE", "1")
    mp.setenv("ITG_FORECAST_RUNS", "5")
    mp.setenv("ITG_DATA_DIR", str(data))
    import itg.api.app as app_module
    app_module = importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        yield c
    mp.undo()


def test_health_reports_offline(client):
    assert client.get("/api/health").json()["foundry"] is False


def test_generate_and_list_matches(client):
    created = client.post("/api/matches", json={"home": "carb-loaders", "away": "expected-ls", "seed": 3})
    assert created.status_code == 200
    match_id = created.json()["match_id"]
    listed = client.get("/api/matches").json()
    assert any(m["match_id"] == match_id and m["cached_run"] is False for m in listed)
    assert client.post("/api/matches", json={"home": "carb-loaders", "away": "carb-loaders", "seed": 1}).status_code == 400


def test_websocket_streams_a_whole_match(client):
    match_id = client.post("/api/matches", json={"home": "expected-ls", "away": "press-gang",
                                                 "seed": 42}).json()["match_id"]
    kinds, moments = set(), 0
    with client.websocket_connect(f"/ws/matches/{match_id}?source=live&speed=1000000") as ws:
        assert ws.receive_json()["type"] == "room"
        first = ws.receive_json()
        assert first["type"] == "match" and len(first["profiles"]) == 4  # 4 viewers, English only.
        ws.send_json({"type": "languages", "value": ["fr", "xx"]})
        while True:
            message = ws.receive_json()
            kinds.add(message["type"])
            if message["type"] == "profiles":
                assert {p["language"] for p in message["profiles"]} == {"en", "fr"}
            if message["type"] == "moment":
                moments += 1
                assert message["cues"] and message["cues"][0]["template"].startswith("lower_third_")
            if message["type"] == "status" and message["state"] == "ended":
                break
        ws.send_json({"type": "ask", "question": "Who is winning?"})
        while (answer := ws.receive_json())["type"] != "answer":
            pass
    assert {"event", "snapshot", "signal", "moment", "recap", "player_report"} <= kinds
    assert moments >= 10
    assert "Expected Ls" in answer["answer"]


def test_unknown_match_is_reported(client):
    with client.websocket_connect("/ws/matches/nope") as ws:
        assert ws.receive_json()["type"] == "error"


def test_cue_sheet_is_sorted_and_filtered():
    match = simulate_match(CLUBS["expected-ls"], CLUBS["press-gang"], seed=42)
    profiles = default_profiles(match, ["en", "fr"])
    orchestrator = MatchOrchestrator(match, profiles, AgentTeam(LLM(None)), forecast_runs=0)
    asyncio.run(orchestrator.run())
    sheet = cue_sheet(match.info.match_id, orchestrator.memory, profiles, feed="home-fan.fr")
    assert sheet["cues"] and all(c["feed"] == "home-fan.fr" and c["language"] == "fr" for c in sheet["cues"])
    keys = [(c["period"], c["match_clock_s"]) for c in sheet["cues"]]
    assert keys == sorted(keys)


def test_live_sessions_are_capped(client, monkeypatch):
    match_id = client.post("/api/matches", json={"home": "benchwarmers-fc", "away": "carb-loaders",
                                                 "seed": 11}).json()["match_id"]
    import itg.api.app as app_module
    monkeypatch.setattr(app_module, "MAX_LIVE_SESSIONS", 0)
    state = client.app.state
    offline = state.llm
    state.llm = LLM(client=object())  # Pretend Foundry is connected.
    try:
        with client.websocket_connect(f"/ws/matches/{match_id}?source=live") as ws:
            message = ws.receive_json()
            assert message["type"] == "error" and "busy" in message["message"]
    finally:
        state.llm = offline


def until(ws, predicate, limit=20000):
    for _ in range(limit):
        message = ws.receive_json()
        if predicate(message):
            return message
    raise AssertionError("message never arrived")


def test_watch_room_is_shared(client):
    match_id = client.post("/api/matches", json={"home": "expected-ls", "away": "press-gang",
                                                 "seed": 5}).json()["match_id"]
    with client.websocket_connect(f"/ws/matches/{match_id}?source=live&speed=1000000") as host:
        welcome = host.receive_json()
        assert welcome["type"] == "room" and welcome["host"] is True
        code = welcome["code"]
        until(host, lambda m: m["type"] == "status" and m["state"] == "ended")
        assert client.get(f"/api/rooms/{code}").json()["members"] == 1

        with client.websocket_connect(f"/ws/matches/{match_id}?room={code}") as guest:
            joined = guest.receive_json()
            assert joined["code"] == code and joined["host"] is False
            assert guest.receive_json()["type"] == "match"  # A late arrival gets the match so far.
            until(guest, lambda m: m["type"] == "moment")
            until(guest, lambda m: m["type"] == "social" and m["social"]["members"] == 2)

            home = "expected-ls"
            guest.send_json({"type": "support", "team_id": home})
            social = until(host, lambda m: m["type"] == "social" and m["social"]["stands"][home]["fans"] == 1)
            assert social["social"]["rating_open"] is True and social["social"]["predictions_open"] is False

            guest.send_json({"type": "rate", "stars": 4})
            host.send_json({"type": "rate", "stars": 2})
            until(guest, lambda m: m["type"] == "social" and m["social"]["ratings"]["count"] == 2)

            guest.send_json({"type": "propose", "team_id": home, "kind": "tactic", "tactic": "high_press"})
            ready = until(host, lambda m: m["type"] == "social" and m["social"]["proposals"]
                          and m["social"]["proposals"][0]["status"] == "ready")
            proposal = ready["social"]["proposals"][0]
            assert proposal["score"] == 1 and proposal["win_before"] is not None
            host.send_json({"type": "vote", "proposal_id": proposal["id"], "value": -1})
            until(guest, lambda m: m["type"] == "social" and m["social"]["proposals"][0]["score"] == 0)

            guest.send_json({"type": "catch_up", "profile_id": "home-fan.fr", "since": 0})
            catch = until(guest, lambda m: m["type"] == "catch_up")
            assert catch["catch_up"]["headline"]


def test_speech_is_escaped_and_reports_when_unavailable(client):
    from itg.api.speech import Speech

    ssml = Speech.ssml("Goal! <b>2-1</b> & counting", "fr", excited=True)
    assert "fr-FR-HenriNeural" in ssml and "&lt;b&gt;" in ssml and "&amp;" in ssml and "+12%" in ssml
    assert client.get("/api/speech", params={"text": "Hello", "lang": "en"}).status_code == 503  # Offline.


def _mcp(client, method, params=None, id_=1):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _tool(client, name, **arguments):
    result = _mcp(client, "tools/call", {"name": name, "arguments": arguments})
    text = result["content"][0]["text"]
    return result.get("isError", False), text


def test_mcp_server_answers_copilot(client):
    init = _mcp(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}})
    assert init["serverInfo"]["name"] == "Prompt the Pitch"
    tools = {t["name"] for t in _mcp(client, "tools/list")["tools"]}
    assert {"list_matches", "get_match_state", "simulate_what_if", "get_win_probability_timeline"} <= tools

    match_id = client.post("/api/matches", json={"home": "press-gang", "away": "benchwarmers-fc",
                                                 "seed": 8}).json()["match_id"]
    error, text = _tool(client, "list_matches")
    assert not error and match_id in text
    error, text = _tool(client, "get_match_state", match_id=match_id, minute=30)
    assert not error and json.loads(text)["result"]["clock"] == "30'"
    error, text = _tool(client, "get_team_stats", team="PRG", match_id=match_id, minute=30)
    assert not error and json.loads(text)["result"]["short_name"] == "PRG"
    error, text = _tool(client, "simulate_what_if", match_id=match_id, minute=60, goals_home=1)
    assert not error and json.loads(text)["result"]["from"] == "60'"
    # Anticipated failures reach the model as a readable message.
    error, text = _tool(client, "get_match_state", room="NOPE00")
    assert error and "No open watch room" in text
    error, text = _tool(client, "simulate_what_if", match_id=match_id)
    assert error and "minute" in text
