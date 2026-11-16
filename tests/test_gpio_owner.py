"""Two controllers must never drive the same GPIO pins (the pump outputs would jump between them)."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

from rig_supervisor.config import ConfigStore
from rig_supervisor.hardware import io as IO
from rig_supervisor.hardware import owner
from rig_supervisor.hardware.gpio import SimGPIOBackend

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDER = "import sys, time; sys.path.insert(0, %r); from rig_supervisor.hardware import owner; " \
         "print(owner.acquire()[0], flush=True); time.sleep(60)" % ROOT


class _Events:
    def __init__(self):
        self.items = []

    def add(self, level, message, code="", source=""):
        self.items.append((level, code, message))


@unittest.skipUnless(sys.platform.startswith("linux"), "abstract unix sockets: Linux only")
class OwnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(os.path.join(self.tmp.name, "config.json"))
        while owner.owned():  # earlier tests in this process may still hold a reference
            owner.release()

    def tearDown(self):
        self.tmp.cleanup()

    def _holder(self):
        p = subprocess.Popen([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.readline().strip(), b"True")

        def done():
            p.kill()
            p.wait()
            p.stdout.close()
        self.addCleanup(done)
        return p

    def test_second_copy_is_refused_and_takes_over_later(self):
        p = self._holder()
        ok, text = owner.acquire()
        self.assertFalse(ok)
        self.assertIn("másik Rig Supervisor példány", text)

        io = IO.IOManager(self.store, _Events(), use_sim=False)
        io.start({"pump1": True, "pump2": True})
        try:
            st = io.status()
            self.assertTrue(st["owner_blocked"])
            self.assertFalse(st["gpio_ok"])
            self.assertIn("másik Rig Supervisor példány", st["gpio_errors"][0])
            self.assertIn("másik", st["adc_error"])          # the shared ADC is not touched either
            p.kill()
            p.wait()
            io._owner_retry_at = 0
            io.set_outputs({"pump1": False, "pump2": False}, time.monotonic())
            self.assertFalse(io.status()["owner_blocked"])    # took over once the other copy was gone
            self.assertTrue(owner.owned())
        finally:
            io.stop()
        self.assertFalse(owner.owned())

    def test_app_refuses_to_start(self):
        from rig_supervisor.app import AlreadyRunning, App

        self._holder()
        app = App(self.tmp.name, port=0, simulate=False)
        with self.assertRaises(AlreadyRunning):
            app.start("127.0.0.1")
        self.assertIsNone(app.web)                            # nothing was started


class FlappingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(os.path.join(self.tmp.name, "config.json"))
        self.io = IO.IOManager(self.store, _Events(), use_sim=True)
        self.io.start({"pump1": True, "pump2": False})
        self.gpio = self.io.gpio
        self.assertIsInstance(self.gpio, SimGPIOBackend)

    def tearDown(self):
        self.io.stop()
        self.tmp.cleanup()

    def test_output_overwritten_by_someone_else(self):
        now = 1000.0
        for i in range(8):
            # another program pulls the P1 pin low now and then; our re-assert sets it back
            if i % 2 == 0:
                self.gpio.pins[17]["level"] = False
            self.io._last_readback = -1e9
            self.io.set_outputs({"pump1": True, "pump2": False}, now)
            now += 6.0                                         # > REASSERT_S: the pin is rewritten
        st = self.io.status()
        self.assertIn("pump1", st["output_flapping"])
        self.assertNotIn("pump1", st["output_mismatch"])      # not a permanent mismatch
        self.assertNotIn("pump2", st["output_flapping"])

    def test_floating_input(self):
        now = 0.0
        level = False
        for _ in range(60):
            level = not level
            self.gpio.drive_external(23, level)
            for _ in range(4):                                 # each level is stable long enough to pass
                self.io.poll_inputs(time.monotonic())
                time.sleep(0.12)
            now += 1
            if "flow2" in self.io.status()["input_flapping"]:
                break
        self.assertIn("flow2", self.io.status()["input_flapping"])

    def test_pin_conflict_is_reported(self):
        self.gpio.conflict = lambda pin: "a rendszer SPI busz funkcióra használja" if pin == 27 else None
        self.store.update({"hardware": {"io": {"pump2": {"gpio": 26}}}})
        self.store.update({"hardware": {"io": {"pump2": {"gpio": 27}}}})
        self.assertIn("SPI", self.io.status()["pin_warnings"]["P2 szivattyú SSR (külső kör)"])


if __name__ == "__main__":
    unittest.main()
