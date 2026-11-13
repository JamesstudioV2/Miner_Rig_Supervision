"""Hardware mode (simulation <-> real GPIO) must switch immediately and the UI must tell the truth.

Reproduces the reported problem: the program runs in simulation, "real hardware" is selected on
the web interface - from that moment the real GPIO pins have to follow the outputs.
"""

import json
import os
import socket
import sys
import tempfile
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

from rig_supervisor.config import ConfigStore
from rig_supervisor.hardware.io import IOManager, MISMATCH_AFTER
from rig_supervisor.miners import manager as M
from rig_supervisor.util import ChangeSignal


class FakeRPiGPIO:
    """Minimal stand-in for the RPi.GPIO module that records what happens to the pins."""

    BCM, OUT, IN, HIGH, LOW = 11, 0, 1, 1, 0
    PUD_OFF, PUD_DOWN, PUD_UP = 20, 21, 22
    VERSION = "0.7.1-fake"

    def __init__(self):
        self.pins = {}
        self.cleaned = []
        self.no_access = False

    def install(self):
        pkg = types.ModuleType("RPi")
        mod = types.ModuleType("RPi.GPIO")
        for name in ("BCM", "OUT", "IN", "HIGH", "LOW", "PUD_OFF", "PUD_DOWN", "PUD_UP", "VERSION"):
            setattr(mod, name, getattr(self, name))
        for name in ("setwarnings", "setmode", "setup", "output", "input", "cleanup", "gpio_function"):
            setattr(mod, name, getattr(self, name))
        pkg.GPIO = mod
        return mock.patch.dict(sys.modules, {"RPi": pkg, "RPi.GPIO": mod})

    def setwarnings(self, flag):
        pass

    def setmode(self, mode):
        assert mode == self.BCM

    def gpio_function(self, pin):
        if self.no_access:
            raise RuntimeError("No access to /dev/mem.  Try running as root!")
        return self.IN

    def setup(self, pin, direction, initial=None, pull_up_down=None):
        if direction == self.OUT:
            self.pins[pin] = {"dir": "out", "level": bool(initial)}
        else:
            self.pins[pin] = {"dir": "in", "level": pull_up_down == self.PUD_UP}

    def output(self, pin, level):
        assert self.pins[pin]["dir"] == "out", "GPIO%d is not an output" % pin
        self.pins[pin]["level"] = bool(level)

    def input(self, pin):
        return 1 if self.pins[pin]["level"] else 0

    def cleanup(self, pin):
        self.cleaned.append(pin)
        self.pins.pop(pin, None)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveModeSwitchTest(unittest.TestCase):
    def setUp(self):
        from rig_supervisor.app import App
        self.fake = FakeRPiGPIO()
        self.patch = self.fake.install()
        self.patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        # exactly the reported situation: started with "--sim"
        self.app = App(self.tmp.name, port=_free_port(), simulate=True)
        self.app.start("127.0.0.1")
        self.base = "http://127.0.0.1:%d" % self.app.port

    def tearDown(self):
        self.app.stop()
        self.tmp.cleanup()
        self.patch.stop()

    def req(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET")
        if body is not None:
            r.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def wait_for(self, cond, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    def test_selecting_real_hardware_drives_the_real_pins_immediately(self):
        st, d = self.req("/api/state")
        self.assertTrue(d["sim"])
        self.assertIn("--sim", d["mode"]["reason"])
        self.assertEqual(self.fake.pins, {})  # simulation never touches the real pins

        st, d = self.req("/api/config", {"patch": {"hardware": {"backend": "rpi"}}})
        self.assertEqual(st, 200, d)
        self.assertFalse(d["sim"])                       # no restart needed
        self.assertFalse(self.app.simulated)
        self.assertEqual(self.app.io.status()["gpio_backend"], "RPi.GPIO")
        self.assertEqual(self.fake.pins[17], {"dir": "out", "level": False})
        self.assertEqual(self.fake.pins[27], {"dir": "out", "level": False})
        self.assertEqual(self.fake.pins[22]["dir"], "in")

        # manual (hold-to-run) pump button -> the real pin goes high, release -> low
        st, _ = self.req("/api/control/manual", {"pump": "pump1", "action": "press"})
        self.assertEqual(st, 200)
        self.assertTrue(self.wait_for(lambda: self.fake.pins[17]["level"]))
        st, d = self.req("/api/hw")
        self.assertTrue(d["pins"]["pump1"]["actual"])     # read back from the pin itself
        self.assertFalse(d["pins"]["pump1"]["mismatch"])
        self.assertFalse(d["mode"]["simulated"])
        self.req("/api/control/manual", {"pump": "pump1", "action": "release"})
        self.assertTrue(self.wait_for(lambda: not self.fake.pins[17]["level"]))

        # an inverted (active low) output drives the pin high while the pump is off
        st, _ = self.req("/api/config", {"patch": {"hardware": {"io": {"pump2": {"active": "low"}}}}})
        self.assertEqual(st, 200)
        self.assertTrue(self.fake.pins[27]["level"])

        # the explicit choice survives a restart even when the program is started with --sim again
        from rig_supervisor.app import App
        again = App(self.tmp.name, port=0, simulate=True)
        self.assertFalse(again.simulated)
        again.events.close()

    def test_switching_back_to_simulation_releases_the_pins(self):
        self.req("/api/config", {"patch": {"hardware": {"backend": "rpi"}}})
        self.assertIn(17, self.fake.pins)
        st, d = self.req("/api/config", {"patch": {"hardware": {"backend": "sim"}}})
        self.assertEqual(st, 200)
        self.assertTrue(d["sim"])
        self.assertEqual(self.fake.pins, {})
        self.assertIn(17, self.fake.cleaned)
        # the simulator is alive again: temperatures come back
        self.assertTrue(self.wait_for(lambda: self.app.io.sensor_snapshot()["t1"]["value"] is not None, 4.0))
        self.assertTrue(self.wait_for(
            lambda: all(m["online"] for m in self.app.miners.snapshot()), 4.0))

    def test_mode_change_refused_while_running(self):
        st, _ = self.req("/api/control/start", {})
        self.assertEqual(st, 200)
        st, d = self.req("/api/config", {"patch": {"hardware": {"backend": "rpi"}}})
        self.assertEqual(st, 409)
        self.assertIn("leállított", d["error"])
        self.assertTrue(self.app.simulated)
        # other settings can still be saved while running
        st, _ = self.req("/api/config", {"patch": {"site_name": "Teszt"}})
        self.assertEqual(st, 200)

    def test_no_gpio_access_is_reported_not_hidden(self):
        self.fake.no_access = True  # e.g. the service user is not in the "gpio" group
        st, d = self.req("/api/config", {"patch": {"hardware": {"backend": "rpi"}}})
        self.assertEqual(st, 200)
        hw = self.app.io.status()
        self.assertFalse(hw["gpio_ok"])
        self.assertTrue(any("No access" in e for e in hw["gpio_errors"]))
        ok, msg = self.app.controller.request_start("teszt")
        self.assertFalse(ok)  # the system cannot be started without working outputs
        self.assertIn("GPIO", msg)


class ModeResolutionTest(unittest.TestCase):
    def test_precedence(self):
        from rig_supervisor.app import App
        with tempfile.TemporaryDirectory() as d:
            app = App(d, port=0, simulate=None)
            cfg = lambda backend: {"hardware": {"backend": backend}}  # noqa: E731
            app.pi_model = ""
            self.assertTrue(app._resolve_mode(cfg("auto"))[0])       # not a Pi -> simulation
            app.pi_model = "Raspberry Pi 2 Model B Rev 1.1"
            self.assertFalse(app._resolve_mode(cfg("auto"))[0])      # Pi -> real hardware
            app._cli_simulate = True
            self.assertTrue(app._resolve_mode(cfg("auto"))[0])       # --sim only changes "auto"
            self.assertFalse(app._resolve_mode(cfg("rpi"))[0])       # the web setting always wins
            app._cli_simulate = False
            self.assertTrue(app._resolve_mode(cfg("sim"))[0])
            app.events.close()


class _Events:
    def __init__(self):
        self.items = []

    def add(self, level, message, code="", source=""):
        self.items.append(message)


class SafetyTest(unittest.TestCase):
    def test_real_miner_is_never_started_in_simulation(self):
        from rig_supervisor.hardware.sim import SimWorld
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(os.path.join(d, "c.json"))
            store.update({"miners": {"list": [
                {"id": "real", "name": "S19", "host": "127.0.0.1", "type": "antminer", "api_port": 1},
                {"id": "sim", "name": "Sim", "host": "10.0.0.2", "type": "simulated"}]}})
            events = _Events()
            mgr = M.MinerManager(store, events, ChangeSignal(), sim_world=SimWorld(store))
            try:
                self.assertEqual(mgr.command_all("start"), 1)          # only the simulated one
                self.assertTrue(any("Szimulációs módban" in m and "S19" in m for m in events.items))
                ok, msg = mgr.command_one("real", "start")
                self.assertFalse(ok)
                self.assertEqual(mgr.command_all("stop"), 2)           # stopping is always allowed
                mgr.set_sim_world(None)                                  # real hardware mode
                self.assertEqual(mgr.command_all("start"), 2)
            finally:
                mgr.stop()

    def test_output_readback_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(os.path.join(d, "c.json"))
            io = IOManager(store, _Events(), use_sim=True)
            io.start({"pump1": False, "pump2": False})
            try:
                orig = io.gpio.read
                io.gpio.read = lambda pin: False if pin == 17 else orig(pin)  # P1 pin stuck low
                for i in range(MISMATCH_AFTER + 1):
                    io.set_outputs({"pump1": True, "pump2": True}, 100.0 + i * 1.5)
                mm = io.status()["output_mismatch"]
                self.assertEqual(list(mm), ["pump1"])
                self.assertTrue(mm["pump1"]["expected"])
                self.assertFalse(mm["pump1"]["actual"])
                pins = io.pin_states()
                self.assertTrue(pins["pump1"]["mismatch"])
                self.assertFalse(pins["pump2"]["mismatch"])
                io.gpio.read = orig
                io.set_outputs({"pump1": True, "pump2": True}, 200.0)
                self.assertEqual(io.status()["output_mismatch"], {})
            finally:
                io.stop()


if __name__ == "__main__":
    unittest.main()
