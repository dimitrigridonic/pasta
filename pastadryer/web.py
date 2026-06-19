"""FastAPI-Webserver: Dashboard + JSON-API."""
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
from .zb import Zigbee
from .programs import ProgramStore

STATIC = Path(__file__).parent / "static"


class ManualReq(BaseModel):
    aid: str
    iid: str
    on: bool


class ProgReq(BaseModel):
    name: str


class NudgeReq(BaseModel):
    delta: float


class HumRefReq(BaseModel):
    mode: str


class ResumeReq(BaseModel):
    name: str
    phase_index: int = 0
    elapsed_s: float = 0


class ProgramBody(BaseModel):
    name: str
    phases: list[dict]
    old_name: str | None = None


class RenameReq(BaseModel):
    aid: str
    name: str


def create_app(config_path: str = "config.yaml") -> FastAPI:
    cfg = Config.load(config_path)
    zb = Zigbee(cfg.mqtt_host, cfg.mqtt_port)
    history = History(cfg.log_file, cfg.log_enabled)
    store = ProgramStore("programs.json", cfg.programs)
    store.load()
    loop = ControlLoop(zb, cfg, history, store, "sensor_names.json")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await zb.start()
        history.start()
        await loop.start()
        try:
            yield
        finally:
            await loop.stop()
            history.close()
            await zb.stop()

    app = FastAPI(title="Pasta-Trockner", lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        return loop.state()

    @app.api_route("/api/off", methods=["GET", "POST"])
    async def off():
        loop.set_off()
        return loop.state()

    @app.post("/api/manual")
    async def manual(req: ManualReq):
        loop.set_manual(req.aid, req.iid, req.on)
        return loop.state()

    @app.api_route("/api/manual/enter", methods=["GET", "POST"])
    async def manual_enter():
        loop.enter_manual()
        return loop.state()

    @app.api_route("/api/overrides/clear", methods=["GET", "POST"])
    async def overrides_clear():
        loop.clear_overrides()
        return loop.state()

    @app.post("/api/program/start")
    async def program_start(req: ProgReq):
        if not loop.start_program(req.name):
            return JSONResponse({"error": "unbekanntes Programm"}, status_code=404)
        return loop.state()

    @app.api_route("/api/program/stop", methods=["GET", "POST"])
    async def program_stop():
        loop.set_off()
        return loop.state()

    @app.api_route("/api/program/skip", methods=["GET", "POST"])
    async def program_skip():
        loop.skip_phase()
        return loop.state()

    @app.post("/api/program/nudge")
    async def program_nudge(req: NudgeReq):
        loop.nudge_humidity(req.delta)
        return loop.state()

    @app.post("/api/humref")
    async def set_humref(req: HumRefReq):
        loop.set_hum_ref(req.mode)
        return loop.state()

    @app.post("/api/program/resume")
    async def program_resume(req: ResumeReq):
        if not loop.resume_program(req.name, req.phase_index, req.elapsed_s):
            return JSONResponse({"error": "Wiederaufnahme fehlgeschlagen"}, status_code=400)
        return loop.state()

    @app.api_route("/api/fault/clear", methods=["GET", "POST"])
    async def fault_clear():
        loop.clear_fault()
        return loop.state()

    @app.api_route("/api/sensors/read", methods=["GET", "POST"])
    async def sensors_read():
        await loop.read_once()
        return loop.state()

    # --- Programm-Editor ---
    @app.get("/api/programs")
    async def programs_list():
        return store.list()

    @app.post("/api/programs")
    async def programs_save(body: ProgramBody):
        store.upsert(body.name, body.phases, body.old_name)
        return store.list()

    @app.delete("/api/programs/{name}")
    async def programs_delete(name: str):
        store.delete(name)
        return store.list()

    # --- Sensor umbenennen ---
    @app.post("/api/sensor/name")
    async def sensor_name(req: RenameReq):
        loop.set_sensor_name(req.aid, req.name.strip() or f"Sensor {req.aid}")
        return loop.state()

    # --- Verlauf ---
    @app.get("/api/history")
    async def get_history(hours: float = 72):
        since = time.time() - hours * 3600
        names = {aid: s["name"] for aid, s in loop.sensors.items()}
        return {"names": names, "series": history.series(since)}

    # --- Analyse: vergangene Durchgänge ---
    @app.get("/api/runs")
    async def get_runs():
        names = {aid: s["name"] for aid, s in loop.sensors.items()}
        return {"names": names, "runs": history.runs()}

    @app.get("/api/run")
    async def get_run(start: float, end: float):
        names = {aid: s["name"] for aid, s in loop.sensors.items()}
        return {"names": names, **history.run_series(start, end)}

    @app.get("/api/history.csv")
    async def get_history_csv(hours: float = 72, start: float | None = None,
                              end: float | None = None):
        if start is not None or end is not None:
            data = history.csv(start, end)
        else:
            data = history.csv(time.time() - hours * 3600)
        return PlainTextResponse(
            data,
            headers={"Content-Disposition": "attachment; filename=pasta-history.csv"},
        )

    @app.post("/api/history/clear")
    async def clear_history():
        history.clear()
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
