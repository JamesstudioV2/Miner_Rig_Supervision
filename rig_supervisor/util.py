"""Small shared helpers (file handling, dict merging, number formatting)."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time

log = logging.getLogger(__name__)


def now() -> float:
    """Wall clock time (epoch seconds) - used for timestamps shown to users."""
    return time.time()


def mono() -> float:
    """Monotonic clock - used for all timing decisions (immune to NTP jumps)."""
    return time.monotonic()


def atomic_write_json(path: str, data, keep_backup: bool = False) -> None:
    """Write JSON atomically (temp file + fsync + rename).

    A power loss during the write never leaves a half written file behind.
    With ``keep_backup`` the previous version is kept as ``<path>.bak``.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if keep_backup and os.path.exists(path):
            try:
                bak = path + ".bak"
                with open(path, "rb") as src, open(bak + ".tmp", "wb") as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(bak + ".tmp", bak)
            except OSError as exc:  # backup is best effort only
                log.warning("Nem sikerült biztonsági mentést készíteni (%s): %s", path, exc)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge(base: dict, patch: dict) -> dict:
    """Return a new dict: ``patch`` merged recursively into ``base``.

    Dicts are merged key by key, every other value (lists included) is replaced.
    """
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def clamp(value, lo, hi):
    return lo if value < lo else hi if value > hi else value


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d mp" % seconds
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return "%d perc %d mp" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return "%d óra %d perc" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%d nap %d óra" % (days, hours)


class ChangeSignal:
    """Version counter + condition variable: wakes the live (SSE) streams on changes."""

    def __init__(self):
        self._cond = threading.Condition()
        self.version = 0

    def bump(self) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def wait(self, seen_version: int, timeout: float) -> int:
        with self._cond:
            if self.version == seen_version:
                self._cond.wait(timeout)
            return self.version


class StoppableThread(threading.Thread):
    """Daemon thread running ``target(stop_event)`` with a shared stop event."""

    def __init__(self, name: str, target):
        super().__init__(name=name, daemon=True)
        self._target_fn = target
        self.stop_event = threading.Event()

    def run(self):
        try:
            self._target_fn(self.stop_event)
        except Exception:  # pragma: no cover - logged, thread ends
            log.exception("A(z) %s szál váratlanul leállt", self.name)

    def stop(self):
        self.stop_event.set()
