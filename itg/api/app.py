"""Web server: REST API, live WebSocket stream and the front end.

    uvicorn itg.api.app:app --reload
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from itg.agents.briefs import LANGUAGES, default_roles
from itg.agents.llm import open_llm
from itg.api.mcp_server import mount_mcp
from itg.api.overlays import cue_sheet
from itg.api.rooms import Member, Room, nickname
from itg.api.session import MatchSession, load_cached_reports, load_cached_run
from itg.api.speech import VOICES, Speech
from itg.schema import Match
from itg.synthetic.clubs import CLUBS
from itg.synthetic.replay import load_match
from itg.synthetic.simulator import simulate_match
from itg.tracking import build_tracking

ROOT = Path(os.environ.get("ITG_DATA_DIR", "data"))
MATCHES, RUNS = ROOT / "matches", ROOT / "runs"
WEB = Path(__file__).resolve().parents[2] / "web"
# Live sessions call Foundry for every key moment; on a public URL, cap them to bound the cost.
MAX_LIVE_SESSIONS = int(os.environ.get("ITG_MAX_LIVE_SESSIONS", "3"))
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    MATCHES.mkdir(parents=True, exist_ok=True)
    async with open_llm() as llm:
        app.state.llm = llm
        app.state.speech = Speech(llm)
        app.state.matches = {}
        app.state.rooms = {}
        app.state.tracking = {}
        async with mcp.session_manager.run():
            yield


app = FastAPI(title="Prompt the Pitch: match intelligence", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)


def get_match(match_id: str) -> Match:
    cache: dict[str, Match] = app.state.matches
    if match_id not in cache:
        path = MATCHES / f"{match_id}.json"
        if not path.is_file():
            raise HTTPException(404, f"Unknown match {match_id!r}")
        cache[match_id] = load_match(path)
    return cache[match_id]


@app.get("/api/health")
async def health() -> dict:
    llm = app.state.llm
    return {"status": "ok", "foundry": llm.online, "model": getattr(llm.client, "model", None)}


@app.get("/api/speech")
async def speech(text: str = Query(..., max_length=400), lang: str = Query("en"), excited: bool = False) -> Response:
    """Spoken version of an overlay, in the viewer's language (Azure AI Speech)."""
    service: Speech = app.state.speech
    if not service.available or lang not in VOICES:
        raise HTTPException(503, "Speech is not available; the browser voice can be used instead.")
    try:
        audio = await service.synthesize(text, lang, excited)
    except Exception as exc:
        log.warning("speech failed: %r", exc)
        raise HTTPException(503, "Speech is temporarily unavailable.")
    return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/clubs")
async def clubs() -> list[dict]:
    return [{"id": t.id, "name": t.name, "short_name": t.short_name, "colors": t.colors, "formation": t.formation,
             "style": t.style.model_dump(),
             "players": [{"id": p.id, "name": p.name, "short_name": p.short_name, "position": p.position}
                         for p in t.players]}
            for t in CLUBS.values()]


@app.get("/api/matches")
async def list_matches() -> list[dict]:
    out = []
    for path in sorted(MATCHES.glob("*.json")):
        match = get_match(path.stem)
        home, away = match.teams
        out.append({"match_id": path.stem, "home": home.name, "away": away.name,
                    "home_colors": home.colors, "away_colors": away.colors,
                    "cached_run": (RUNS / f"{path.stem}.json").is_file()})
    return out


class NewMatch(BaseModel):
    home: str
    away: str
    seed: int


@app.post("/api/matches")
async def create_match(request: NewMatch) -> dict:
    if request.home not in CLUBS or request.away not in CLUBS or request.home == request.away:
        raise HTTPException(400, "Choose two different clubs.")
    match = await asyncio.to_thread(simulate_match, CLUBS[request.home], CLUBS[request.away], request.seed)
    (MATCHES / f"{match.info.match_id}.json").write_text(match.model_dump_json(), encoding="utf-8")
    app.state.matches[match.info.match_id] = match
    return {"match_id": match.info.match_id}


