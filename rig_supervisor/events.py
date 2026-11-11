"""Persistent event log (state changes, alarms, operator actions)."""

from __future__ import annotations

import collections
import logging
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

LEVELS = ("info", "warning", "error")


class EventStore:
    def __init__(self, path: str, max_events: int = 50000):
        self.path = path
        self.max_events = max_events
        self._lock = threading.Lock()
        self._recent = collections.deque(maxlen=100)
        self._listeners = []
        self._inserts = 0
        self._conn = None
        try:
            self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL, level TEXT NOT NULL, code TEXT, message TEXT NOT NULL, source TEXT)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT id, ts, level, code, message, source FROM events ORDER BY id DESC LIMIT 100").fetchall()
            for row in reversed(rows):
                self._recent.append(self._row(row))
        except sqlite3.Error as exc:  # keep running with an in-memory log only
            log.error("Az eseménynapló adatbázis nem nyitható meg (%s): %s", path, exc)
            self._conn = None

    @staticmethod
    def _row(row) -> dict:
        return {"id": row[0], "ts": row[1], "level": row[2], "code": row[3] or "",
                "message": row[4], "source": row[5] or ""}

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)

    def add(self, level: str, message: str, code: str = "", source: str = "") -> dict:
        if level not in LEVELS:
            level = "info"
        ts = time.time()
        event = {"id": None, "ts": ts, "level": level, "code": code or "", "message": message,
                 "source": source or ""}
        log_fn = {"info": log.info, "warning": log.warning, "error": log.error}[level]
        log_fn("[%s] %s%s", source or "rendszer", (code + " ") if code else "", message)
        with self._lock:
            if self._conn is not None:
                try:
                    cur = self._conn.execute(
                        "INSERT INTO events(ts, level, code, message, source) VALUES (?,?,?,?,?)",
                        (ts, level, code, message, source))
                    self._conn.commit()
                    event["id"] = cur.lastrowid
                    self._inserts += 1
                    if self._inserts % 500 == 0:
                        self._conn.execute(
                            "DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?",
                            (self.max_events,))
                        self._conn.commit()
                except sqlite3.Error as exc:
                    log.error("Esemény mentése sikertelen: %s", exc)
            if event["id"] is None:
                event["id"] = -int(ts * 1000)
            self._recent.append(event)
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                log.exception("Esemény figyelő hiba")
        return event

    def recent(self, n: int = 20) -> list:
        with self._lock:
            items = list(self._recent)[-n:]
        return list(reversed(items))

    def query(self, limit: int = 100, before_id=None, level=None) -> list:
        limit = max(1, min(int(limit), 1000))
        sql = "SELECT id, ts, level, code, message, source FROM events"
        cond, args = [], []
        if before_id is not None:
            cond.append("id < ?")
            args.append(int(before_id))
        if level in ("warning", "error"):
            cond.append("level IN (%s)" % ("'warning','error'" if level == "warning" else "'error'"))
        elif level == "info":
            cond.append("level = 'info'")
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            if self._conn is None:
                return self.recent(limit)
            try:
                rows = self._conn.execute(sql, args).fetchall()
            except sqlite3.Error as exc:
                log.error("Eseménynapló lekérdezés sikertelen: %s", exc)
                return []
        return [self._row(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
