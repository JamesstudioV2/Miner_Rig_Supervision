import copy
import os
import tempfile
import unittest

from rig_supervisor import config as C
from rig_supervisor.hardware import thermistor as T


class ThermistorTest(unittest.TestCase):
    def test_roundtrip_5v_and_3v3(self):
        for vs in (5.0, 3.3):
            for temp in (-10.0, 0.0, 25.0, 42.5, 80.0):
                v = T.voltage_from_temperature(temp, vs, 10000, 10000, 3950)
                r = T.evaluate(v, vs, 10000, 10000, 3950)
                self.assertIsNone(r["fault"])
                self.assertAlmostEqual(r["value"], temp, places=6)

    def test_25c_is_half_supply(self):
        v = T.voltage_from_temperature(25.0, 5.0, 10000, 10000, 3950)
        self.assertAlmostEqual(v, 2.5, places=6)

    def test_faults(self):
        self.assertEqual(T.evaluate(4.99, 5.0, 10000, 10000, 3950)["fault"], "open")
        self.assertEqual(T.evaluate(0.01, 5.0, 10000, 10000, 3950)["fault"], "short")
        self.assertEqual(T.evaluate(2.0, 5.0, 10000, 10000, 3950, saturated=True)["fault"], "open")
        self.assertEqual(T.evaluate(None, 5.0, 10000, 10000, 3950)["fault"], "adc")
        # 160 °C equivalent -> outside the valid range
        v = T.voltage_from_temperature(160.0, 5.0, 10000, 10000, 3950)
        self.assertEqual(T.evaluate(v, 5.0, 10000, 10000, 3950)["fault"], "range")

    def test_calibration(self):
        v = T.voltage_from_temperature(30.0, 3.3, 10000, 10000, 3950)
        r = T.evaluate(v, 3.3, 10000, 10000, 3950, offset=-0.5, gain=1.01)
        self.assertAlmostEqual(r["raw_value"], 30.0, places=6)
        self.assertAlmostEqual(r["value"], 30.0 * 1.01 - 0.5, places=6)


class ConfigTest(unittest.TestCase):
    def test_defaults_valid(self):
        cfg, errors = C.validate(C.default_config())
        self.assertEqual(errors, [])

    def test_duplicate_gpio(self):
        cfg = C.default_config()
        cfg["hardware"]["io"]["pump2"]["gpio"] = cfg["hardware"]["io"]["pump1"]["gpio"]
        _, errors = C.validate(cfg)
        self.assertTrue(any("kétszer" in e for e in errors), errors)

    def test_bus_pin_conflict(self):
        cfg = C.default_config()
        self.assertEqual(cfg["hardware"]["adc"]["type"], "ads1220")
        cfg["hardware"]["io"]["flow1"]["gpio"] = 8  # SPI CE0 used by the ADS1220
        _, errors = C.validate(cfg)
        self.assertTrue(any("ADC buszhoz" in e for e in errors), errors)
        cfg["hardware"]["adc"]["type"] = "ads1115"
        cfg["hardware"]["io"]["flow1"]["gpio"] = 2  # I2C SDA used by the ADS1115
        _, errors = C.validate(cfg)
        self.assertTrue(any("ADC buszhoz" in e for e in errors), errors)
        cfg["hardware"]["adc"]["type"] = "mcp3008"  # SPI does not use GPIO2
        _, errors = C.validate(cfg)
        self.assertEqual(errors, [])
        cfg["hardware"]["io"]["flow1"]["gpio"] = 10  # SPI MOSI
        _, errors = C.validate(cfg)
        self.assertTrue(errors)

    def test_limits_and_channels(self):
        cfg = C.default_config()
        cfg["sensors"]["t1"]["min"] = 70
        cfg["sensors"]["t2"]["channel"] = cfg["sensors"]["t1"]["channel"]
        _, errors = C.validate(cfg)
        self.assertTrue(any("minimum" in e for e in errors))
        self.assertTrue(any("csatornát" in e for e in errors))
        cfg = C.default_config()
        cfg["sensors"]["t3"]["channel"] = 5  # ADS1115 only has 4 channels
        _, errors = C.validate(cfg)
        self.assertTrue(errors)

    def test_miner_list_normalised(self):
        cfg = C.default_config()
        cfg["miners"]["list"] = [{"host": "192.168.1.50", "type": "antminer", "api_port": "4028"},
                                 {"host": "bad host!", "type": "antminer"}]
        out, errors = C.validate(cfg)
        self.assertEqual(len(errors), 1)
        m = out["miners"]["list"][0]
        self.assertTrue(m["id"].startswith("m"))
        self.assertEqual(m["name"], "192.168.1.50")
        self.assertEqual(m["api_port"], 4028)

    def test_store_roundtrip_and_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            store = C.ConfigStore(path)
            self.assertTrue(store.created)
            seen = []
            store.add_listener(lambda old, new: seen.append((old["safety"]["post_stop_pump_s"], new["safety"]["post_stop_pump_s"])))
            store.update({"safety": {"post_stop_pump_s": 45}})
            self.assertEqual(seen, [(30.0, 45.0)])
            with self.assertRaises(C.ConfigError):
                store.update({"safety": {"post_stop_pump_s": -1}})
            self.assertEqual(store.data["safety"]["post_stop_pump_s"], 45.0)
            # a corrupted main file falls back to the backup
            with open(path, "w") as f:
                f.write("{broken")
            store2 = C.ConfigStore(path)
            self.assertFalse(store2.created)
            self.assertTrue(store2.load_warnings)
            self.assertEqual(store2.data["safety"]["post_stop_pump_s"], 30.0)  # .bak holds the previous version

    def test_published_dict_not_mutated(self):
        with tempfile.TemporaryDirectory() as d:
            store = C.ConfigStore(os.path.join(d, "config.json"))
            before = copy.deepcopy(store.data)
            ref = store.data
            store.update({"site_name": "Teszt"})
            self.assertEqual(ref, before)
            self.assertEqual(store.data["site_name"], "Teszt")


if __name__ == "__main__":
    unittest.main()