@app.get("/api/matches/{match_id}/overlays")
async def overlays(match_id: str, feed: str | None = None) -> dict:
    """Cue sheet of every overlay of a recorded run, for a graphics engine."""
    path = RUNS / f"{match_id}.json"
    if not path.is_file():
        raise HTTPException(404, "No recorded agent run for this match.")
    profiles, moments, _ = load_cached_run(path)
    return cue_sheet(match_id, list(moments.values()), profiles, feed)


@app.get("/api/matches/{match_id}/tracking")
async def tracking(match_id: str) -> dict:
    """Every player's and the ball's position through the match, for the 3D stadium."""
    cache: dict[str, dict] = app.state.tracking
    if match_id not in cache:
        cache[match_id] = await asyncio.to_thread(build_tracking, get_match(match_id))
    return cache[match_id]


def live_rooms() -> int:
    return sum(1 for room in app.state.rooms.values() if room.source == "live")


@app.get("/api/rooms/{code}")
async def room_info(code: str) -> dict:
    room = app.state.rooms.get(code.upper())
    if room is None:
        raise HTTPException(404, "This watch room has closed.")
    return {"code": room.code, "match_id": room.match.info.match_id, "source": room.source,
            "members": len(room.members)}


@app.websocket("/ws/matches/{match_id}")
async def live(websocket: WebSocket, match_id: str,
               source: Literal["live", "cached"] = Query("cached"), speed: float = Query(30.0),
               languages: str = Query("en"), room: str | None = Query(None),
               observer: bool = Query(False)) -> None:
    """Join a watch room (`room` code) or open a new one for this match."""
    await websocket.accept()
    try:
        match = get_match(match_id)
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "message": exc.detail})
        await websocket.close()
        return
    rooms: dict[str, Room] = app.state.rooms
    target = rooms.get((room or "").upper())
    if target is not None and target.match.info.match_id != match_id:
        target = None
    if target is None:
        target = await open_room(websocket, match, source, speed, languages)
        if target is None:
            return

    lock = asyncio.Lock()

    async def send(message: dict) -> None:
        async with lock:
            await websocket.send_json(message)

    member = Member(id=secrets.token_hex(4), nickname=nickname(), send=send, observer=observer)
    await target.join(member)
    try:
        while True:
            await target.handle(member, await websocket.receive_json())
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("room %s failed", target.code)
    finally:
        await target.leave(member.id)


async def open_room(websocket: WebSocket, match: Match, source: str, speed: float, languages: str) -> Room | None:
    llm = app.state.llm
    match_id = match.info.match_id
    run_path = RUNS / f"{match_id}.json"
    if source == "cached" and not run_path.is_file():
        source = "live"
    if source == "live" and llm.online and live_rooms() >= MAX_LIVE_SESSIONS:
        busy = "Live agents are busy with other viewers right now."
        if run_path.is_file():
            source = "cached"
            await websocket.send_json({"type": "notice", "message": busy + " Showing the recorded run instead."})
        else:
            await websocket.send_json({"type": "error", "message": busy + " Please try again in a few minutes."})
            await websocket.close()
            return None
    langs = [lang for lang in languages.split(",") if lang in LANGUAGES] or ["en"]
    if source == "cached":
        profiles, moments, recaps = load_cached_run(run_path)
        reports = load_cached_reports(run_path)
    else:
        profiles, moments, recaps, reports = None, {}, {}, []
    code = secrets.token_hex(3).upper()
    while code in app.state.rooms:
        code = secrets.token_hex(3).upper()

    def factory(send):
        return MatchSession(match, llm, send, source, default_roles(match), langs, profiles, moments, recaps,
                            reports, speed)

    room = Room(code, match, factory, on_close=lambda: app.state.rooms.pop(code, None))
    app.state.rooms[code] = room
    return room


# GitHub Copilot (or any MCP client) asks about the matches and watch rooms at /mcp.
mcp = mount_mcp(app, RUNS, get_match, list_matches)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
