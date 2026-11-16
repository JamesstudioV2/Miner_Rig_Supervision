"""Only one Rig Supervisor process may drive the GPIO pins.

Two running copies of the program (e.g. the systemd service and a copy started by
hand from the command line, or an old installation next to a new one) would both
write the pump outputs: the classic RPi.GPIO library does not prevent it, so the
pins jump between the two programs' states (every few seconds, when either copy
re-asserts its outputs), and both copies talk to the same ADC at the same time,
which garbles the temperature readings.

The owner is marked by an abstract unix socket: it needs no file permissions,
works across users and data directories, and the kernel frees it the moment the
owning process dies (even after kill -9 or a crash).
"""

from __future__ import annotations

import os
import socket
import sys
import threading

_NAME = "\0rig-supervisor-gpio-owner"
_lock = threading.Lock()
_sock = None
_refs = 0


def acquire():
    """Take the GPIO ownership: ``(True, "")`` or ``(False, description of the other copy)``."""
    global _sock, _refs
    with _lock:
        if _sock is not None:
            _refs += 1
            return True, ""
        if not sys.platform.startswith("linux"):
            _sock, _refs = "no-lock", 1
            return True, ""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(_NAME)
        except OSError:
            s.close()
            return False, describe_others()
        _sock, _refs = s, 1
        return True, ""


def release() -> None:
    global _sock, _refs
    with _lock:
        if _sock is None:
            return
        _refs -= 1
        if _refs <= 0:
            if not isinstance(_sock, str):
                _sock.close()
            _sock, _refs = None, 0


def owned() -> bool:
    return _sock is not None


def other_instances() -> list:
    """Other running Rig Supervisor processes: ``[{"pid", "cmd"}]`` (diagnostics only)."""
    out = []
    me = os.getpid()
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        if pid == me:
            continue
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                parts = [p.decode("utf-8", "replace") for p in f.read().split(b"\0") if p]
        except OSError:
            continue
        if not parts or "python" not in os.path.basename(parts[0]):
            continue
        is_app = any(parts[i] == "-m" and parts[i + 1].split(".")[0] == "rig_supervisor"
                     for i in range(len(parts) - 1)) or any(p.endswith("rig_supervisor/__main__.py") for p in parts)
        if is_app and not any(p in ("--diagnose", "--gpio-test", "--reset-password") for p in parts):
            out.append({"pid": pid, "cmd": " ".join(parts)[:200]})
    return out


def describe_others() -> str:
    others = other_instances()
    who = "; ".join("PID %d: %s" % (o["pid"], o["cmd"]) for o in others) or "ismeretlen folyamat"
    return ("egy másik Rig Supervisor példány már vezérli a GPIO lábakat (%s) – két vezérlő egyszerre "
            "ugráló kimeneteket okozna. Csak egy példány fusson: állítsd le a másikat "
            "(sudo systemctl stop rig-supervisor, vagy kill %s)" % (
                who, " ".join(str(o["pid"]) for o in others) or "<PID>"))
