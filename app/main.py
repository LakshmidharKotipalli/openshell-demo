"""FastAPI app: one WebSocket (/ws) for everything interactive, plus a static page and /api/health."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .openshell_client import OpenShellClient, OpenShellError
from .protocol import client_message_adapter
from .session import MODEL, PROVIDER, Busy, Session

STATIC = Path(__file__).resolve().parent / "static"
REPLAY = os.environ.get("REPLAY", "") not in ("", "0", "false", "False")
client = OpenShellClient()
session = Session(client, replay=REPLAY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not REPLAY:
        # A previous crash may have left demo sandboxes behind; start clean.
        await session.cleanup()
    reaper = asyncio.create_task(session.idle_reaper())
    try:
        yield
    finally:
        reaper.cancel()
        await session.cleanup()  # delete sandboxes on shutdown


app = FastAPI(title="Sandboxed Agent Console", lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict:
    if REPLAY:
        return {"mode": "replay", "ok": True, "openshell": None, "provider": None, "model": MODEL}
    connected = await client.is_connected()
    provider_ok = False
    if connected:
        try:
            provider_ok = PROVIDER in await client.provider_names()
        except OpenShellError:
            pass
    return {"mode": "live", "ok": connected and provider_ok, "openshell": connected,
            "provider": provider_ok, "provider_name": PROVIDER, "model": MODEL}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await session.on_connect(ws, 0)
    try:
        while True:
            raw = await ws.receive_json()
            ref = raw.get("id", "") if isinstance(raw, dict) else ""
            try:
                msg = client_message_adapter.validate_python(raw)
            except ValidationError as e:
                await ws.send_json({"type": "error", "ref": ref, "message": "bad message: " + e.errors()[0]["msg"]})
                continue
            if msg.type == "hello":  # resume: replay events after last_seq (on_connect sent none yet)
                for ev in session.log[msg.last_seq:]:
                    await session.send(ws, ev)
                continue
            asyncio.create_task(_run(ws, msg, ref))
    except WebSocketDisconnect:
        pass
    finally:
        session.clients.discard(ws)


async def _run(ws: WebSocket, msg, ref: str) -> None:
    try:
        await session.handle(ws, msg)
        await session.send(ws, {"type": "ack", "ref": ref})
    except Busy as e:
        await session.send(ws, {"type": "error", "ref": ref, "message": str(e)})
    except (OpenShellError, ValueError) as e:
        await session.emit("error", message=str(e))
        if session.status in ("creating", "running"):
            await session.set_status("error")
        await session.send(ws, {"type": "error", "ref": ref, "message": str(e)})
    except Exception as e:  # noqa: BLE001
        logging.getLogger("uvicorn.error").exception("command failed")
        await session.emit("error", message=f"internal error: {type(e).__name__}: {e}")
        await session.set_status("error")
        await session.send(ws, {"type": "error", "ref": ref, "message": f"internal error: {type(e).__name__}"})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
