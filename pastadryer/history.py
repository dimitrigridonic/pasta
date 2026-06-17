"""Verlaufs-Aufzeichnung der Sensorwerte in SQLite (für Auswertung nach dem Lauf).

Pro Logpunkt werden alle Sensoren als eigene Zeile gespeichert, plus (optional)
der laufende Programmname und die Ideallinie (humidity_target) zu diesem Zeitpunkt
— damit das Analyse-Tab die echte Kurve gegen die Soll-Linie zeigen kann.
"""
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
            "CREATE TABLE IF NOT EXISTS readings "
            "(ts REAL, aid TEXT, temp REAL, hum REAL, prog TEXT, target REAL)"
        )
        # Migration alter DBs: fehlende Spalten ergänzen
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(readings)")]
        if "prog" not in cols:
            self._conn.execute("ALTER TABLE readings ADD COLUMN prog TEXT")
        if "target" not in cols:
            self._conn.execute("ALTER TABLE readings ADD COLUMN target REAL")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON readings (ts)")
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def log(self, ts: float, sensors: dict, prog: str | None = None,
            target: float | None = None) -> None:
        """sensors: {aid: {'temp': x, 'hum': y, ...}}; prog/target = Programm + Ideallinie."""
        if not self.enabled or not self._conn:
            return
        rows = [(ts, aid, s.get("temp"), s.get("hum"), prog, target)
                for aid, s in sensors.items()]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO readings (ts, aid, temp, hum, prog, target) VALUES (?,?,?,?,?,?)",
                rows)
            self._conn.commit()

    def runs(self, gap_s: float = 1800, limit: int = 60) -> list[dict]:
        """Erkennt Durchgänge anhand von Zeitlücken (> gap_s = neuer Lauf).
        Neueste zuerst."""
        if not self.enabled or not self._conn:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, MAX(prog) FROM readings GROUP BY ts ORDER BY ts").fetchall()
        runs: list[dict] = []
        cur: dict | None = None
        for ts, prog in rows:
            if cur is None or ts - cur["end"] > gap_s:
                cur = {"start": ts, "end": ts, "points": 1, "prog": prog}
                runs.append(cur)
            else:
                cur["end"] = ts
                cur["points"] += 1
                if prog and not cur["prog"]:
                    cur["prog"] = prog
        runs.reverse()
        return runs[:limit]

    def run_series(self, start: float, end: float) -> dict:
        """Detail-Serien eines Durchgangs: Mittelwert-Kurve (+ Ideallinie) und je Sensor."""
        if not self.enabled or not self._conn:
            return {"start": start, "end": end, "agg": [], "sensors": {}}
        with self._lock:
            agg = self._conn.execute(
                "SELECT ts, AVG(temp), AVG(hum), MAX(target) FROM readings "
                "WHERE ts BETWEEN ? AND ? GROUP BY ts ORDER BY ts", (start, end)).fetchall()
            rows = self._conn.execute(
                "SELECT ts, aid, temp, hum FROM readings "
                "WHERE ts BETWEEN ? AND ? ORDER BY ts", (start, end)).fetchall()
        per: dict[str, list] = {}
        for ts, aid, temp, hum in rows:
            per.setdefault(str(aid), []).append([ts, temp, hum])
        agg_out = [[ts,
                    round(t, 2) if t is not None else None,
                    round(h, 2) if h is not None else None,
                    tg] for ts, t, h, tg in agg]
        return {"start": start, "end": end, "agg": agg_out, "sensors": per}

    def series(self, since: float | None = None) -> dict:
        """Liefert {aid: [[ts, temp, hum], ...]} (Kompatibilität)."""
        if not self.enabled or not self._conn:
            return {}
        q = "SELECT ts, aid, temp, hum FROM readings"
        params: tuple = ()
        if since is not None:
            q += " WHERE ts >= ?"
            params = (since,)
        q += " ORDER BY ts"
        out: dict[str, list] = {}
        with self._lock:
            for ts, aid, temp, hum in self._conn.execute(q, params):
                out.setdefault(str(aid), []).append([ts, temp, hum])
        return out

    def csv(self, since: float | None = None, until: float | None = None) -> str:
        if not self.enabled or not self._conn:
            return "ts,aid,temp,hum,prog,target\n"
        q = "SELECT ts, aid, temp, hum, prog, target FROM readings"
        conds, params = [], []
        if since is not None:
            conds.append("ts >= ?")
            params.append(since)
        if until is not None:
            conds.append("ts <= ?")
            params.append(until)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts"
        buf = io.StringIO()
        buf.write("ts,aid,temp,hum,prog,target\n")
        with self._lock:
            for ts, aid, temp, hum, prog, target in self._conn.execute(q, tuple(params)):
                buf.write(f"{ts},{aid},{temp},{hum},{prog or ''},{target if target is not None else ''}\n")
        return buf.getvalue()

    def clear(self) -> None:
        if not self.enabled or not self._conn:
            return
        with self._lock:
            self._conn.execute("DELETE FROM readings")
            self._conn.commit()
