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
from .zb import Zigbee
from .programs import ProgramStore

log = logging.getLogger(__name__)


class ControlLoop:
    def __init__(self, zb: Zigbee, cfg: Config, history: History,
                 store: ProgramStore, names_path: str = "sensor_names.json"):
        self.zb = zb
        self.cfg = cfg
        self.history = history
        self.store = store
        self.names_path = names_path

        # Sensoren: friendly_name -> {name, temp, hum, batt}
        self.sensors: dict[str, dict] = {}
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
        # Manuelle Eingriffe WÄHREND eines Programms: Kanal-Override, Programm läuft weiter.
        # Zeitlich begrenzt (override_max_min) -> danach automatisch aus.
        self.overrides: dict[tuple, bool] = {}
        self._override_since: dict[tuple, float] = {}

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
        self.preheating = False          # Vorheizphase (leerer Kasten)
        self._preheat_started: float | None = None
        self.humidity_target: float | None = None
        self.humidity_trim = 0.0   # Live-Versatz auf das Feuchte-Ziel (− = schneller, + = sanfter)
        # Feuchte-Referenz: "all" = Schnitt aller Sensoren, "guide" = nur Leit-Sensoren
        # (z.B. untere Reihe, die zuerst übertrocknet). Live umschaltbar.
        self.hum_ref = "guide" if cfg.humidity_guide else "all"
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
        self._read_lock = asyncio.Lock()   # serialisiert Sensor-Reads

    # ---- Lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self._init_sensors()
        self._running = True
        self._fan_cycle_started = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task:
            await self._task
        await self._all_off()

    def _init_sensors(self) -> None:
        """Sensoren aus der Konfiguration (z2m friendly_names) anlegen."""
        for sc in self.cfg.sensors:
            self.sensors[sc.name] = {
                "name": sc.name, "temp": None, "hum": None, "batt": None,
            }
        self._apply_saved_names()
        log.info("Sensoren (z2m): %s", list(self.sensors))

    def _apply_saved_names(self) -> None:
        """Optionaler Anzeige-Alias je friendly_name (sensor_names.json)."""
        if not os.path.exists(self.names_path):
            return
        try:
            with open(self.names_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            for key, name in saved.items():
                if key in self.sensors:
                    self.sensors[key]["name"] = name
        except Exception as e:
            log.warning("Sensor-Namen laden fehlgeschlagen: %s", e)

    def set_sensor_name(self, key: str, name: str) -> bool:
        if key not in self.sensors:
            return False
        self.sensors[key]["name"] = name
        saved = {}
        if os.path.exists(self.names_path):
            try:
                with open(self.names_path, encoding="utf-8") as fh:
                    saved = json.load(fh)
            except Exception:
                saved = {}
        saved[key] = name
        with open(self.names_path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh, indent=2, ensure_ascii=False)
        return True

    def _kick(self) -> None:
        self._wake.set()

    async def read_once(self) -> None:
        """Einmalige Sensor-Abfrage auf Knopfdruck (auch im Standby)."""
        await self._read_sensors()

    # ---- Bedien-API ------------------------------------------------------
    def set_off(self) -> None:
        self.mode = "off"
        self.program = None
        self.resting = False
        self.overrides.clear()
        for k in self.manual:        # Manuell-Schalter zurücksetzen (sauberer Reset)
            self.manual[k] = False
        self._kick()

    def enter_manual(self) -> None:
        """Manuell-Modus betreten: sauberer Start, alle Schalter aus."""
        if self.fault:
            return
        self.mode = "manual"
        self.program = None
        self.resting = False
        for k in self.manual:
            self.manual[k] = False
        self._kick()

    def set_manual(self, aid: str, iid: str, value: bool) -> None:
        if self.fault:
            return   # verriegelt: erst quittieren
        if self.mode == "program":
            # Eingriff während des Programms: NICHT stoppen, nur diesen Kanal
            # überschreiben. Das Programm (Phasen/Zeit) läuft normal weiter.
            # Zeitlich begrenzt -> nach override_max_min automatisch wieder aus.
            self.overrides[(aid, iid)] = value
            self._override_since[(aid, iid)] = time.monotonic()
            self._kick()
            return
        self.mode = "manual"
        self.program = None
        self.manual[(aid, iid)] = value
        self._kick()

    def clear_overrides(self) -> None:
        """Alle manuellen Eingriffe aufheben – das Programm steuert wieder alles."""
        self.overrides.clear()
        self._override_since.clear()
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
        self.humidity_trim = 0.0
        self.overrides.clear()
        self._hum_hist.clear()
        self.preheating = self.cfg.preheat_enabled
        self._preheat_started = time.monotonic()
        self._kick()
        return True

    def nudge_humidity(self, delta: float) -> None:
        """Live-Versatz auf das Feuchte-Ziel (− = schneller trocknen, + = sanfter).
        Gilt für den Rest des Laufs; clamped auf ±15 %."""
        if self.mode != "program":
            return
        self.humidity_trim = round(max(-15.0, min(15.0, self.humidity_trim + delta)), 1)
        log.info("Feuchte-Ziel-Versatz jetzt %+.0f%%", self.humidity_trim)
        self._kick()

    def skip_phase(self) -> None:
        if self.mode == "program" and self.program:
            if self.preheating:
                self.preheating = False
                self.phase_index = 0
                self._phase_started = time.monotonic()
            else:
                self._advance_phase(force=True)
            self._kick()

    def set_hum_ref(self, mode: str) -> None:
        """Feuchte-Referenz live umschalten: 'all' (Schnitt) | 'guide' (Leit-Sensoren)."""
        if mode in ("all", "guide"):
            self.hum_ref = mode
            log.info("Feuchte-Referenz jetzt: %s", mode)
            self._kick()

    def resume_program(self, name: str, phase_index: int, elapsed_s: float) -> bool:
        """Laufenden Lauf nach einem Neustart wiederaufnehmen (OHNE Vorheizen):
        Phase + bereits verstrichene Zeit in der Phase wiederherstellen."""
        if self.fault:
            return False
        prog = self.store.get(name)
        if prog is None:
            return False
        self.program = prog
        self.phase_index = max(0, min(int(phase_index), len(prog.phases) - 1))
        nowm = time.monotonic()
        self._phase_started = nowm - max(0.0, float(elapsed_s))
        prior = sum((p.duration_h or 0) for p in prog.phases[:self.phase_index]) * 3600 + max(0.0, float(elapsed_s))
        self._program_started = time.time() - prior
        self.mode = "program"
        self.preheating = False
        self.resting = False
        self.humidity_trim = 0.0
        self.overrides.clear()
        self._hum_hist.clear()
        self.active_side = 0
        self._fan_cycle_started = nowm
        log.info("Programm '%s' wiederaufgenommen: Phase %d, %.0f min in der Phase",
                 name, self.phase_index, float(elapsed_s) / 60)
        self._kick()
        return True

    # ---- Hauptschleife ---------------------------------------------------
    async def _run(self) -> None:
        log.info("Control-Loop gestartet (Intervall %ss)", self.cfg.poll_interval)
        while self._running:
            try:
                # Push-Daten aus dem MQTT-Cache lesen (kostet keine Batterie)
                await self._read_sensors()
                self._decide()
                await self._apply()
                if self.mode == "program":
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
        """Letzte Push-Werte aus dem z2m/MQTT-Cache übernehmen."""
        try:
            for name, s in self.sensors.items():
                d = self.zb.get(name)
                if not d:
                    continue
                if d.get("temperature") is not None:
                    s["temp"] = round(float(d["temperature"]), 1)
                if d.get("humidity") is not None:
                    s["hum"] = round(float(d["humidity"]), 1)
                if d.get("battery") is not None:
                    s["batt"] = int(d["battery"])
            self._aggregate()
            if self.agg_hum is not None:
                nowm = time.monotonic()
                self._hum_hist.append((nowm, self.agg_hum))
                span = max(self.cfg.rate_window_min * 60, self.cfg.fan_stall_h * 3600)
                cutoff = nowm - (span + 120)
                while self._hum_hist and self._hum_hist[0][0] < cutoff:
                    self._hum_hist.popleft()
            self.last_reading_ok = any(s["temp"] is not None or s["hum"] is not None
                                       for s in self.sensors.values())
            self.last_reading_at = time.time()
            self.last_error = None
        except Exception as e:
            self.last_reading_ok = False
            self.last_error = f"Sensoren lesen: {e}"
            log.warning("Sensorlesen fehlgeschlagen: %s", e)

    def _aggregate(self) -> None:
        temps = [s["temp"] for s in self.sensors.values() if s["temp"] is not None]
        # Feuchte-Referenz: alle Sensoren ODER nur die Leit-Sensoren (z.B. untere Reihe)
        if self.hum_ref == "guide" and self.cfg.humidity_guide:
            names = [n for n in self.cfg.humidity_guide if n in self.sensors]
            hums = [self.sensors[n]["hum"] for n in names if self.sensors[n]["hum"] is not None]
        else:
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

    def _side_hum(self, side: int) -> float | None:
        """Mittlere Feuchte einer Seite (0=links, 1=rechts) aus den zugeordneten Sensoren."""
        names = self.cfg.sides_right if side == 1 else self.cfg.sides_left
        vals = [self.sensors[n]["hum"] for n in names
                if n in self.sensors and self.sensors[n]["hum"] is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def _pick_side(self, prev_side: int, nowm: float) -> int:
        """Welche Seite heizen/lüften? Die FEUCHTERE Seite gewinnt; nur bei etwa
        gleicher Feuchte wird im Takt abgewechselt (gleichmäßige Trocknung)."""
        n = max(len(self.cfg.heaters), len(self.cfg.fans), 1)
        if n < 2:
            return 0
        # SICHERHEIT zuerst: nie länger als fan_cycle_min am Stück auf derselben Seite,
        # sonst läuft eine Heizung über den Dauerlauf-Wächter (heater_max_on). Diese
        # Zwangs-Abwechslung hat Vorrang vor der Feuchte-Präferenz. Die trockene Seite
        # wird dabei trotzdem nicht geheizt – das Feuchte-Gate in _decide_program lässt
        # sie aus, sodass die feuchtere Seite nur kurz pausiert statt durchzulaufen.
        if nowm - self._fan_cycle_started >= self.cfg.fan_cycle_min * 60:
            return (prev_side + 1) % n
        hl, hr = self._side_hum(0), self._side_hum(1)
        if hl is not None and hr is not None and abs(hl - hr) >= self.cfg.side_bias_min:
            return 0 if hl > hr else 1          # sonst: feuchtere Seite bevorzugen
        return prev_side

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

        # Manueller Eingriff ist ZEITLICH BEGRENZT: nach override_max_min automatisch
        # beenden -> Kanal geht zurück ans Programm (schützt vergessene Heizer/Lüfter).
        if self.overrides:
            nowm = time.monotonic()
            for k in list(self.overrides):
                if nowm - self._override_since.get(k, nowm) > self.cfg.override_max_min * 60:
                    self.overrides.pop(k, None)
                    self._override_since.pop(k, None)
                    log.info("Manueller Eingriff %s nach %.0f min automatisch beendet",
                             k, self.cfg.override_max_min)

        # Verbleibende Eingriffe über das LAUFENDE Programm legen – Programm läuft weiter.
        # (Sicherheiten unten greifen weiterhin, auch über einen Override.)
        if self.mode == "program" and self.overrides:
            for k, v in self.overrides.items():
                self.desired[k] = v
            self.heater_on = any(self.desired.get(h.point()) for h in self.cfg.heaters)
            self.venting = any(self.desired.get(f.point()) for f in self.cfg.fans)

        # Sicherheit 1: zu heiss -> Heizungen hart aus (in jedem Modus)
        self.safety_tripped = False
        if self.max_temp_seen is not None and self.max_temp_seen >= self.cfg.max_temp:
            self.safety_tripped = True
            self.heater_on = False
            for h in self.cfg.heaters:
                self.desired[h.point()] = False

        # Sicherheit 2: Heizung-Dauerlauf-Wächter (VERRIEGELND) — läuft eine Heizung
        # länger als heater_max_on am Stück, ist etwas defekt -> Not-Aus, bleibt aus.
        # Beim Vorheizen pausiert: beide Heizungen laufen absichtlich durch (leerer
        # Kasten), abgesichert über max_temp-Hartabschaltung + preheat_max_min.
        nowm = time.monotonic()
        if self.preheating:
            self._heater_on_since.clear()
        else:
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

    def _decide_preheat(self) -> None:
        """Vorheizen bei LEEREM Kasten, ZEITBASIERT (kein Sensor im Kasten):
        BEIDE Heizungen volle Leistung für preheat_min Minuten, dann Programmstart.
        Begrenzt rein über die Zeit. Falls trotzdem ein Sensor im Kasten ist, greift
        die max_temp-Abschaltung in _decide zusätzlich."""
        elapsed = time.monotonic() - (self._preheat_started or time.monotonic())
        if elapsed >= self.cfg.preheat_min * 60:
            self.preheating = False
            self.phase_index = 0
            self._phase_started = time.monotonic()
            self.active_side = 0                        # Programm startet sauber links
            self._fan_cycle_started = time.monotonic()  # voller erster Seiten-Takt
            log.info("Vorheizen fertig (zeitbasiert, %.0f min) -> Programm startet", elapsed / 60)
            return
        self.heater_on = True
        self.venting = False
        # leerer Kasten -> beide Heizungen an (kein Wechsel)
        for hch in self.cfg.heaters:
            self.desired[hch.point()] = True
        for f in self.cfg.fans:
            self.desired[f.point()] = False

    def _decide_program(self) -> None:
        if not self.program:
            self.set_off()
            return
        if self.preheating:
            self._decide_preheat()
            return
        self._advance_phase()
        if self.mode != "program":
            return
        phase = self.program.phases[self.phase_index]

        # --- Ideallinie der Phase (Rampe). Die Feuchte soll ihr FOLGEN, nie drunter. ---
        self.humidity_target = self._current_humidity_target(phase)
        if self.humidity_target is not None and self.humidity_trim:
            self.humidity_target = round(min(95.0, max(20.0, self.humidity_target + self.humidity_trim)), 1)
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
        nowm = time.monotonic()

        # --- Heizseite wählen: die FEUCHTERE Seite gewinnt (nicht stur abwechseln) ---
        new_side = self._pick_side(self.active_side, nowm)
        if new_side != self.active_side:
            self.active_side = new_side
            self._fan_cycle_started = nowm

        # Feuchte-Gate auf die AKTIVE Seite: eine schon trockene Seite NICHT heizen,
        # auch wenn sie laut Takt „dran" wäre. Fällt zurück auf den Gesamtwert,
        # falls die Seite (noch) keine Sensordaten hat.
        h_side = self._side_hum(self.active_side)
        h_gate = h_side if h_side is not None else h
        humidity_ok = floor is None or (h_gate is not None and h_gate > floor)

        # --- Heizung: Band low..high mit Trägheit (Heizung sitzt oben, ~2-3 min bis Wirkung) ---
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

        # --- Stellglieder auf die gewählte aktive Seite legen (Heizung UND Lüfter) ---
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
                await self.zb.set_state(point[0], point[1], value)
                self._written[point] = value
                log.info("%s -> %s", point, "AN" if value else "AUS")
            except Exception as e:
                self.last_error = f"schalten {point}: {e}"
                log.warning("Schalten %s fehlgeschlagen: %s", point, e)

    async def _all_off(self) -> None:
        for ch in self.cfg.heaters + self.cfg.fans:
            try:
                await self.zb.set_state(ch.aid, ch.iid, False)
            except Exception:
                pass

    def _maybe_log(self) -> None:
        now = time.time()
        if now - self._last_log >= self.cfg.log_interval:
            self._last_log = now
            prog = self.program.name if self.program else None
            self.history.log(now, self.sensors, prog=prog, target=self.humidity_target)

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
                {"aid": key, "name": s["name"], "temp": s["temp"], "hum": s["hum"], "batt": s["batt"]}
                for key, s in sorted(self.sensors.items())
            ],
            "agg_temp": self.agg_temp,
            "agg_hum": self.agg_hum,
            "aggregate": self.cfg.aggregate,
            "temp_low": self.cfg.temp_low,
            "temp_high": self.cfg.temp_high,
            "heater_on": self.heater_on,
            "venting": self.venting,
            "active_side": self.active_side,
            "hum_left": self._side_hum(0),
            "hum_right": self._side_hum(1),
            "resting": self.resting,
            "rest_recover_to": round(self.humidity_target + self.cfg.humidity_hysteresis, 1)
                if (self.resting and self.humidity_target is not None) else None,
            "drop_rate": round(self.drop_rate, 2) if self.drop_rate is not None else None,
            "heaters": [{"name": h.name, "on": self.desired.get(h.point(), False),
                         "aid": h.aid, "iid": h.iid} for h in self.cfg.heaters],
            "fans": [{"name": f.name, "on": self.desired.get(f.point(), False),
                      "aid": f.aid, "iid": f.iid} for f in self.cfg.fans],
            "reading_ok": self.last_reading_ok,
            # Alter des FRISCHESTEN Sensor-Push (echte Push-Zeit, KEINE Abfrage).
            "reading_age": min([a for a in (self.zb.age(n) for n in self.sensors)
                                if a is not None], default=None),
            "mqtt_connected": self.zb.connected,
            "sensors_active": True,
            "safety_tripped": self.safety_tripped,
            "fault": self.fault,
            "fault_reason": self.fault_reason,
            "max_temp": self.cfg.max_temp,
            "heater_max_on": self.cfg.heater_max_on,
            "program": self.program.name if self.program else None,
            "humidity_trim": round(self.humidity_trim, 1),
            "overrides": [{"aid": k[0], "iid": k[1], "on": v,
                           "remaining": max(0, int(self.cfg.override_max_min * 60
                                       - (time.monotonic() - self._override_since.get(k, time.monotonic()))))}
                          for k, v in self.overrides.items()],
            "hum_ref": self.hum_ref,
            "humidity_guide": self.cfg.humidity_guide,
            "preheating": self.preheating,
            "preheat": ({
                "total_min": self.cfg.preheat_min,
                "remaining": (max(0, int(self.cfg.preheat_min * 60
                              - (time.monotonic() - self._preheat_started)))
                              if self._preheat_started else None),
            } if self.preheating else None),
            "phase": phase,
            "phase_remaining": phase_remaining,
            "program_started": self._program_started,
            "error": self.last_error,
            "programs": self.store.names(),
        }
