"""FastAPI-Webserver: Handy-Oberfläche + JSON-API."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config
from .control import ControlLoop
from .history import History
from .hk import HomeKit

STATIC = Path(__file__).parent / "static"


class ManualReq(BaseModel):
    aid: int
    iid: int
    on: bool


class ProgReq(BaseModel):
    name: str


def create_app(config_path: str = "config.yaml") -> FastAPI:
    cfg = Config.load(config_path)
    hk = HomeKit(cfg.pairing_file, cfg.alias)
    history = History(cfg.log_file, cfg.log_enabled)
    loop = ControlLoop(hk, cfg, history)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hk.start()
        history.start()
        await loop.start()
        try:
            yield
        finally:
            await loop.stop()
            history.close()
            await hk.stop()

    app = FastAPI(title="Pasta-Trockner", lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        return loop.state()

    @app.post("/api/off")
    async def off():
        loop.set_off()
        return loop.state()

    @app.post("/api/manual")
    async def manual(req: ManualReq):
        loop.set_manual(req.aid, req.iid, req.on)
        return loop.state()

    @app.post("/api/program/start")
    async def program_start(req: ProgReq):
        if not loop.start_program(req.name):
            return JSONResponse({"error": "unbekanntes Programm"}, status_code=404)
        return loop.state()

    @app.post("/api/program/stop")
    async def program_stop():
        loop.set_off()
        return loop.state()

    @app.post("/api/program/skip")
    async def program_skip():
        loop.skip_phase()
        return loop.state()

    @app.get("/api/history")
    async def get_history(hours: float = 72):
        since = time.time() - hours * 3600
        names = {aid: s["name"] for aid, s in loop.sensors.items()}
        return {"names": names, "series": history.series(since)}

    @app.get("/api/history.csv")
    async def get_history_csv(hours: float = 72):
        since = time.time() - hours * 3600
        return PlainTextResponse(
            history.csv(since),
            headers={"Content-Disposition": "attachment; filename=pasta-history.csv"},
        )

    @app.post("/api/history/clear")
    async def clear_history():
        history.clear()
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
