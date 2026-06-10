"""Konfiguration laden (config.yaml)."""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml


@dataclass
class Channel:
    """Ein schaltbarer Relais-Kanal (Heizung oder Lüfter)."""
    name: str
    aid: int
    iid: int

    @classmethod
    def parse(cls, d: dict) -> "Channel":
        return cls(name=d.get("name", "?"), aid=int(d["aid"]), iid=int(d["iid"]))

    def point(self) -> tuple[int, int]:
        return (self.aid, self.iid)


@dataclass
class SensorCfg:
    """Optionaler Name für einen Sensor (sonst Auto-Erkennung über aid)."""
    name: str
    aid: int

    @classmethod
    def parse(cls, d: dict) -> "SensorCfg":
        return cls(name=d.get("name", f"Sensor {d['aid']}"), aid=int(d["aid"]))


@dataclass
class Phase:
    name: str
    duration_h: float | None          # None = bis manuell weiter
    humidity_start: float | None
    humidity_end: float | None
    temp_low: float | None = None     # überschreibt globales temp_low/high
    temp_high: float | None = None

    @classmethod
    def parse(cls, d: dict) -> "Phase":
        return cls(
            name=d.get("name", "Phase"),
            duration_h=d.get("duration_h"),
            humidity_start=d.get("humidity_start"),
            humidity_end=d.get("humidity_end", d.get("humidity_start")),
            temp_low=d.get("temp_low"),
            temp_high=d.get("temp_high"),
        )


@dataclass
class Program:
    name: str
    phases: list[Phase]

    @classmethod
    def parse(cls, d: dict) -> "Program":
        return cls(name=d["name"], phases=[Phase.parse(p) for p in d.get("phases", [])])


@dataclass
class Config:
    # HomeKit
    pairing_file: str
    alias: str
    # Web
    host: str
    port: int
    # Regelung
    poll_interval: float
    aggregate: str            # average | max | min  (Sensor-Aggregation für Regelung)
    temp_low: float           # Heizung AN unterhalb
    temp_high: float          # Heizung AUS oberhalb
    max_temp: float           # harte Sicherheits-Abschaltung
    humidity_hysteresis: float
    fan_cycle_min: float      # Wechsel-Takt links/rechts (Minuten)
    min_temp_for_fan: float   # Lüfter erst ab dieser Temp
    drop_tolerance_per_h: float  # erlaubter Feuchte-Abfall ÜBER der Soll-Rampe (%/h)
    rest_min: float           # Dauer einer Ruhephase (Minuten)
    rate_window_min: float    # Fenster zur Messung der Abfall-Geschwindigkeit (Minuten)
    heater_min_on: float      # Mindest-Laufzeit Heizung (Minuten) – Trägheit
    heater_min_off: float     # Mindest-Pause Heizung (Minuten) – beobachten
    fan_stall_h: float        # Lüfter erst, wenn Feuchte so lange (h) nicht fällt
    fan_stall_drop: float     # … und in dieser Zeit weniger als X %-Punkte gefallen ist
    # Geräte
    heaters: list[Channel]
    fans: list[Channel]
    sensors: list[SensorCfg]  # leer = alle automatisch
    # Logging
    log_enabled: bool
    log_file: str
    log_interval: float
    # Programme
    programs: list[Program] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        hk = raw.get("homekit", {})
        web = raw.get("web", {})
        c = raw.get("control", {})
        log = raw.get("logging", {})

        return cls(
            pairing_file=hk.get("pairing_file", "pairing.json"),
            alias=hk.get("alias", "pastadryer"),
            host=web.get("host", "0.0.0.0"),
            port=int(web.get("port", 8000)),
            poll_interval=float(c.get("poll_interval", 15)),
            aggregate=c.get("aggregate", "average"),
            temp_low=float(c.get("temp_low", 30)),
            temp_high=float(c.get("temp_high", 32)),
            max_temp=float(c.get("max_temp", 38)),
            humidity_hysteresis=float(c.get("humidity_hysteresis", 3)),
            fan_cycle_min=float(c.get("fan_cycle_min", 10)),
            min_temp_for_fan=float(c.get("min_temp_for_fan", 0)),
            drop_tolerance_per_h=float(c.get("drop_tolerance_per_h", 1.5)),
            rest_min=float(c.get("rest_min", 120)),
            rate_window_min=float(c.get("rate_window_min", 40)),
            heater_min_on=float(c.get("heater_min_on", 5)),
            heater_min_off=float(c.get("heater_min_off", 5)),
            fan_stall_h=float(c.get("fan_stall_h", 5)),
            fan_stall_drop=float(c.get("fan_stall_drop", 1.0)),
            heaters=[Channel.parse(x) for x in raw.get("heaters", [])],
            fans=[Channel.parse(x) for x in raw.get("fans", [])],
            sensors=[SensorCfg.parse(x) for x in raw.get("sensors", [])],
            log_enabled=bool(log.get("enabled", True)),
            log_file=log.get("file", "history.db"),
            log_interval=float(log.get("interval", 60)),
            programs=[Program.parse(p) for p in raw.get("programs", [])],
        )
