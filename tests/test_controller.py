"""Deterministic tests of the safety controller (fake clock, fake IO and miners)."""

import json
import os
import tempfile
import unittest

from rig_supervisor import controller as CT
from rig_supervisor.config import ConfigStore, SENSOR_IDS
from rig_supervisor.util import ChangeSignal


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1.7e9 + self.t


class FakeIO:
    def __init__(self):
        self.inputs = {"flow1": False, "flow2": False, "btn_start": False, "btn_stop": False, "killswitch": False}
        self.errors = {}
        self.edges = []
        self.temps = {sid: {"value": 25.0, "raw_value": 25.0, "fault": None, "fault_text": None, "voltage": 1.6,
                            "ohm": 10000.0} for sid in SENSOR_IDS}
        self.outputs = {"pump1": False, "pump2": False}
        self.gpio_ok = True

    def poll_inputs(self, mono):
        edges, self.edges = self.edges, []
        return dict(self.inputs), edges

    def set_input(self, name, value):
        if self.inputs[name] != value:
            self.inputs[name] = value
            self.edges.append((name, value))

    def input_error(self, name):
        return self.errors.get(name)

    def sensor_snapshot(self):
        return {k: dict(v) for k, v in self.temps.items()}

    def set_outputs(self, states, mono):
        self.outputs = dict(states)

    def status(self):
        return {"sim": False, "gpio_backend": "fake", "gpio_ok": self.gpio_ok, "gpio_errors": [], "pin_errors": {},
                "adc": "fake", "adc_ok": True, "adc_error": None}


class FakeMiners:
    def __init__(self):
        self.calls = []
        self.hot = (None, None)
        self.forced = 0

    def command_all(self, action, reason="", source=""):
        self.calls.append(action)
        return 4

    def hottest(self):
        return self.hot

    def force_stop_hashing(self, reason):
        self.forced += 1
        return 1


