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
        self.store.update({"hardware": {"io": {"pump1": {"gpio": 6}}}})
        self.assertNotIn(17, self.gpio.pins)      # old pin released (after being driven inactive)
        self.assertTrue(self.gpio.output_level(6))  # new pin keeps the pump running

    def test_output_inversion_is_glitch_free(self):
        calls = []
        orig_release, orig_write = self.gpio.release, self.gpio.write
        self.gpio.release = lambda pin: (calls.append(("release", pin)), orig_release(pin))
        self.gpio.write = lambda pin, level: (calls.append(("write", pin, level)), orig_write(pin, level))
        self.assertTrue(self.gpio.output_level(17))           # P1 BE, magas = aktív -> 3,3 V
        self.store.update({"hardware": {"io": {"pump1": {"active": "low"}}}})
        self.assertEqual(calls, [("write", 17, False)])        # nincs elengedés, csak átírás
        self.assertFalse(self.gpio.output_level(17))          # P1 továbbra is BE, de negálva -> 0 V
        self.assertTrue(self.io.outputs()["pump1"])           # a logikai állapot nem változott
        self.io.set_outputs({"pump1": False, "pump2": False}, 10.0)
        self.assertTrue(self.gpio.output_level(17))           # KI + negált -> 3,3 V
        pins = self.io.pin_states()
        self.assertFalse(pins["pump1"]["active"])
        self.assertTrue(pins["pump1"]["level"])

    def test_input_inversion_no_spurious_press(self):
        states, _ = self.io.poll_inputs(0.0)
        self.assertFalse(states["btn_start"])                 # felhúzva (3,3 V), alacsony = aktív -> nincs lenyomva
        self.store.update({"hardware": {"io": {"btn_start": {"active": "high"}}}})
        states, edges = self.io.poll_inputs(0.1)
        self.assertTrue(states["btn_start"])                  # ugyanaz a 3,3 V most aktívnak számít...
        self.assertEqual(edges, [])                           # ...de ez NEM gombnyomás (nincs él)
        self.store.update({"hardware": {"io": {"flow1": {"active": "high"}}}})
        states, edges = self.io.poll_inputs(1.0)
        self.assertTrue(states["flow1"])                      # negálás nélkül: 3,3 V = áramlik (azonnal, él nélkül)
        self.assertEqual(edges, [])
        self.gpio.drive_external(22, False)                   # 0 V -> nem áramlik
        self.io.poll_inputs(1.1)
        states, edges = self.io.poll_inputs(1.5)
        self.assertFalse(states["flow1"])
        self.assertIn(("flow1", False), edges)

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
