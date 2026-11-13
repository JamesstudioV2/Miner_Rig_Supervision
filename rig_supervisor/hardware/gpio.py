"""GPIO backends.

Supported libraries (tried in this order):
  * ``RPi.GPIO``  - classic library, ``sudo apt install python3-rpi.gpio``
  * ``lgpio``     - default on Raspberry Pi OS Bookworm, ``sudo apt install python3-lgpio``

Outputs are intentionally NOT reset when the program exits: a restart of the
service must never interrupt the cooling (the pumps keep their state until the
new process takes them over).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading

log = logging.getLogger(__name__)

INSTALL_HINT = "telepítés: sudo apt install python3-rpi.gpio python3-lgpio"
_SOC_RE = re.compile(r"^Hardware\s*:\s*BCM2[78]\d\d", re.MULTILINE)


def pi_model() -> str:
    """Model string of the board ("Raspberry Pi 2 Model B Rev 1.1"), empty when not a Pi."""
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            with open(path, "rb") as f:
                text = f.read().decode("utf-8", "replace").strip("\x00 \n")
            if "Raspberry Pi" in text:
                return text
        except OSError:
            pass
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r"^Model\s*:\s*(.*Raspberry Pi.*)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    if _SOC_RE.search(text):  # old kernels without device tree: only the SoC name is listed
        return "Raspberry Pi (%s)" % _SOC_RE.search(text).group(0).split(":")[1].strip()
    return ""


def is_raspberry_pi() -> bool:
    return bool(pi_model())


class GPIOBackend:
    name = "base"

    def setup_output(self, pin: int, level: bool) -> None:
        raise NotImplementedError

    def write(self, pin: int, level: bool) -> None:
        raise NotImplementedError

    def setup_input(self, pin: int, pull: str) -> None:
        raise NotImplementedError

    def read(self, pin: int) -> bool:
        raise NotImplementedError

    def release(self, pin: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class RPiGPIOBackend(GPIOBackend):
    name = "RPi.GPIO"

    def __init__(self):
        import RPi.GPIO as GPIO  # noqa: N814 - library naming

        self.G = GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        # access probe: fails right here when /dev/gpiomem is not accessible (missing "gpio"
        # group membership...) so that the next library can be tried instead
        GPIO.gpio_function(4)
        self.version = str(getattr(GPIO, "VERSION", "?"))

    def setup_output(self, pin, level):
        self.G.setup(pin, self.G.OUT, initial=self.G.HIGH if level else self.G.LOW)

    def write(self, pin, level):
        self.G.output(pin, self.G.HIGH if level else self.G.LOW)

    def setup_input(self, pin, pull):
        pud = {"up": self.G.PUD_UP, "down": self.G.PUD_DOWN}.get(pull, self.G.PUD_OFF)
        self.G.setup(pin, self.G.IN, pull_up_down=pud)

    def read(self, pin):
        return bool(self.G.input(pin))

    def release(self, pin):
        self.G.cleanup(pin)


class LGPIOBackend(GPIOBackend):
    name = "lgpio"

    def __init__(self, chip: int = 0):
        import lgpio

        self.lg = lgpio
        self.handle = lgpio.gpiochip_open(chip)
        self.version = str(getattr(lgpio, "LGPIO_PY_VERSION", "?"))

    def close(self):
        try:
            self.lg.gpiochip_close(self.handle)
        except Exception:  # noqa: BLE001
            pass

    def setup_output(self, pin, level):
        try:
            self.lg.gpio_free(self.handle, pin)
        except Exception:  # noqa: BLE001 - not claimed yet
            pass
        self.lg.gpio_claim_output(self.handle, pin, 1 if level else 0)

    def write(self, pin, level):
        self.lg.gpio_write(self.handle, pin, 1 if level else 0)

    def setup_input(self, pin, pull):
        try:
            self.lg.gpio_free(self.handle, pin)
        except Exception:  # noqa: BLE001
            pass
        flags = {"up": self.lg.SET_PULL_UP, "down": self.lg.SET_PULL_DOWN}.get(pull, self.lg.SET_PULL_NONE)
        self.lg.gpio_claim_input(self.handle, pin, flags)

    def read(self, pin):
        return bool(self.lg.gpio_read(self.handle, pin))

    def release(self, pin):
        self.lg.gpio_free(self.handle, pin)


class SimGPIOBackend(GPIOBackend):
    """In-memory GPIO used by the simulator and the tests."""

    name = "szimulált"

    def __init__(self):
        self._lock = threading.Lock()
        self.pins = {}
        self.external = {}

    def setup_output(self, pin, level):
        with self._lock:
            self.pins[pin] = {"mode": "out", "level": bool(level)}

    def write(self, pin, level):
        with self._lock:
            p = self.pins.get(pin)
            if p is None or p["mode"] != "out":
                raise RuntimeError("GPIO%d nincs kimenetként beállítva" % pin)
            p["level"] = bool(level)

    def setup_input(self, pin, pull):
        with self._lock:
            self.pins[pin] = {"mode": "in", "pull": pull}

    def read(self, pin):
        with self._lock:
            p = self.pins.get(pin)
            if p is None:
                raise RuntimeError("GPIO%d nincs beállítva" % pin)
            if p["mode"] == "out":
                return p["level"]
            ext = self.external.get(pin)
            if ext is not None:
                return ext
            return p.get("pull") == "up"

    def release(self, pin):
        with self._lock:
            self.pins.pop(pin, None)

    # --- simulator helpers -------------------------------------------------
    def drive_external(self, pin, level) -> None:
        """Model the external circuit: ``None`` = floating (pull resistor wins)."""
        with self._lock:
            if level is None:
                self.external.pop(pin, None)
            else:
                self.external[pin] = bool(level)

    def output_level(self, pin):
        with self._lock:
            p = self.pins.get(pin)
            if p and p["mode"] == "out":
                return p["level"]
            return None


class NullGPIOBackend(GPIOBackend):
    """Placeholder when no GPIO library works - every call fails loudly."""

    name = "nincs"

    def __init__(self, reason: str):
        self.reason = reason

    def _fail(self, *_args, **_kwargs):
        raise RuntimeError("GPIO nem elérhető: %s" % self.reason)

    setup_output = write = setup_input = read = release = _fail


def create_gpio_backend(kind: str):
    """Return ``(backend, errors)``. ``kind`` is ``"sim"`` or ``"rpi"``."""
    if kind == "sim":
        return SimGPIOBackend(), []
    errors = []
    for cls in (RPiGPIOBackend, LGPIOBackend):
        try:
            backend = cls()
            log.info("GPIO könyvtár: %s", backend.name)
            return backend, errors
        except Exception as exc:  # noqa: BLE001 - try the next one
            errors.append("%s: %s" % (cls.name, exc))
    errors.append(INSTALL_HINT)
    reason = "; ".join(errors)
    log.error("Egyik GPIO könyvtár sem használható: %s", reason)
    return NullGPIOBackend(reason), errors


def diagnostics() -> dict:
    """Environment facts that matter for GPIO access (shown on the calibration page)."""
    devices = {}
    for dev in ("/dev/gpiomem", "/dev/gpiochip0", "/dev/spidev0.0", "/dev/i2c-1"):
        if not os.path.exists(dev):
            devices[dev] = "nincs"
        elif os.access(dev, os.R_OK | os.W_OK):
            devices[dev] = "elérhető"
        else:
            devices[dev] = "nincs jogosultság"
    libs = {}
    for mod in ("RPi.GPIO", "lgpio", "spidev", "smbus2", "smbus"):
        libs[mod] = "betöltve" if mod in sys.modules else "–"
    try:
        import grp
        groups = sorted({grp.getgrgid(g).gr_name for g in os.getgroups()})
    except Exception:  # noqa: BLE001
        groups = []
    try:
        import pwd
        user = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001
        user = str(os.geteuid()) if hasattr(os, "geteuid") else ""
    return {"model": pi_model(), "user": user, "groups": groups, "devices": devices, "libs": libs,
            "python": sys.version.split()[0]}
