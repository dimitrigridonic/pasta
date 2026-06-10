"""Verlaufs-Aufzeichnung der Sensorwerte in SQLite (für Auswertung nach dem Lauf)."""
from __future__ import annotations

import io
import sqlite3
import threading


class History:
    def __init__(self, path: str, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self.enabled:
            return
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS readings (ts REAL, aid INTEGER, temp REAL, hum REAL)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON readings (ts)")
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def log(self, ts: float, sensors: dict[int, dict]) -> None:
        """sensors: {aid: {'temp': x, 'hum': y, 'name': ...}}"""
        if not self.enabled or not self._conn:
            return
        rows = [(ts, aid, s.get("temp"), s.get("hum")) for aid, s in sensors.items()]
        with self._lock:
            self._conn.executemany("INSERT INTO readings VALUES (?,?,?,?)", rows)
            self._conn.commit()

    def series(self, since: float | None = None) -> dict:
        """Liefert {aid: [[ts, temp, hum], ...]} für die Diagramm-Darstellung."""
        if not self.enabled or not self._conn:
            return {}
        q = "SELECT ts, aid, temp, hum FROM readings"
        params: tuple = ()
        if since is not None:
            q += " WHERE ts >= ?"
            params = (since,)
        q += " ORDER BY ts"
        out: dict[int, list] = {}
        with self._lock:
            for ts, aid, temp, hum in self._conn.execute(q, params):
                out.setdefault(aid, []).append([ts, temp, hum])
        return out

    def csv(self, since: float | None = None) -> str:
        if not self.enabled or not self._conn:
            return "ts,aid,temp,hum\n"
        q = "SELECT ts, aid, temp, hum FROM readings"
        params: tuple = ()
        if since is not None:
            q += " WHERE ts >= ?"
            params = (since,)
        q += " ORDER BY ts"
        buf = io.StringIO()
        buf.write("ts,aid,temp,hum\n")
        with self._lock:
            for ts, aid, temp, hum in self._conn.execute(q, params):
                buf.write(f"{ts},{aid},{temp},{hum}\n")
        return buf.getvalue()

    def clear(self) -> None:
        if not self.enabled or not self._conn:
            return
        with self._lock:
            self._conn.execute("DELETE FROM readings")
            self._conn.commit()
