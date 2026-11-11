"""Analog-digital converters for the NTC thermistors.

The Raspberry Pi has NO analog input, so the thermistor voltage dividers must be
read through an external ADC:

  * ADS1115 (I²C, 16 bit, 4 channels)  - needs ``python3-smbus`` or ``smbus2``
  * MCP3008 (SPI, 10 bit, 8 channels)  - needs ``python3-spidev``
  * MCP3208 (SPI, 12 bit, 8 channels)  - needs ``python3-spidev``
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class ADCError(Exception):
    pass


class BaseADC:
    name = "none"
    channels = 8

    def read(self, channel: int):
        """Return ``(voltage, saturated)``."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class NoADC(BaseADC):
    name = "nincs"

    def read(self, channel):
        raise ADCError("nincs ADC beállítva")


class ADS1115(BaseADC):
    name = "ADS1115"
    channels = 4
    _PGA = {6.144: 0, 4.096: 1, 2.048: 2, 1.024: 3, 0.512: 4, 0.256: 5}
    _RATES = {8: 0, 16: 1, 32: 2, 64: 3, 128: 4, 250: 5, 475: 6, 860: 7}

    def __init__(self, bus: int = 1, address: int = 0x48, full_scale: float = 4.096, data_rate: int = 250):
        try:
            from smbus2 import SMBus  # type: ignore
        except ImportError:
            try:
                from smbus import SMBus  # type: ignore
            except ImportError:
                raise ADCError("hiányzik az smbus könyvtár (sudo apt install python3-smbus)")
        try:
            self.bus = SMBus(bus)
        except (OSError, IOError) as exc:
            raise ADCError("az I²C busz (%d) nem nyitható meg - engedélyezd a raspi-config-ban (%s)" % (bus, exc))
        self.address = address
        self.full_scale = full_scale
        self.pga_bits = self._PGA[full_scale]
        self.data_rate = data_rate
        self.rate_bits = self._RATES[data_rate]
        self._lock = threading.Lock()
        # probe the chip once so a wrong address is reported immediately
        try:
            self.bus.read_i2c_block_data(self.address, 0x01, 2)
        except (OSError, IOError) as exc:
            raise ADCError("az ADS1115 nem válaszol a 0x%02X címen (%s)" % (address, exc))

    def read(self, channel):
        if not 0 <= channel <= 3:
            raise ADCError("érvénytelen ADS1115 csatorna: %d" % channel)
        config = (1 << 15) | ((0x4 + channel) << 12) | (self.pga_bits << 9) | (1 << 8) | (self.rate_bits << 5) | 0x03
        with self._lock:
            try:
                self.bus.write_i2c_block_data(self.address, 0x01, [(config >> 8) & 0xFF, config & 0xFF])
                time.sleep(1.0 / self.data_rate + 0.0008)
                deadline = time.monotonic() + 0.1
                while True:
                    hi, _lo = self.bus.read_i2c_block_data(self.address, 0x01, 2)
                    if hi & 0x80:
                        break
                    if time.monotonic() > deadline:
                        raise ADCError("ADS1115 konverziós időtúllépés")
                    time.sleep(0.001)
                hi, lo = self.bus.read_i2c_block_data(self.address, 0x00, 2)
            except (OSError, IOError) as exc:
                raise ADCError("I²C kommunikációs hiba: %s" % exc)
        raw = (hi << 8) | lo
        if raw & 0x8000:
            raw -= 1 << 16
        return raw * self.full_scale / 32768.0, raw >= 32767

    def close(self):
        try:
            self.bus.close()
        except Exception:  # noqa: BLE001
            pass


class MCP3x08(BaseADC):
    def __init__(self, bits: int = 10, bus: int = 0, device: int = 0, vref: float = 3.3, speed_hz: int = 500000):
        try:
            import spidev  # type: ignore
        except ImportError:
            raise ADCError("hiányzik a spidev könyvtár (sudo apt install python3-spidev)")
        self.bits = bits
        self.name = "MCP3008" if bits == 10 else "MCP3208"
        self.channels = 8
        self.full = 1 << bits
        self.vref = vref
        self._lock = threading.Lock()
        self.spi = spidev.SpiDev()
        try:
            self.spi.open(bus, device)
        except (OSError, IOError) as exc:
            raise ADCError("az SPI eszköz (/dev/spidev%d.%d) nem nyitható meg - engedélyezd az SPI-t (%s)"
                           % (bus, device, exc))
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0

    def read(self, channel):
        if not 0 <= channel <= 7:
            raise ADCError("érvénytelen csatorna: %d" % channel)
        with self._lock:
            try:
                if self.bits == 10:
                    r = self.spi.xfer2([1, (8 + channel) << 4, 0])
                    raw = ((r[1] & 0x03) << 8) | r[2]
                else:
                    r = self.spi.xfer2([0x06 | ((channel & 0x04) >> 2), (channel & 0x03) << 6, 0])
                    raw = ((r[1] & 0x0F) << 8) | r[2]
            except (OSError, IOError) as exc:
                raise ADCError("SPI kommunikációs hiba: %s" % exc)
        return raw * self.vref / self.full, raw >= self.full - 1

    def close(self):
        try:
            self.spi.close()
        except Exception:  # noqa: BLE001
            pass


class SimADC(BaseADC):
    name = "szimulált ADC"
    channels = 8

    def __init__(self, world):
        self.world = world

    def read(self, channel):
        v = self.world.adc_voltage(channel)
        if v is None:
            raise ADCError("szimulált ADC hiba")
        return v, False


def create_adc(adc_cfg: dict, supply_v: float, sim_world=None):
    """Return ``(adc, error_message_or_None)``."""
    if sim_world is not None:
        return SimADC(sim_world), None
    kind = adc_cfg.get("type")
    try:
        if kind == "ads1115":
            full_scale = 6.144 if supply_v > 4.0 else 4.096
            return ADS1115(int(adc_cfg["i2c_bus"]), int(adc_cfg["i2c_address"]), full_scale), None
        if kind in ("mcp3008", "mcp3208"):
            return MCP3x08(10 if kind == "mcp3008" else 12, int(adc_cfg["spi_bus"]),
                           int(adc_cfg["spi_device"]), float(adc_cfg["vref"])), None
        return NoADC(), "Nincs ADC beállítva – a hőmérséklet nem mérhető"
    except ADCError as exc:
        log.error("ADC inicializálás sikertelen: %s", exc)
        return NoADC(), "ADC hiba: %s" % exc
    except Exception as exc:  # noqa: BLE001
        log.exception("ADC inicializálás sikertelen")
        return NoADC(), "ADC hiba: %s" % exc
