import os
import tempfile
import unittest

from rig_supervisor.config import ConfigStore
from rig_supervisor.hardware.io import IOManager


class _Events:
    def __init__(self):
        self.items = []

    def add(self, level, message, code="", source=""):
        self.items.append(message)


class IOManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(os.path.join(self.tmp.name, "c.json"))
        self.io = IOManager(self.store, _Events(), use_sim=True)
        self.io.start({"pump1": True, "pump2": False})
        self.gpio = self.io.gpio

    def tearDown(self):
        self.io.stop()
        self.tmp.cleanup()

    def test_initial_outputs_and_levels(self):
        self.assertTrue(self.gpio.output_level(17))   # P1 on, active high
        self.assertFalse(self.gpio.output_level(27))  # P2 off
        self.store.update({"hardware": {"io": {"pump2": {"active": "low"}}}})
        self.assertTrue(self.gpio.output_level(27))   # off + active low -> high level
        self.io.set_outputs({"pump1": False, "pump2": True}, 0.0)
        self.assertFalse(self.gpio.output_level(17))
        self.assertFalse(self.gpio.output_level(27))

    def test_debounced_inputs(self):
        # flow switch: active low with pull-up -> idle reads "no flow"
        states, edges = self.io.poll_inputs(0.0)
        self.assertFalse(states["flow1"])
        self.gpio.drive_external(22, False)  # flowing
        states, edges = self.io.poll_inputs(0.1)
        self.assertFalse(states["flow1"])      # not yet (debounce 0.3 s)
        states, edges = self.io.poll_inputs(0.45)
        self.assertTrue(states["flow1"])
        self.assertIn(("flow1", True), edges)

    def test_pin_reassignment_releases_old_pin(self):
        self.store.update({"hardware": {"io": {"pump1": {"gpio": 5}}}})
        self.assertNotIn(17, self.gpio.pins)      # old pin released (after being driven inactive)
        self.assertTrue(self.gpio.output_level(5))  # new pin keeps the pump running

    def test_read_error_is_fail_safe(self):
        self.gpio.drive_external(22, False)
        self.io.poll_inputs(0.0)
        states, _ = self.io.poll_inputs(0.5)
        self.assertTrue(states["flow1"])
        self.gpio.release(22)  # reading now raises
        states, edges = self.io.poll_inputs(0.6)
        self.assertFalse(states["flow1"])
        self.assertIn(("flow1", False), edges)
        self.assertFalse(self.io.status()["pin_errors"] == {})


if __name__ == "__main__":
    unittest.main()
