"""Konfiguration laden (config.yaml). Geräte werden über zigbee2mqtt adressiert:
Relais-Kanal = (device friendly_name, property z.B. state_l1); Sensor = friendly_name.
Intern heissen die Felder weiter aid/iid (= device/property) — minimiert Code-Änderungen.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml


@dataclass
class Channel:
    """Ein schaltbarer Relais-Kanal: aid=z2m-Gerät, iid=Property (state_l1/state_l2)."""
    name: str
    aid: str   # z2m friendly_name, z.B. "Relais links"
    iid: str   # Property, z.B. "state_l1"

    @classmethod
    def parse(cls, d: dict) -> "Channel":
        return cls(name=d.get("name", "?"), aid=str(d["device"]), iid=str(d["prop"]))

    def point(self) -> tuple[str, str]:
        return (self.aid, self.iid)


@dataclass
class SensorCfg:
    name: str   # z2m friendly_name, z.B. "Sensor 1"

    @classmethod
    def parse(cls, d) -> "SensorCfg":
        return cls(name=str(d))


@dataclass
class Phase:
    name: str
    duration_h: float | None
    humidity_start: float | None
    humidity_end: float | None
    temp_low: float | None = None
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
    # MQTT / zigbee2mqtt
    mqtt_host: str
    mqtt_port: int
    # Web
    host: str
    port: int
    # Regelung
    poll_interval: float
    aggregate: str
    temp_low: float
    temp_high: float
    max_temp: float
    humidity_hysteresis: float
    fan_cycle_min: float
    min_temp_for_fan: float
    drop_tolerance_per_h: float
    rest_min: float
    rate_window_min: float
    heater_min_on: float
    heater_min_off: float
    fan_stall_h: float
    fan_stall_drop: float
    heater_max_on: float
    preheat_enabled: bool
    preheat_min: float          # zeitbasiertes Vorheizen: feste Dauer in Minuten
    # Geräte
    heaters: list[Channel]
    fans: list[Channel]
    sensors: list[SensorCfg]
    # Logging
    log_enabled: bool
    log_file: str
    log_interval: float
    # Seiten-Zuordnung (für die intelligente Heizseiten-Wahl): friendly_names je Seite
    sides_left: list[str] = field(default_factory=list)
    sides_right: list[str] = field(default_factory=list)
    side_bias_min: float = 2.5   # ab dieser Feuchte-Differenz (%rF) wird die feuchtere Seite bevorzugt
    humidity_guide: list[str] = field(default_factory=list)  # Leit-Sensoren für die Feuchte-Referenz (leer = alle)
    override_max_min: float = 5  # manueller Eingriff (Heizung/Lüfter erzwingen) läuft max. so lange, dann auto-aus
    # Programme
    programs: list[Program] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        mq = raw.get("mqtt", {})
        web = raw.get("web", {})
        c = raw.get("control", {})
        log = raw.get("logging", {})
        sides = raw.get("sides", {}) or {}

        return cls(
            mqtt_host=mq.get("host", "localhost"),
            mqtt_port=int(mq.get("port", 1883)),
            host=web.get("host", "0.0.0.0"),
            port=int(web.get("port", 8000)),
            poll_interval=float(c.get("poll_interval", 15)),
            aggregate=c.get("aggregate", "average"),
            temp_low=float(c.get("temp_low", 30)),
            temp_high=float(c.get("temp_high", 32)),
            max_temp=float(c.get("max_temp", 38)),
            humidity_hysteresis=float(c.get("humidity_hysteresis", 3)),
            fan_cycle_min=float(c.get("fan_cycle_min", 5)),
            min_temp_for_fan=float(c.get("min_temp_for_fan", 0)),
            drop_tolerance_per_h=float(c.get("drop_tolerance_per_h", 1.5)),
            rest_min=float(c.get("rest_min", 120)),
            rate_window_min=float(c.get("rate_window_min", 40)),
            heater_min_on=float(c.get("heater_min_on", 5)),
            heater_min_off=float(c.get("heater_min_off", 5)),
            fan_stall_h=float(c.get("fan_stall_h", 5)),
            fan_stall_drop=float(c.get("fan_stall_drop", 1.0)),
            heater_max_on=float(c.get("heater_max_on", 6)),
            preheat_enabled=bool(c.get("preheat_enabled", True)),
            preheat_min=float(c.get("preheat_min", c.get("preheat_max_min", 15))),
            heaters=[Channel.parse(x) for x in raw.get("heaters", [])],
            fans=[Channel.parse(x) for x in raw.get("fans", [])],
            sensors=[SensorCfg.parse(x) for x in raw.get("sensors", [])],
            log_enabled=bool(log.get("enabled", True)),
            log_file=log.get("file", "history.db"),
            log_interval=float(log.get("interval", 60)),
            sides_left=[str(x) for x in (sides.get("left") or [])],
            sides_right=[str(x) for x in (sides.get("right") or [])],
            side_bias_min=float(c.get("side_bias_min", 2.5)),
            humidity_guide=[str(x) for x in (raw.get("humidity_guide") or [])],
            override_max_min=float(c.get("override_max_min", 5)),
            programs=[Program.parse(p) for p in raw.get("programs", [])],
        )
