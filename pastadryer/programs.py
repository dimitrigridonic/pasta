"""Editierbare Trocken-Programme, persistiert als JSON (programs.json).

Beim ersten Start aus der config.yaml geseedet; danach im Browser editierbar.
Ein Programm = { name, phases: [ {name, duration_h, humidity_start, humidity_end,
                                  temp_low?, temp_high?}, ... ] }
"""
from __future__ import annotations

import json
import os

from .config import Phase, Program


def _phase_to_dict(ph: Phase) -> dict:
    d = {
        "name": ph.name,
        "duration_h": ph.duration_h,
        "humidity_start": ph.humidity_start,
        "humidity_end": ph.humidity_end,
    }
    if ph.temp_low is not None:
        d["temp_low"] = ph.temp_low
    if ph.temp_high is not None:
        d["temp_high"] = ph.temp_high
    return d


class ProgramStore:
    def __init__(self, path: str, seed: list[Program]):
        self.path = path
        self._seed = seed
        self.programs: list[dict] = []

    def load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.programs = json.load(fh)
        else:
            self.programs = [
                {"name": p.name, "phases": [_phase_to_dict(ph) for ph in p.phases]}
                for p in self._seed
            ]
            self.save()

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.programs, fh, indent=2, ensure_ascii=False)

    def names(self) -> list[str]:
        return [p["name"] for p in self.programs]

    def list(self) -> list[dict]:
        return self.programs

    def get(self, name: str) -> Program | None:
        for p in self.programs:
            if p["name"] == name:
                return Program(name=name, phases=[Phase.parse(ph) for ph in p["phases"]])
        return None

    def upsert(self, name: str, phases: list[dict], old_name: str | None = None) -> None:
        target = old_name or name
        for p in self.programs:
            if p["name"] == target:
                p["name"] = name
                p["phases"] = phases
                self.save()
                return
        self.programs.append({"name": name, "phases": phases})
        self.save()

    def delete(self, name: str) -> bool:
        before = len(self.programs)
        self.programs = [p for p in self.programs if p["name"] != name]
        if len(self.programs) != before:
            self.save()
            return True
        return False
