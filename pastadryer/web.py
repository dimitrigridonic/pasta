"""FastAPI-Webserver: Dashboard + JSON-API."""
from __future__ import annotations

import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
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


_NUM_FIELDS = ("duration_h", "humidity_start", "humidity_end", "temp_low", "temp_high")
MAX_PHASE_H = 1000.0        # längste erlaubte Phase (Std.) – schützt Zeitrechnung/State vor Überlauf
BAND_MARGIN = 1.0           # Band-Obergrenze muss so weit unter max_temp bleiben (= Engine-Clamp)


def _validate_phases(phases, cfg: Config) -> str | None:
    """Prüft ein Programm VOR dem Speichern/Starten. Mutiert die Phasen: Zahlen werden
    zu float normalisiert, damit nie Strings/NaN in programs.json landen (die Engine
    vergleicht damit und würde sonst jeden Tick crashen — VOR der Sicherheitskette).
    Temperaturband: 'aus' > 'an' und 'aus' < max_temp, sonst flattert die Abschaltung."""
    if not isinstance(phases, list) or not phases:
        return "Ein Programm braucht mindestens eine Phase."
    if len(phases) > 200:
        return "Zu viele Phasen (max. 200)."
    for i, ph in enumerate(phases, 1):
        if not isinstance(ph, dict):
            return f"Phase {i}: ungültiges Format."
        ph["name"] = str(ph.get("name") or f"Phase {i}")[:80]
        for key in _NUM_FIELDS:
            v = ph.get(key)
            if v is None:
                ph.pop(key, None)
                continue
            try:
                ok = not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(float(v))
            except OverflowError:       # int jenseits float-Bereich
                ok = False
            if not ok:
                return f"Phase {i}: '{key}' muss eine Zahl sein (ist {str(v)[:40]!r})."
            ph[key] = float(v)
        dur = ph.get("duration_h")
        if dur is None or dur < 0:
            return f"Phase {i}: Dauer (Std.) fehlt oder ist negativ."
        if dur > MAX_PHASE_H:
            return f"Phase {i}: Dauer ({dur:g} h) über dem Maximum von {MAX_PHASE_H:g} h."
        for key in ("humidity_start", "humidity_end"):
            v = ph.get(key)
            if v is not None and not (0 <= v <= 100):
                return f"Phase {i}: '{key}' muss zwischen 0 und 100 % liegen."
        lo, hi = ph.get("temp_low"), ph.get("temp_high")
        if lo is None and hi is None:
            continue
        lo_eff = cfg.temp_low if lo is None else lo
        hi_eff = cfg.temp_high if hi is None else hi
        if lo is not None and lo < 5:
            return f"Phase {i}: '°C an' ({lo:g}) ist unplausibel niedrig."
        if hi_eff <= lo_eff:
            return f"Phase {i}: '°C aus' ({hi_eff:g}) muss über '°C an' ({lo_eff:g}) liegen."
        if hi_eff > cfg.max_temp - BAND_MARGIN:
            return (f"Phase {i}: '°C aus' ({hi_eff:g}) muss mindestens {BAND_MARGIN:g} °C unter der "
                    f"Sicherheits-Abschaltung max_temp ({cfg.max_temp:g} °C, config.yaml) liegen, "
                    f"also ≤ {cfg.max_temp - BAND_MARGIN:g}.")
    return None


def _clean_name(name) -> str | None:
    n = " ".join(str(name or "").split())[:80]
    return n or None


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

    def _check_stored(name: str) -> None:
        """Gespeichertes Programm vor Start/Resume prüfen (auch alte programs.json)."""
        raw = next((p for p in store.list() if p.get("name") == name), None)
        if raw is None:
            raise HTTPException(status_code=404, detail="unbekanntes Programm")
        phases = raw.get("phases")
        copy = [dict(ph) if isinstance(ph, dict) else ph for ph in phases] if isinstance(phases, list) else phases
        err = _validate_phases(copy, cfg)
        if err:
            raise HTTPException(status_code=400, detail=f"Programm '{name}' ungültig – {err}")

    @app.post("/api/program/start")
    async def program_start(req: ProgReq):
        _check_stored(req.name)
        if not loop.start_program(req.name):
            raise HTTPException(status_code=409, detail="Start nicht möglich (Not-Aus verriegelt? erst quittieren)")
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
        if not math.isfinite(req.elapsed_s) or req.elapsed_s < 0 or req.elapsed_s > MAX_PHASE_H * 3600:
            raise HTTPException(status_code=400, detail="elapsed_s ungültig")
        _check_stored(req.name)
        if not loop.resume_program(req.name, req.phase_index, req.elapsed_s):
            raise HTTPException(status_code=409, detail="Wiederaufnahme fehlgeschlagen (Not-Aus verriegelt?)")
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
        name = _clean_name(body.name)
        if name is None:
            raise HTTPException(status_code=400, detail="Programmname fehlt.")
        target = body.old_name or name
        if name != target and any(p.get("name") == name for p in store.list()):
            raise HTTPException(status_code=400, detail=f"Es gibt schon ein Programm namens '{name}'.")
        err = _validate_phases(body.phases, cfg)
        if err:
            raise HTTPException(status_code=400, detail=err)
        store.upsert(name, body.phases, body.old_name)
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
