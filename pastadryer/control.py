"""Regel-Engine für den Pasta-Trockner.

Modell (vom Nutzer vorgegeben) — der IDEALLINIE folgen, FEUCHTE GEWINNT:
  • Jede Phase definiert eine Ideallinie (Feuchte über Zeit, Rampe). Die Feuchte
    soll ihr folgen und nie darunter fallen. Temperatur ist die Konstante (~30–32°C).
  • Über der Linie: Heizung hält 30–32 °C (mit Trägheit/Mindestzeiten) und trocknet.
  • An/unter der Linie: dynamische RUHEPHASE — alles aus, bis sich die Feuchte
    wieder über die Linie (+ Reserve) erholt hat (Dauer dynamisch, nicht fix).
  • Lüfter = Notnagel: nur bei STILLSTAND (Feuchte fällt über Stunden nicht).
  • Sensoren einzeln gelesen/angezeigt/geloggt; Chart zeigt die Ideallinie.

Modi: off | manual | program
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque

from .config import Config
from .history import History
from .hk import HomeKit
from .programs import ProgramStore

log = logging.getLogger(__name__)

TYPE_TEMP = "00000011"
TYPE_HUM = "00000010"
TYPE_BATT = "00000068"


def _short(uuid: str) -> str:
    return str(uuid).split("-")[0].lower().zfill(8)[-8:]


class ControlLoop:
    def __init__(self, hk: HomeKit, cfg: Config, history: History,
                 store: ProgramStore, names_path: str = "sensor_names.json"):
        self.hk = hk
        self.cfg = cfg
        self.history = history
        self.store = store
        self.names_path = names_path

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
        self._heater_changed_at = 0.0  # für Mindest-Laufzeit/-Pause (Trägheit)
        self.active_side = 0         # aktive Seite (0=links, 1=rechts) – Heizung UND Lüfter
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

        # SICHERHEIT: verriegelnder Not-Aus (Heizung-Dauerlauf)
        self.fault = False
        self.fault_reason: str | None = None
        self._heater_on_since: dict = {}   # point -> monotonic (seit wann an)

        # Ruhephasen / Abfall-Wächter
        self._hum_hist: deque = deque()      # (monotonic, agg_hum)
        self.resting = False
        self._rest_until = 0.0
        self.drop_rate: float | None = None  # %-Punkte/h fallend (positiv)
        self.allowed_drop: float | None = None

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
            temp_iid = hum_iid = batt_iid = None
            for svc in acc.get("services", []):
                for ch in svc.get("characteristics", []):
                    s = _short(ch.get("type"))
                    if s == TYPE_TEMP:
                        temp_iid = ch.get("iid")
                    elif s == TYPE_HUM:
                        hum_iid = ch.get("iid")
                    elif s == TYPE_BATT:
                        batt_iid = ch.get("iid")
            if temp_iid is None and hum_iid is None:
                continue
            if wanted and aid not in wanted:
                continue
            self.sensors[aid] = {
                "name": names.get(aid, f"Sensor {aid}"),
                "temp": None, "hum": None, "batt": None,
                "temp_iid": temp_iid, "hum_iid": hum_iid, "batt_iid": batt_iid,
            }
        self._apply_saved_names()
        log.info("Sensoren erkannt: %s", sorted(self.sensors))

    def _apply_saved_names(self) -> None:
        if not os.path.exists(self.names_path):
            return
        try:
            with open(self.names_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            for aid_str, name in saved.items():
                aid = int(aid_str)
                if aid in self.sensors:
                    self.sensors[aid]["name"] = name
        except Exception as e:
            log.warning("Sensor-Namen laden fehlgeschlagen: %s", e)

    def set_sensor_name(self, aid: int, name: str) -> bool:
        if aid not in self.sensors:
            return False
        self.sensors[aid]["name"] = name
        saved = {}
        if os.path.exists(self.names_path):
            try:
                with open(self.names_path, encoding="utf-8") as fh:
                    saved = json.load(fh)
            except Exception:
                saved = {}
        saved[str(aid)] = name
        with open(self.names_path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh, indent=2, ensure_ascii=False)
        return True

    def _kick(self) -> None:
        self._wake.set()

    # ---- Bedien-API ------------------------------------------------------
    def set_off(self) -> None:
        self.mode = "off"
        self.program = None
        self.resting = False
        self._kick()

    def set_manual(self, aid: int, iid: int, value: bool) -> None:
        if self.fault:
            return   # verriegelt: erst quittieren
        self.mode = "manual"
        self.program = None
        self.manual[(aid, iid)] = value
        self._kick()

    def _trip_fault(self, reason: str) -> None:
        if not self.fault:
            log.critical("NOT-AUS verriegelt: %s", reason)
        self.fault = True
        self.fault_reason = reason

    def clear_fault(self) -> None:
        self.fault = False
        self.fault_reason = None
        self._heater_on_since.clear()
        for k in self.manual:
            self.manual[k] = False
        log.info("Not-Aus quittiert (zurückgesetzt)")
        self._kick()

    def start_program(self, name: str) -> bool:
        if self.fault:
            return False   # verriegelt: erst quittieren
        prog = self.store.get(name)
        if prog is None:
            return False
        self.program = prog
        self.phase_index = 0
        self._phase_started = time.monotonic()
        self._program_started = time.time()
        self.mode = "program"
        self.resting = False
        self._hum_hist.clear()
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
            for key in ("temp_iid", "hum_iid", "batt_iid"):
                if s[key]:
                    points.append((aid, s[key]))
        if not points:
            return
        try:
            vals = await self.hk.get_values(points)
            for aid, s in self.sensors.items():
                for key, field in (("temp_iid", "temp"), ("hum_iid", "hum"), ("batt_iid", "batt")):
                    if s[key] and (aid, s[key]) in vals:
                        v = vals[(aid, s[key])]
                        if v is not None:
                            s[field] = round(float(v), 1) if field != "batt" else int(v)
            self._aggregate()
            if self.agg_hum is not None:
                nowm = time.monotonic()
                self._hum_hist.append((nowm, self.agg_hum))
                span = max(self.cfg.rate_window_min * 60, self.cfg.fan_stall_h * 3600)
                cutoff = nowm - (span + 120)
                while self._hum_hist and self._hum_hist[0][0] < cutoff:
                    self._hum_hist.popleft()
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

    def _drop_over(self, span_s: float):
        """(Abfall %-Punkte, gemessene Zeitspanne s) über ~span_s; None wenn zu wenig Daten."""
        if len(self._hum_hist) < 2:
            return None
        now = time.monotonic()
        oldest = next(((ts, hv) for ts, hv in self._hum_hist if now - ts <= span_s), None)
        if oldest is None:
            return None
        newest_ts, newest_h = self._hum_hist[-1]
        dt = newest_ts - oldest[0]
        if dt < span_s * 0.6:          # noch nicht genug Zeitspanne gemessen
            return None
        return (oldest[1] - newest_h, dt)   # positiv = gefallen

    def _humidity_drop_rate(self) -> float | None:
        """Feuchte-Abfall in %-Punkten/Stunde über das Messfenster (positiv = fallend)."""
        r = self._drop_over(self.cfg.rate_window_min * 60)
        return None if r is None else r[0] / (r[1] / 3600)

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

        # Sicherheit 1: zu heiss -> Heizungen hart aus (in jedem Modus)
        self.safety_tripped = False
        if self.max_temp_seen is not None and self.max_temp_seen >= self.cfg.max_temp:
            self.safety_tripped = True
            self.heater_on = False
            for h in self.cfg.heaters:
                self.desired[h.point()] = False

        # Sicherheit 2: Heizung-Dauerlauf-Wächter (VERRIEGELND) — läuft eine Heizung
        # länger als heater_max_on am Stück, ist etwas defekt -> Not-Aus, bleibt aus.
        nowm = time.monotonic()
        for hch in self.cfg.heaters:
            p = hch.point()
            if self.desired.get(p):
                self._heater_on_since.setdefault(p, nowm)
                if nowm - self._heater_on_since[p] > self.cfg.heater_max_on * 60:
                    self._trip_fault(f"{hch.name} lief länger als {self.cfg.heater_max_on:.0f} min am Stück")
            else:
                self._heater_on_since.pop(p, None)

        if self.fault:   # verriegelt: alles aus, Programm gestoppt, bleibt aus
            self.mode = "off"
            self.program = None
            self.heater_on = False
            self.venting = False
            self._heater_on_since.clear()
            for ch in self.cfg.heaters + self.cfg.fans:
                self.desired[ch.point()] = False

    def _decide_program(self) -> None:
        if not self.program:
            self.set_off()
            return
        self._advance_phase()
        if self.mode != "program":
            return
        phase = self.program.phases[self.phase_index]

        # --- Ideallinie der Phase (Rampe). Die Feuchte soll ihr FOLGEN, nie drunter. ---
        self.humidity_target = self._current_humidity_target(phase)
        floor = self.humidity_target
        low = phase.temp_low if phase.temp_low is not None else self.cfg.temp_low
        high = phase.temp_high if phase.temp_high is not None else self.cfg.temp_high
        t = self.agg_temp
        h = self.agg_hum
        hyst = self.cfg.humidity_hysteresis
        self.drop_rate = self._humidity_drop_rate()   # nur zur Anzeige
        self.allowed_drop = None

        # --- Dynamische Ruhephase: an die Ideallinie gekoppelt ---
        # Erreicht/unterschreitet die Feuchte die Linie -> ALLES aus, bis sie sich
        # wieder über die Linie (+ Reserve) erholt hat. Dauer = dynamisch, nicht fix.
        if floor is not None and h is not None:
            if self.resting:
                if h >= floor + hyst:
                    self.resting = False
            elif h <= floor:
                self.resting = True
                log.info("Ruhephase: Feuchte %.0f%% an Ideallinie %.0f%% – erholen lassen", h, floor)
        if self.resting:
            self.heater_on = False
            self.venting = False
            for ch in self.cfg.heaters + self.cfg.fans:
                self.desired[ch.point()] = False
            return

        # ===== Über der Linie: aktiv trocknen =====
        humidity_ok = floor is None or (h is not None and h > floor)

        # --- Heizung: Band low..high mit Trägheit (Heizung sitzt oben, ~2-3 min bis Wirkung) ---
        nowm = time.monotonic()
        force_off = (t is None) or (not humidity_ok)   # an der Linie -> sofort aus
        if force_off:
            band_on = False
        elif t < low:
            band_on = True
        elif t > high:
            band_on = False
        else:
            band_on = self.heater_on            # im Band: Zustand halten
        desired_heater = band_on
        if not force_off:                       # Mindest-Laufzeit/-Pause respektieren
            elapsed = nowm - self._heater_changed_at
            if self.heater_on and not desired_heater and elapsed < self.cfg.heater_min_on * 60:
                desired_heater = True
            elif not self.heater_on and desired_heater and elapsed < self.cfg.heater_min_off * 60:
                desired_heater = False
        if desired_heater != self.heater_on:
            self._heater_changed_at = nowm
        self.heater_on = desired_heater

        # --- Lüfter: Notnagel nur bei STILLSTAND (>fan_stall_h ohne Abfall) ---
        if floor is not None and h is not None:
            stall = self._drop_over(self.cfg.fan_stall_h * 3600)
            stalled = stall is not None and stall[0] < self.cfg.fan_stall_drop
            if stalled and h > floor + hyst:
                self.venting = True
            elif h <= floor + hyst:
                self.venting = False
        else:
            self.venting = False

        # --- Seiten IMMER abwechseln: gilt für Heizung UND Lüfter (links/rechts) ---
        n = max(len(self.cfg.heaters), len(self.cfg.fans), 1)
        if time.monotonic() - self._fan_cycle_started >= self.cfg.fan_cycle_min * 60:
            self.active_side = (self.active_side + 1) % n
            self._fan_cycle_started = time.monotonic()
        for i, hch in enumerate(self.cfg.heaters):
            self.desired[hch.point()] = self.heater_on and (i == self.active_side)
        for i, f in enumerate(self.cfg.fans):
            self.desired[f.point()] = self.venting and (i == self.active_side)

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
                {"aid": aid, "name": s["name"], "temp": s["temp"], "hum": s["hum"], "batt": s["batt"]}
                for aid, s in sorted(self.sensors.items())
            ],
            "agg_temp": self.agg_temp,
            "agg_hum": self.agg_hum,
            "aggregate": self.cfg.aggregate,
            "temp_low": self.cfg.temp_low,
            "temp_high": self.cfg.temp_high,
            "heater_on": self.heater_on,
            "venting": self.venting,
            "active_side": self.active_side,
            "resting": self.resting,
            "rest_recover_to": round(self.humidity_target + self.cfg.humidity_hysteresis, 1)
                if (self.resting and self.humidity_target is not None) else None,
            "drop_rate": round(self.drop_rate, 2) if self.drop_rate is not None else None,
            "heaters": [{"name": h.name, "on": self.desired.get(h.point(), False),
                         "aid": h.aid, "iid": h.iid} for h in self.cfg.heaters],
            "fans": [{"name": f.name, "on": self.desired.get(f.point(), False),
                      "aid": f.aid, "iid": f.iid} for f in self.cfg.fans],
            "reading_ok": self.last_reading_ok,
            "reading_age": (time.time() - self.last_reading_at) if self.last_reading_at else None,
            "safety_tripped": self.safety_tripped,
            "fault": self.fault,
            "fault_reason": self.fault_reason,
            "max_temp": self.cfg.max_temp,
            "heater_max_on": self.cfg.heater_max_on,
            "program": self.program.name if self.program else None,
            "phase": phase,
            "phase_remaining": phase_remaining,
            "program_started": self._program_started,
            "error": self.last_error,
            "programs": self.store.names(),
        }