class Events:
    def __init__(self):
        self.items = []

    def add(self, level, message, code="", source=""):
        self.items.append((level, code, message))

    def codes(self):
        return [c for _, c, _ in self.items if c]


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self._orig_time = CT.time
        CT.time = self.clock
        self.store = ConfigStore(os.path.join(self.tmp.name, "config.json"))
        self.io = FakeIO()
        self.miners = FakeMiners()
        self.events = Events()
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.c = self._make()

    def tearDown(self):
        CT.time = self._orig_time
        self.tmp.cleanup()

    def _make(self):
        c = CT.Controller(self.store, self.io, self.miners, self.events, ChangeSignal(), self.state_path)
        return c

    def run_for(self, seconds, step=0.05):
        end = self.clock.t + seconds
        while self.clock.t < end - 1e-9:
            self.clock.t += step
            self.c.tick()

    def start_running(self):
        self.c.restore()
        ok, _ = self.c.request_start("teszt")
        self.assertTrue(ok)
        self.c.tick()
        self.io.set_input("flow1", True)
        self.io.set_input("flow2", True)
        self.run_for(2.5)
        self.assertEqual(self.c.state, CT.RUNNING)

    # ------------------------------------------------------------- sequences
    def test_start_sequence(self):
        self.c.restore()
        self.assertEqual(self.miners.calls, ["stop"])  # stopped state is enforced at start-up
        self.c.request_start("teszt")
        self.c.tick()
        self.assertEqual(self.c.state, CT.STARTING)
        self.assertEqual(self.io.outputs, {"pump1": True, "pump2": True})  # pumps first
        self.run_for(1.8)
        self.assertEqual(self.miners.calls, ["stop"])  # miners not yet
        self.run_for(0.4)
        self.assertEqual(self.miners.calls, ["stop", "start"])  # 2 s after the pumps
        self.assertEqual(self.c.state, CT.RUNNING)
        with open(self.state_path) as f:
            self.assertEqual(json.load(f)["state"], CT.RUNNING)

    def test_flow_never_starts(self):
        self.c.restore()
        self.c.request_start("teszt")
        self.io.set_input("flow2", True)
        self.run_for(14.0)
        self.assertEqual(self.c.state, CT.RUNNING)
        self.run_for(1.2)
        self.assertEqual(self.c.state, CT.EMERGENCY)
        self.assertEqual(self.c.alarm["code"], "E101")
        self.assertEqual(self.io.outputs, {"pump1": False, "pump2": False})
        self.assertEqual(self.miners.calls[-1], "stop")

    def test_flow_loss_while_running(self):
        self.start_running()
        self.io.set_input("flow2", False)
        self.run_for(2.0)
        self.io.set_input("flow2", True)  # short dropout is tolerated
        self.run_for(1.0)
        self.assertEqual(self.c.state, CT.RUNNING)
        self.io.set_input("flow2", False)
        self.run_for(3.2)
        self.assertEqual(self.c.state, CT.EMERGENCY)
        self.assertEqual(self.c.alarm["code"], "E104")
        self.assertIn("E104", self.events.codes())

    def test_flow_check_disabled(self):
        self.store.update({"hardware": {"io": {"flow1": {"enabled": False}}}})
        self.c.restore()
        self.c.request_start("teszt")
        self.io.set_input("flow2", True)
        self.run_for(20)
        self.assertEqual(self.c.state, CT.RUNNING)
        self.assertTrue(any(w["code"] == "W303" for w in self.c.snapshot()["warnings"]))

    def test_stop_sequence(self):
        self.start_running()
        ok, _ = self.c.request_stop("teszt")
        self.assertTrue(ok)
        self.c.tick()
        self.assertEqual(self.miners.calls[-1], "stop")
        self.assertEqual(self.c.state, CT.STOPPING)
        self.assertTrue(self.io.outputs["pump1"])  # post-circulation
        self.run_for(29.0)
        self.assertEqual(self.c.state, CT.STOPPING)
        self.run_for(1.2)
        self.assertEqual(self.c.state, CT.STOPPED)
        self.assertEqual(self.io.outputs, {"pump1": False, "pump2": False})

    def test_physical_buttons(self):
        self.c.restore()
        self.io.set_input("btn_start", True)
        self.c.tick()
        self.assertEqual(self.c.state, CT.STARTING)
        self.io.set_input("btn_start", False)
        self.io.set_input("flow1", True)
        self.io.set_input("flow2", True)
        self.run_for(3)
        self.io.set_input("btn_stop", True)
        self.c.tick()
        self.assertEqual(self.c.state, CT.STOPPING)

    def test_button_inversion_does_not_start(self):
        """Flipping a button's polarity changes its logical level without an edge -> no start."""
        self.c.restore()
        self.io.inputs["btn_start"] = True  # level flipped by the inversion, but no edge reported
        self.run_for(2)
        self.assertEqual(self.c.state, CT.STOPPED)

    # ------------------------------------------------------------ killswitch
    def test_killswitch_while_running(self):
        self.start_running()
        self.io.set_input("killswitch", True)
        self.c.tick()  # same tick: pumps off, miners stopped - no post-circulation
        self.assertEqual(self.io.outputs, {"pump1": False, "pump2": False})
        self.assertEqual(self.miners.calls[-1], "stop")
        self.assertEqual(self.c.state, CT.EMERGENCY)
        self.assertEqual(self.c.alarm["code"], "E001")
        self.miners.hot = (90.0, "x")  # overheat assist is suppressed
        self.run_for(5)
        self.assertFalse(self.io.outputs["pump1"])
        ok, _ = self.c.manual_pump("pump1", "press")
        self.assertFalse(ok)
        ok, msg = self.c.request_start("teszt")
        self.assertFalse(ok)
        self.assertIn("KILLSWITCH", msg)
        ok, _ = self.c.acknowledge("teszt")
        self.assertFalse(ok)  # still active
        self.io.set_input("killswitch", False)
        self.run_for(1)
        self.assertFalse(self.io.outputs["pump1"])  # latched until acknowledged
        ok, _ = self.c.acknowledge("teszt")
        self.assertTrue(ok)
        self.c.tick()
        self.assertTrue(self.io.outputs["pump1"])  # normal rules again (overheat assist)

    def test_killswitch_overrides_post_circulation_and_emergency(self):
        self.start_running()
        self.c.request_stop("teszt")
        self.c.tick()
        self.assertTrue(self.io.outputs["pump1"])  # stopping: post-circulation
        self.io.set_input("killswitch", True)
        self.c.tick()
        self.assertEqual(self.io.outputs, {"pump1": False, "pump2": False})
        self.assertEqual(self.c.alarm["code"], "E001")

    def test_killswitch_overrides_existing_emergency_and_restart(self):
        self.start_running()
        self.io.set_input("flow1", False)
        self.run_for(3.5)
        self.assertEqual(self.c.alarm["code"], "E102")
        self.io.set_input("killswitch", True)
        self.c.tick()
        self.assertEqual(self.c.alarm["code"], "E001")
        c2 = self._make()
        c2.restore()
        self.assertTrue(c2.killed)
        self.assertEqual(c2._compute_pumps(), {"pump1": False, "pump2": False})

    def test_killswitch_pin_error_trips(self):
        self.start_running()
        self.io.errors["killswitch"] = "GPIO5: read error"
        self.c.tick()
        self.assertEqual(self.c.alarm["code"], "E002")
        self.assertFalse(self.io.outputs["pump1"])

    def test_killswitch_disabled(self):
        self.store.update({"hardware": {"io": {"killswitch": {"enabled": False}}}})
        self.start_running()
        self.io.set_input("killswitch", True)
        self.run_for(1)
        self.assertEqual(self.c.state, CT.RUNNING)

    def test_disabled_button_ignored(self):
        self.store.update({"hardware": {"io": {"btn_start": {"enabled": False}}}})
        self.c.restore()
        self.io.set_input("btn_start", True)
        self.c.tick()
        self.assertEqual(self.c.state, CT.STOPPED)

    # ---------------------------------------------------------- temperatures
    def test_temperature_limit_trips(self):
        self.start_running()
        self.io.temps["t2"]["value"] = 61.0  # default max for T2 is 60 °C
        self.run_for(4.5)
        self.assertEqual(self.c.state, CT.RUNNING)
        self.run_for(0.7)
        self.assertEqual(self.c.state, CT.EMERGENCY)
        self.assertEqual(self.c.alarm["code"], "E202")
        ok, msg = self.c.request_start("teszt")  # start is inhibited while out of range
        self.assertFalse(ok)
        self.assertIn("T2", msg)
        self.io.temps["t2"]["value"] = 40.0
        self.c.tick()
        ok, _ = self.c.request_start("teszt")
        self.assertTrue(ok)
        self.assertIsNone(self.c.alarm)

    def test_temperature_limit_disabled(self):
        self.store.update({"sensors": {"t2": {"limit_enabled": False}}})
        self.start_running()
        self.io.temps["t2"]["value"] = 90.0
        self.run_for(10)
        self.assertEqual(self.c.state, CT.RUNNING)

    def test_sensor_fault_warning_only(self):
        self.start_running()
        self.io.temps["t3"].update(value=None, raw_value=None, fault="open", fault_text="Szakadás")
        self.run_for(6)
        self.assertEqual(self.c.state, CT.RUNNING)
        self.assertTrue(any(w["code"] == "W213" for w in self.c.snapshot()["warnings"]))

    def test_sensor_fault_stops_when_configured(self):
        self.store.update({"sensors": {"t3": {"stop_on_fault": True}}})
        self.start_running()
        self.io.temps["t3"].update(value=None, raw_value=None, fault="short", fault_text="Rövidzár")
        self.run_for(5.2)
        self.assertEqual(self.c.state, CT.EMERGENCY)
        self.assertEqual(self.c.alarm["code"], "E213")

    # ------------------------------------------------------ overheat assist
    def test_overheat_assist_after_stop(self):
        self.c.restore()
        self.c.tick()
        self.assertFalse(self.io.outputs["pump1"])
        self.miners.hot = (64.0, "Hydro-01")
        self.c.tick()
        self.assertEqual(self.io.outputs, {"pump1": True, "pump2": True})
        self.assertEqual(self.miners.forced, 1)
        self.assertIn("I501", self.events.codes())
        self.miners.hot = (50.0, "Hydro-01")  # below threshold - hysteresis, but minimum run time first
        self.run_for(30)
        self.assertTrue(self.io.outputs["pump1"])
        self.run_for(31)
        self.assertFalse(self.io.outputs["pump1"])

    def test_overheat_assist_uses_water_temperature(self):
        self.c.restore()
        self.io.temps["t2"]["value"] = 61.0
        self.c.tick()
        self.assertTrue(self.io.outputs["pump2"])

    def test_overheat_assist_in_emergency_configurable(self):
        self.store.update({"safety": {"overheat_after_emergency": False}})
        self.start_running()
        self.io.set_input("flow1", False)
        self.run_for(3.5)
        self.assertEqual(self.c.state, CT.EMERGENCY)
        self.miners.hot = (70.0, "x")
        self.run_for(1)
        self.assertFalse(self.io.outputs["pump1"])

    # --------------------------------------------------------- manual pumps
    def test_manual_hold_to_run(self):
        self.c.restore()
        ok, _ = self.c.manual_pump("pump1", "press")
        self.assertTrue(ok)
        self.c.tick()
        self.assertEqual(self.io.outputs, {"pump1": True, "pump2": False})
        for _ in range(10):  # keep-alive while the button is held
            self.run_for(0.3)
            self.c.manual_pump("pump1", "hold")
        self.assertTrue(self.io.outputs["pump1"])
        self.run_for(1.6)  # connection lost -> watchdog switches the pump off
        self.assertFalse(self.io.outputs["pump1"])
        self.c.manual_pump("pump2", "press")
        self.c.tick()
        self.c.manual_pump("pump2", "release")
        self.c.tick()
        self.assertFalse(self.io.outputs["pump2"])

    def test_manual_refused_while_running(self):
        self.start_running()
        ok, msg = self.c.manual_pump("pump1", "press")
        self.assertFalse(ok)

    def test_manual_allowed_in_emergency(self):
        self.start_running()
        self.io.set_input("flow1", False)
        self.run_for(3.5)
        self.assertEqual(self.c.state, CT.EMERGENCY)
        ok, _ = self.c.manual_pump("pump1", "press")
        self.assertTrue(ok)
        self.c.tick()
        self.assertTrue(self.io.outputs["pump1"])

    # --------------------------------------------------------- persistence
    def test_ack(self):
        self.start_running()
        self.io.set_input("flow1", False)
        self.run_for(3.5)
        ok, _ = self.c.acknowledge("teszt")
        self.assertTrue(ok)
        self.assertEqual(self.c.state, CT.STOPPED)
        self.assertIsNone(self.c.alarm)
        ok, _ = self.c.acknowledge("teszt")
        self.assertFalse(ok)

    def test_auto_resume_after_restart(self):
        self.start_running()
        c2 = self._make()
        self.assertEqual(c2.initial_outputs(), {"pump1": True, "pump2": True})
        self.c = c2
        c2.restore()
        self.assertEqual(c2.state, CT.STARTING)

    def test_no_auto_resume(self):
        self.store.update({"safety": {"auto_resume": False}})
        self.start_running()
        c2 = self._make()
        self.assertEqual(c2.initial_outputs(), {"pump1": False, "pump2": False})
        c2.restore()
        self.assertEqual(c2.state, CT.STOPPED)

    def test_emergency_survives_restart(self):
        self.start_running()
        self.io.set_input("flow1", False)
        self.run_for(3.5)
        c2 = self._make()
        self.assertEqual(c2.initial_outputs(), {"pump1": False, "pump2": False})
        c2.restore()
        self.assertEqual(c2.state, CT.EMERGENCY)
        self.assertEqual(c2.alarm["code"], "E102")

    def test_gpio_failure_inhibits_start(self):
        self.io.gpio_ok = False
        self.c.restore()
        ok, msg = self.c.request_start("teszt")
        self.assertFalse(ok)
        self.assertIn("GPIO", msg)


if __name__ == "__main__":
    unittest.main()
