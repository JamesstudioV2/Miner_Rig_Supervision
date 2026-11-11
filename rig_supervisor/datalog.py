"""Time-series data logging.

* One SQLite file per (UTC) day in ``<data_dir>/logs/YYYY-MM-DD.sqlite``.
* Raw samples every ``logging.interval_s`` seconds + 5 minute averages
  (used for long time ranges on the charts).
* When the total size of the log files exceeds ``logging.max_size_gb``
  (default 10 GB) the OLDEST daily files are deleted.  The same happens when
  the free space on the SD card drops below ``logging.min_free_mb``.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import shutil
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

SYS_COLS = ("t1", "t2", "t3", "t4", "flow1", "flow2", "pump1", "pump2", "hashrate", "power", "online", "mtemp")
MINER_COLS = ("hashrate", "temp_chip", "temp_board", "power", "online", "mining")
ROLLUP_S = 300
COMMIT_EVERY_S = 30.0
SIZE_CHECK_EVERY_S = 600.0
KEEP_DAYS_ON_LOW_DISK = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sys (ts INTEGER PRIMARY KEY, state INTEGER, t1 REAL, t2 REAL, t3 REAL, t4 REAL,
  flow1 REAL, flow2 REAL, pump1 REAL, pump2 REAL, hashrate REAL, power REAL, online REAL, mtemp REAL);
CREATE TABLE IF NOT EXISTS sys_5m (ts INTEGER PRIMARY KEY, run REAL, emerg REAL, t1 REAL, t2 REAL, t3 REAL,
  t4 REAL, flow1 REAL, flow2 REAL, pump1 REAL, pump2 REAL, hashrate REAL, power REAL, online REAL, mtemp REAL);
CREATE TABLE IF NOT EXISTS miner (mid TEXT NOT NULL, ts INTEGER NOT NULL, hashrate REAL, temp_chip REAL,
  temp_board REAL, power REAL, online REAL, mining REAL, PRIMARY KEY (mid, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS miner_5m (mid TEXT NOT NULL, ts INTEGER NOT NULL, hashrate REAL, temp_chip REAL,
  temp_board REAL, power REAL, online REAL, mining REAL, PRIMARY KEY (mid, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS miner_names (mid TEXT PRIMARY KEY, name TEXT);
"""


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _avg(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _r(v, nd=3):
    return None if v is None else round(v, nd)


class _Rollup:
    """Accumulates raw samples into 5 minute averages."""

    def __init__(self, bucket: int):
        self.bucket = bucket
        self.sys = {c: [] for c in SYS_COLS}
        self.run = []
        self.emerg = 0.0
        self.miners = {}

    def add(self, row: dict, miners: list) -> None:
        for c in SYS_COLS:
            self.sys[c].append(row.get(c))
        self.run.append(1.0 if row.get("state") == 2 else 0.0)
        if row.get("state") == 4:
            self.emerg = 1.0
        for m in miners:
            acc = self.miners.setdefault(m["mid"], {c: [] for c in MINER_COLS})
            for c in MINER_COLS:
                acc[c].append(m.get(c))


class DataLogger:
    def __init__(self, log_dir: str, config_store, snapshot_fn, events=None):
        self.dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.cfg_store = config_store
        self.snapshot_fn = snapshot_fn
        self.events = events
        self._lock = threading.Lock()
        self._conn = None
        self._conn_day = None
        self._names = {}
        self._rollup = None
        self._last_commit = time.monotonic()
        self._last_size_check = -1e9
        self._last_error_log = -1e9
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.enforce_limits()
        self._last_size_check = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name="datalog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            self._close_conn()

    def _loop(self):
        while True:
            interval = float(self.cfg_store.data["logging"]["interval_s"])
            now = time.time()
            wait = interval - (now % interval)  # align samples to the interval grid
            if self._stop.wait(wait):
                return
            try:
                self.sample()
            except Exception:  # noqa: BLE001
                if time.monotonic() - self._last_error_log > 60:
                    self._last_error_log = time.monotonic()
                    log.exception("Adatnaplózási hiba")
            if time.monotonic() - self._last_size_check >= SIZE_CHECK_EVERY_S:
                self._last_size_check = time.monotonic()
                try:
                    self.enforce_limits()
                except Exception:  # noqa: BLE001
                    log.exception("Naplóméret ellenőrzési hiba")

    # --------------------------------------------------------------- writing
    def _path(self, day: str) -> str:
        return os.path.join(self.dir, day + ".sqlite")

    def _close_conn(self):
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.execute("PRAGMA journal_mode=DELETE")
                self._conn.close()
            except sqlite3.Error as exc:
                log.warning("Naplófájl lezárási hiba: %s", exc)
            self._conn = None
            self._conn_day = None
            self._names = {}

    def _get_conn(self, ts: float):
        day = day_of(ts)
        if self._conn is not None and self._conn_day == day:
            return self._conn
        self._close_conn()
        conn = sqlite3.connect(self._path(day), timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn, self._conn_day = conn, day
        self._names = dict(conn.execute("SELECT mid, name FROM miner_names").fetchall())
        return conn

    def sample(self, ts=None) -> None:
        snap = self.snapshot_fn()
        ts = int(ts if ts is not None else time.time())
        row = snap["sys"]
        miners = snap["miners"]
        with self._lock:
            conn = self._get_conn(ts)
            conn.execute("INSERT OR REPLACE INTO sys (ts, state, %s) VALUES (?, ?, %s)"
                         % (", ".join(SYS_COLS), ", ".join("?" * len(SYS_COLS))),
                         [ts, row.get("state")] + [row.get(c) for c in SYS_COLS])
            if miners:
                conn.executemany("INSERT OR REPLACE INTO miner (mid, ts, %s) VALUES (?, ?, %s)"
                                 % (", ".join(MINER_COLS), ", ".join("?" * len(MINER_COLS))),
                                 [[m["mid"], ts] + [m.get(c) for c in MINER_COLS] for m in miners])
                for m in miners:
                    if self._names.get(m["mid"]) != m["name"]:
                        conn.execute("INSERT OR REPLACE INTO miner_names (mid, name) VALUES (?, ?)",
                                     (m["mid"], m["name"]))
                        self._names[m["mid"]] = m["name"]
            bucket = ts - ts % ROLLUP_S
            if self._rollup is not None and self._rollup.bucket != bucket:
                self._flush_rollup()
            if self._rollup is None:
                self._rollup = _Rollup(bucket)
            self._rollup.add(row, miners)
            if time.monotonic() - self._last_commit >= COMMIT_EVERY_S:
                self.commit_locked()

    def commit_locked(self) -> None:
        if self._conn is not None:
            self._conn.commit()
        self._last_commit = time.monotonic()

    def flush(self) -> None:
        with self._lock:
            self.commit_locked()

    def _flush_rollup(self) -> None:
        ro = self._rollup
        self._rollup = None
        if ro is None:
            return
        conn = self._get_conn(ro.bucket)
        values = [_avg(ro.sys[c]) for c in SYS_COLS]
        conn.execute("INSERT OR REPLACE INTO sys_5m (ts, run, emerg, %s) VALUES (?, ?, ?, %s)"
                     % (", ".join(SYS_COLS), ", ".join("?" * len(SYS_COLS))),
                     [ro.bucket, _avg(ro.run), ro.emerg] + values)
        rows = []
        for mid, acc in ro.miners.items():
            rows.append([mid, ro.bucket] + [_avg(acc[c]) for c in MINER_COLS])
        if rows:
            conn.executemany("INSERT OR REPLACE INTO miner_5m (mid, ts, %s) VALUES (?, ?, %s)"
                             % (", ".join(MINER_COLS), ", ".join("?" * len(MINER_COLS))), rows)

    # ------------------------------------------------------------ size limit
    def _files(self) -> list:
        return sorted(glob.glob(os.path.join(self.dir, "????-??-??.sqlite")))

    @staticmethod
    def _file_size(path: str) -> int:
        size = 0
        for p in (path, path + "-wal", path + "-shm", path + "-journal"):
            try:
                size += os.path.getsize(p)
            except OSError:
                pass
        return size

    def enforce_limits(self) -> list:
        lg = self.cfg_store.data["logging"]
        max_bytes = float(lg["max_size_gb"]) * 1024 ** 3
        min_free = float(lg["min_free_mb"]) * 1024 ** 2
        current = self._path(day_of(time.time()))
        files = self._files()
        sizes = {f: self._file_size(f) for f in files}
        deleted = []
        while files:
            total = sum(sizes[f] for f in files)
            try:
                free = shutil.disk_usage(self.dir).free
            except OSError:
                free = float("inf")
            over_size = total > max_bytes
            low_disk = free < min_free and len(files) > KEEP_DAYS_ON_LOW_DISK
            if not over_size and not low_disk:
                break
            oldest = files[0]
            if oldest == current or (self._conn_day and oldest == self._path(self._conn_day)):
                break
            for p in (oldest, oldest + "-wal", oldest + "-shm", oldest + "-journal"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            deleted.append(os.path.basename(oldest)[:10])
            files.pop(0)
        if deleted:
            msg = "Naplóméret korlát: %d legrégebbi naplófájl törölve (%s%s)" % (
                len(deleted), deleted[0], (" … " + deleted[-1]) if len(deleted) > 1 else "")
            log.info(msg)
            if self.events is not None:
                self.events.add("info", msg, source="naplózás")
        return deleted

    def info(self) -> dict:
        files = self._files()
        total = sum(self._file_size(f) for f in files)
        try:
            usage = shutil.disk_usage(self.dir)
            free, disk_total = usage.free, usage.total
        except OSError:
            free = disk_total = None
        lg = self.cfg_store.data["logging"]
        return {
            "files": len(files),
            "size_bytes": total,
            "max_bytes": int(float(lg["max_size_gb"]) * 1024 ** 3),
            "oldest_day": os.path.basename(files[0])[:10] if files else None,
            "newest_day": os.path.basename(files[-1])[:10] if files else None,
            "disk_free_bytes": free,
            "disk_total_bytes": disk_total,
            "interval_s": lg["interval_s"],
            "directory": os.path.abspath(self.dir),
        }

    # --------------------------------------------------------------- queries
    def _days(self, start: int, end: int) -> list:
        days = []
        t = start - start % 86400
        while t <= end:
            days.append(day_of(t))
            t += 86400
        return days

    def history(self, start: int, end: int, points: int = 600) -> dict:
        start, end = int(start), int(end)
        if end <= start:
            raise ValueError("a záró időpontnak a kezdő után kell lennie")
        points = max(50, min(int(points), 4000))
        interval = max(1, int(float(self.cfg_store.data["logging"]["interval_s"])))
        bucket = max(interval, int(math.ceil((end - start) / float(points))))
        use_rollup = bucket >= ROLLUP_S
        if use_rollup:
            bucket = max(ROLLUP_S, bucket - bucket % ROLLUP_S)
        if use_rollup:
            sys_sql = ("SELECT (ts/?)*? AS b, AVG(run), MAX(emerg), %s FROM sys_5m WHERE ts >= ? AND ts < ? "
                       "GROUP BY b ORDER BY b" % ", ".join("AVG(%s)" % c for c in SYS_COLS))
            m_table = "miner_5m"
        else:
            sys_sql = ("SELECT (ts/?)*? AS b, AVG(state = 2), MAX(state = 4), %s FROM sys WHERE ts >= ? AND ts < ? "
                       "GROUP BY b ORDER BY b" % ", ".join("AVG(%s)" % c for c in SYS_COLS))
            m_table = "miner"
        m_sql = ("SELECT mid, (ts/?)*? AS b, %s FROM %s WHERE ts >= ? AND ts < ? GROUP BY mid, b ORDER BY mid, b"
                 % (", ".join("AVG(%s)" % c for c in MINER_COLS), m_table))
        with self._lock:
            self.commit_locked()
        sys_rows = []
        miners = {}
        names = {}
        for day in self._days(start, end):
            path = self._path(day)
            if not os.path.exists(path):
                continue
            try:
                conn = sqlite3.connect(path, timeout=10)
                try:
                    sys_rows.extend(conn.execute(sys_sql, (bucket, bucket, start, end)).fetchall())
                    for row in conn.execute(m_sql, (bucket, bucket, start, end)):
                        miners.setdefault(row[0], []).append(row[1:])
                    names.update(dict(conn.execute("SELECT mid, name FROM miner_names").fetchall()))
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                log.warning("Naplófájl olvasási hiba (%s): %s", day, exc)

        cols = ("run", "emerg") + SYS_COLS
        sys_out = {"t": []}
        for c in cols:
            sys_out[c] = []
        for row in self._merge(sys_rows):
            sys_out["t"].append(row[0])
            for i, c in enumerate(cols):
                sys_out[c].append(_r(row[i + 1], 1 if c in ("hashrate", "power") else 3))
        miner_out = []
        for mid, rows in miners.items():
            item = {"id": mid, "name": names.get(mid, mid), "t": []}
            for c in MINER_COLS:
                item[c] = []
            for row in self._merge(rows):
                item["t"].append(row[0])
                for i, c in enumerate(MINER_COLS):
                    item[c].append(_r(row[i + 1], 1 if c in ("hashrate", "power") else 2))
            miner_out.append(item)
        return {"start": start, "end": end, "bucket": bucket, "source": "5m" if use_rollup else "raw",
                "sys": sys_out, "miners": miner_out}

    @staticmethod
    def _merge(rows: list) -> list:
        """Merge rows with the same bucket (a bucket may span two daily files)."""
        out = []
        for row in rows:
            if out and out[-1][0] == row[0]:
                prev = out[-1]
                merged = [row[0]]
                for a, b in zip(prev[1:], row[1:]):
                    merged.append(b if a is None else a if b is None else (a + b) / 2.0)
                out[-1] = merged
            else:
                out.append(list(row))
        return out

    def export_rows(self, start: int, end: int):
        """Yield raw system rows (for CSV export)."""
        with self._lock:
            self.commit_locked()
        for day in self._days(int(start), int(end)):
            path = self._path(day)
            if not os.path.exists(path):
                continue
            conn = sqlite3.connect(path, timeout=10)
            try:
                for row in conn.execute("SELECT ts, state, %s FROM sys WHERE ts >= ? AND ts < ? ORDER BY ts"
                                        % ", ".join(SYS_COLS), (int(start), int(end))):
                    yield row
            finally:
                conn.close()
