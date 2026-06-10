"""Regel-Engine für den Pasta-Trockner.

Modell (vom Nutzer vorgegeben):
  • Heizungen (beide) halten die Temperatur in einem Band (temp_low..temp_high, z.B. 30–32 °C).
  • Lüfter senken die Feuchte: laufen, wenn die (aggregierte) Feuchte übers aktuelle
    Kurven-Ziel steigt — abwechselnd links/rechts im fan_cycle_min-Takt.
  • Trocken-Programm = Phasen mit Feuchte-Rampen (z.B. 80% halten → 80→70% → 70→60%).
  • Alle Sensoren werden einzeln gelesen, angezeigt und geloggt.

Modi: off | manual | program
"""
from __future__ import annotations

import asyncio
import logging
import time

from .config import Config, Program
from .history import History
from .hk import HomeKit

log = logging.getLogger(__name__)

TYPE_TEMP = "00000011"
TYPE_HUM = "00000010"


def _short(uuid: str) -> str:
    return str(uuid).split("-")[0].lower().zfill(8)[-8:]


class ControlLoop:
    def __init__(self, hk: HomeKit, cfg: Config, history: History):
        self.hk = hk
        self.cfg = cfg
        self.history = history

        # Sensoren: aid -> {name, temp, hum, temp_iid, hum_iid}
        self.sensors: dict[int, dict] = {}
        self.agg_temp: float | None = None
        self.agg_hum: float | None = None
        self.max_temp_seen: float | None = None
        self.last_reading_ok = False
        self.last_reading_at: float | None = None

        # Stellglieder: gewünschter Zustand je (aid,iid)
        self.desired: dict[tuple[int, int], bool] = {}
        self._written: dict[tuple[int, int], bool | None] = {}

        # Modus / manuell
        self.mode = "off"
        self.manual: dict[tuple[int, int], bool] = {}
        for ch in self.cfg.heaters + self.cfg.fans:
            self.manual[ch.point()] = False

        # Heizung/Lüfter-Status (für Anzeige)
        self.heater_on = False
        self.fan_active = 0          # Index in cfg.fans (alternierende Seite)
        self.venting = False
        self._fan_cycle_started = 0.0

        # Programm
        self.program: Program | None = None
        self.phase_index = 0
        self._phase_started: float | None = None
        self._program_started: float | None = None
        self.humidity_target: float | None = None
        self.safety_tripped = False
        self.last_error: str | None = None
        self._last_log = 0.0

        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._running = False

    # ---- Lifecycle -------------------------------------------------------
    async def start(self) -> None:
        await self._discover_sensors()
        self._running = True
        self._fan_cycle_started = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task:
            await self._task
        await self._all_off()

    async def _discover_sensors(self) -> None:
        data = await self.hk.list_accessories()
        names = {s.aid: s.name for s in self.cfg.sensors}
        wanted = {s.aid for s in self.cfg.sensors}  # leer = alle
        for acc in data:
            aid = acc.get("aid")
            temp_iid = hum_iid = None
            for svc in acc.get("services", []):
                for ch in svc.get("characteristics", []):
                    s = _short(ch.get("type"))
                    if s == TYPE_TEMP:
                        temp_iid = ch.get("iid")
                    elif s == TYPE_HUM:
                        hum_iid = ch.get("iid")
            if temp_iid is None and hum_iid is None:
                continue
            if wanted and aid not in wanted:
                continue
            self.sensors[aid] = {
                "name": names.get(aid, f"Sensor {aid}"),
                "temp": None, "hum": None,
                "temp_iid": temp_iid, "hum_iid": hum_iid,
            }
        log.info("Sensoren erkannt: %s", sorted(self.sensors))

    def _kick(self) -> None:
        self._wake.set()

    # ---- Bedien-API ------------------------------------------------------
    def set_off(self) -> None:
        self.mode = "off"
        self.program = None
        self._kick()

    def set_manual(self, aid: int, iid: int, value: bool) -> None:
        self.mode = "manual"
        self.program = None
        self.manual[(aid, iid)] = value
        self._kick()

    def start_program(self, name: str) -> bool:
        prog = next((p for p in self.cfg.programs if p.name == name), None)
        if prog is None:
            return False
        self.program = prog
        self.phase_index = 0
        self._phase_started = time.monotonic()
        self._program_started = time.time()
        self.mode = "program"
        self._kick()
        return True

    def skip_phase(self) -> None:
        if self.mode == "program" and self.program:
            self._advance_phase(force=True)
            self._kick()

    # ---- Hauptschleife ---------------------------------------------------
    async def _run(self) -> None:
        log.info("Control-Loop gestartet (Intervall %ss)", self.cfg.poll_interval)
        while self._running:
            try:
                await self._read_sensors()
                self._decide()
                await self._apply()
                self._maybe_log()
            except Exception as e:
                self.last_error = str(e)
                log.exception("Tick-Fehler: %s", e)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.poll_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _read_sensors(self) -> None:
        points = []
        for aid, s in self.sensors.items():
            if s["temp_iid"]:
                points.append((aid, s["temp_iid"]))
            if s["hum_iid"]:
                points.append((aid, s["hum_iid"]))
        if not points:
            return
        try:
            vals = await self.hk.get_values(points)
            for aid, s in self.sensors.items():
                if s["temp_iid"] and (aid, s["temp_iid"]) in vals:
                    v = vals[(aid, s["temp_iid"])]
                    if v is not None:
                        s["temp"] = round(float(v), 1)
                if s["hum_iid"] and (aid, s["hum_iid"]) in vals:
                    v = vals[(aid, s["hum_iid"])]
                    if v is not None:
                        s["hum"] = round(float(v), 1)
            self._aggregate()
            self.last_reading_ok = True
            self.last_reading_at = time.time()
            self.last_error = None
        except Exception as e:
            self.last_reading_ok = False
            self.last_error = f"Sensoren lesen: {e}"
            log.warning("Sensorlesen fehlgeschlagen: %s", e)

    def _aggregate(self) -> None:
        temps = [s["temp"] for s in self.sensors.values() if s["temp"] is not None]
        hums = [s["hum"] for s in self.sensors.values() if s["hum"] is not None]
        self.max_temp_seen = max(temps) if temps else None

        def agg(vals):
            if not vals:
                return None
            if self.cfg.aggregate == "max":
                return max(vals)
            if self.cfg.aggregate == "min":
                return min(vals)
            return round(sum(vals) / len(vals), 1)

        self.agg_temp = agg(temps)
        self.agg_hum = agg(hums)

    # ---- Regel-Logik -----------------------------------------------------
    def _decide(self) -> None:
        # Stellglied-Wunsch auf Basis Modus berechnen
        if self.mode == "off":
            self.heater_on = False
            self.venting = False
            for ch in self.cfg.heaters + self.cfg.fans:
                self.desired[ch.point()] = False

        elif self.mode == "manual":
            self.heater_on = any(self.manual.get(h.point()) for h in self.cfg.heaters)
            self.venting = any(self.manual.get(f.point()) for f in self.cfg.fans)
            for ch in self.cfg.heaters + self.cfg.fans:
                self.desired[ch.point()] = self.manual.get(ch.point(), False)

        elif self.mode == "program":
            self._decide_program()

        # Sicherheit: zu heiss -> Heizungen hart aus (in jedem Modus)
        self.safety_tripped = False
        if self.max_temp_seen is not None and self.max_temp_seen >= self.cfg.max_temp:
            self.safety_tripped = True
            self.heater_on = False
            for h in self.cfg.heaters:
                self.desired[h.point()] = False

    def _decide_program(self) -> None:
        if not self.program:
            self.set_off()
            return
        self._advance_phase()
        if self.mode != "program":
            return
        phase = self.program.phases[self.phase_index]

        # --- Heizung: Band-Thermostat (beide Heizungen gemeinsam) ---
        low = phase.temp_low if phase.temp_low is not None else self.cfg.temp_low
        high = phase.temp_high if phase.temp_high is not None else self.cfg.temp_high
        t = self.agg_temp
        if t is None:
            self.heater_on = False
        elif t < low:
            self.heater_on = True
        elif t > high:
            self.heater_on = False
        # zwischen low..high: Zustand halten
        for h in self.cfg.heaters:
            self.desired[h.point()] = self.heater_on

        # --- Feuchte-Ziel der Kurve (Rampe innerhalb der Phase) ---
        self.humidity_target = self._current_humidity_target(phase)

        # --- Lüfter: Hygrostat + alternierend links/rechts ---
        h = self.agg_hum
        tgt = self.humidity_target
        hyst = self.cfg.humidity_hysteresis
        warm_enough = t is not None and t >= self.cfg.min_temp_for_fan
        if tgt is not None and h is not None and warm_enough:
            if h > tgt + hyst:
                self.venting = True
            elif h < tgt - hyst:
                self.venting = False
        else:
            self.venting = False

        # alternierende Seite weiterschalten
        if self.cfg.fans:
            cycle = self.cfg.fan_cycle_min * 60
            if time.monotonic() - self._fan_cycle_started >= cycle:
                self.fan_active = (self.fan_active + 1) % len(self.cfg.fans)
                self._fan_cycle_started = time.monotonic()
        for i, f in enumerate(self.cfg.fans):
            self.desired[f.point()] = self.venting and (i == self.fan_active)

    def _current_humidity_target(self, phase) -> float | None:
        if phase.humidity_start is None:
            return None
        end = phase.humidity_end if phase.humidity_end is not None else phase.humidity_start
        if phase.duration_h is None or self._phase_started is None:
            return phase.humidity_start
        elapsed = time.monotonic() - self._phase_started
        frac = max(0.0, min(1.0, elapsed / (phase.duration_h * 3600)))
        return round(phase.humidity_start + (end - phase.humidity_start) * frac, 1)

    def _advance_phase(self, force: bool = False) -> None:
        if not self.program:
            return
        phase = self.program.phases[self.phase_index]
        elapsed = time.monotonic() - (self._phase_started or time.monotonic())
        done = force or (phase.duration_h is not None and elapsed >= phase.duration_h * 3600)
        if not done:
            return
        if self.phase_index + 1 < len(self.program.phases):
            self.phase_index += 1
            self._phase_started = time.monotonic()
            log.info("Phase -> %s", self.program.phases[self.phase_index].name)
        else:
            log.info("Programm '%s' fertig", self.program.name)
            self.program = None
            self.mode = "off"
            self.heater_on = self.venting = False

    # ---- Schalten --------------------------------------------------------
    async def _apply(self) -> None:
        for point, value in self.desired.items():
            if self._written.get(point) == value:
                continue
            try:
                await self.hk.set_value(point[0], point[1], value)
                self._written[point] = value
                log.info("%s -> %s", point, "AN" if value else "AUS")
            except Exception as e:
                self.last_error = f"schalten {point}: {e}"
                log.warning("Schalten %s fehlgeschlagen: %s", point, e)

    async def _all_off(self) -> None:
        for ch in self.cfg.heaters + self.cfg.fans:
            try:
                await self.hk.set_value(ch.aid, ch.iid, False)
            except Exception:
                pass

    def _maybe_log(self) -> None:
        now = time.time()
        if now - self._last_log >= self.cfg.log_interval:
            self._last_log = now
            self.history.log(now, self.sensors)

    # ---- Status für Web --------------------------------------------------
    def state(self) -> dict:
        phase = None
        phase_remaining = None
        if self.mode == "program" and self.program:
            ph = self.program.phases[self.phase_index]
            low = ph.temp_low if ph.temp_low is not None else self.cfg.temp_low
            high = ph.temp_high if ph.temp_high is not None else self.cfg.temp_high
            phase = {
                "index": self.phase_index, "count": len(self.program.phases),
                "name": ph.name, "temp_low": low, "temp_high": high,
                "humidity_target": self.humidity_target,
            }
            if ph.duration_h is not None and self._phase_started is not None:
                elapsed = time.monotonic() - self._phase_started
                phase_remaining = max(0, int(ph.duration_h * 3600 - elapsed))

        return {
            "mode": self.mode,
            "sensors": [
                {"aid": aid, "name": s["name"], "temp": s["temp"], "hum": s["hum"]}
                for aid, s in sorted(self.sensors.items())
            ],
            "agg_temp": self.agg_temp,
            "agg_hum": self.agg_hum,
            "aggregate": self.cfg.aggregate,
            "temp_low": self.cfg.temp_low,
            "temp_high": self.cfg.temp_high,
            "heater_on": self.heater_on,
            "venting": self.venting,
            "fan_active": self.fan_active,
            "heaters": [{"name": h.name, "on": self.desired.get(h.point(), False),
                         "aid": h.aid, "iid": h.iid} for h in self.cfg.heaters],
            "fans": [{"name": f.name, "on": self.desired.get(f.point(), False),
                      "aid": f.aid, "iid": f.iid} for f in self.cfg.fans],
            "reading_ok": self.last_reading_ok,
            "reading_age": (time.time() - self.last_reading_at) if self.last_reading_at else None,
            "safety_tripped": self.safety_tripped,
            "max_temp": self.cfg.max_temp,
            "program": self.program.name if self.program else None,
            "phase": phase,
            "phase_remaining": phase_remaining,
            "program_started": self._program_started,
            "error": self.last_error,
            "programs": [p.name for p in self.cfg.programs],
        }
