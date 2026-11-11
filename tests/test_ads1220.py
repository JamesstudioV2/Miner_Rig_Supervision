"""ADS1220 driver against a fake spidev that emulates the chip's register and conversion behaviour."""

import sys
import types
import unittest
from unittest import mock

from rig_supervisor.hardware import adc as A
from rig_supervisor.hardware.thermistor import evaluate, voltage_from_temperature


class FakeADS1220:
    def __init__(self):
        self.regs = [0, 0, 0, 0]
        self.inputs = [0.0, 0.0, 0.0, 0.0]  # volts on AIN0..3
        self.avdd = 5.0
        self.mode = None
        self.log = []
        self.converted = 0

    # spidev.SpiDev API
    def open(self, bus, dev):
        self.opened = (bus, dev)

    def close(self):
        pass

    def xfer2(self, data):
        self.log.append(list(data))
        cmd = data[0]
        if cmd == 0x06:
            self.regs = [0, 0, 0, 0]
        elif cmd & 0xF0 == 0x40:
            start, n = (cmd >> 2) & 3, (cmd & 3) + 1
            self.regs[start:start + n] = data[1:1 + n]
        elif cmd & 0xF0 == 0x20:
            start, n = (cmd >> 2) & 3, (cmd & 3) + 1
            return [0] + self.regs[start:start + n]
        elif cmd == 0x08:
            ch = (self.regs[0] >> 4) - 8
            assert 0 <= ch <= 3 and self.regs[0] & 1, "single-ended, PGA bypass expected"
            vref = {0: 2.048, 3: self.avdd}[self.regs[2] >> 6]
            code = int(round(self.inputs[ch] / vref * (1 << 23)))
            self.converted = max(-(1 << 23), min((1 << 23) - 1, code)) & 0xFFFFFF
        elif cmd == 0x10:
            c = self.converted
            return [0, (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF]
        return [0] * len(data)


class ADS1220Test(unittest.TestCase):
    def setUp(self):
        self.chip = FakeADS1220()
        mod = types.ModuleType("spidev")
        mod.SpiDev = lambda: self.chip
        self.patch = mock.patch.dict(sys.modules, {"spidev": mod})
        self.patch.start()
        self.sleep = mock.patch.object(A.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.patch.stop()

    def test_ratiometric_measurement(self):
        adc, err = A.create_adc({"type": "ads1220", "spi_bus": 0, "spi_device": 0, "ref": "avdd", "vref": 3.3}, 5.0)
        self.assertIsNone(err)
        self.assertEqual(self.chip.mode, 1)  # SPI mode 1
        temps = [12.0, 25.0, 41.5, 60.0]
        for ch, t in enumerate(temps):
            self.chip.inputs[ch] = voltage_from_temperature(t, 5.0, 10000, 10000, 3950)
        for ch, t in enumerate(temps):
            v, sat = adc.read(ch)
            self.assertFalse(sat)
            r = evaluate(v, 5.0, 10000, 10000, 3950)
            self.assertAlmostEqual(r["value"], t, places=2)
        self.chip.inputs[2] = 5.0  # open sensor -> full scale
        v, sat = adc.read(2)
        self.assertTrue(sat)
        self.assertEqual(evaluate(v, 5.0, 10000, 10000, 3950, saturated=sat)["fault"], "open")

    def test_not_responding(self):
        self.chip.xfer2 = lambda data: [0] * len(data)
        adc, err = A.create_adc({"type": "ads1220", "spi_bus": 0, "spi_device": 0, "ref": "avdd", "vref": 3.3}, 3.3)
        self.assertIsInstance(adc, A.NoADC)
        self.assertIn("nem válaszol", err)


if __name__ == "__main__":
    unittest.main()
