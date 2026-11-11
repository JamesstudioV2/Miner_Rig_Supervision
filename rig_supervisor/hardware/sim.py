"""Physics simulator (used when the program does not run on a Raspberry Pi).

It closes the loop for development and demonstration: it reads the simulated
pump outputs, drives the flow switch / push button inputs, integrates a simple
two-loop thermal model (miners -> inner loop -> heat exchanger -> outer loop ->
radiator) and produces thermistor voltages for the simulated ADC.
Faults (flow loss, open/short sensors, miners that refuse to stop...) can be
injected from the web interface to try out the protection functions safely.
"""

from __future__ import annotations

import logging
import random
import threading
import time

from ..config import SENSOR_IDS
from .thermistor import voltage_from_temperature

log = logging.getLogger(__name__)

WATER_CP = 4186.0


class SimMiner:
    def __init__(self, mid: str, name: str, model: str = "Antminer S21 Hyd.",
                 power_w: float = 5360.0, nominal_hs: float = 335e12):
        self.id = mid
        self.name = name
        self.model = model
        self.power_w = power_w
        self.nominal_hs = nominal_hs
        self.mode = "sleep"
        self.hash_start_at = None
        self.chip_temp = 21.0
        self.board_temp = 21.0
        self.offline = False
        self.injected_error = None
        self.ignore_stop = False
        self.booted_at = time.time()

    def producing(self, now: float) -> bool:
        return (not self.offline and self.mode == "normal" and self.hash_start_at is not None
                and now >= self.hash_start_at)

    def set_mode(self, mode: str, now: float) -> None:
        if mode == "sleep":
            if self.ignore_stop:
                return  # simulated faulty miner: accepts the command but keeps hashing
            self.mode = "sleep"
            self.hash_start_at = None
        elif self.mode != "normal":
            self.mode = "normal"
            self.hash_start_at = now + 15.0  # warm-up before hashing starts


class SimWorld:
    C_INNER = 40.0 * WATER_CP
    C_OUTER = 60.0 * WATER_CP
    FLOW_INNER_KGS = 0.7
    FLOW_OUTER_KGS = 0.9

    def __init__(self, config_store):
        self.cfg_store = config_store
        self._lock = threading.RLock()
        self.gpio = None
        self.t_amb = 7.0      # outdoor air at the radiator
        self.t_room = 18.0    # container / hall temperature
        self.ti = 19.0
        self.to = 11.0
        self.temps = {"t1": 19.0, "t2": 19.0, "t3": 11.0, "t4": 11.0}
        self.pump = [False, False]
        self.flow = [False, False]
        self._pump_on_at = [0.0, 0.0]
        self.faults = {"flow1": False, "flow2": False, "t1": None, "t2": None, "t3": None, "t4": None}
        self.miners = {}
        self._buttons = {}
        self._stop = threading.Event()
        self._thread = None

    # ---------------------------------------------------------------- control
    def attach_gpio(self, gpio) -> None:
        self.gpio = gpio

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="simulator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self):
        last = time.monotonic()
        while not self._stop.wait(0.2):
            now = time.monotonic()
            try:
                self.step(min(1.0, now - last))
            except Exception:  # noqa: BLE001
                log.exception("Szimulátor hiba")
            last = now

    # ------------------------------------------------------------ fault inject
    def get_miner(self, mid: str, name: str) -> SimMiner:
        with self._lock:
            m = self.miners.get(mid)
            if m is None:
                m = SimMiner(mid, name)
                m.chip_temp = m.board_temp = self.ti
                self.miners[mid] = m
            m.name = name
            return m

    def press_button(self, name: str, duration: float = 0.4) -> None:
        with self._lock:
            self._buttons[name] = time.monotonic() + duration

    def set_fault(self, key: str, value) -> None:
        with self._lock:
            if key in self.faults:
                self.faults[key] = value

    def state(self) -> dict:
        with self._lock:
            return {
                "faults": dict(self.faults),
                "ambient": self.t_amb,
                "miners": {mid: {"name": m.name, "offline": m.offline, "error": m.injected_error,
                                 "ignore_stop": m.ignore_stop} for mid, m in self.miners.items()},
            }

    # ---------------------------------------------------------------- physics
    def step(self, dt: float) -> None:
        if self.gpio is None:
            return
        cfg = self.cfg_store.data
        io = cfg["hardware"]["io"]
        mono = time.monotonic()
        now = time.time()
        with self._lock:
            for i, name in enumerate(("pump1", "pump2")):
                level = self.gpio.output_level(io[name]["gpio"])
                on = level is not None and level == (io[name]["active"] == "high")
                if on and not self.pump[i]:
                    self._pump_on_at[i] = mono
                self.pump[i] = on
                self.flow[i] = (on and mono - self._pump_on_at[i] >= 1.5
                                and not self.faults["flow%d" % (i + 1)])
            for i, name in enumerate(("flow1", "flow2")):
                active_level = io[name]["active"] == "high"
                self.gpio.drive_external(io[name]["gpio"], active_level if self.flow[i] else not active_level)
            for name in ("btn_start", "btn_stop"):
                active_level = io[name]["active"] == "high"
                pressed = self._buttons.get(name, 0.0) > mono
                self.gpio.drive_external(io[name]["gpio"], active_level if pressed else not active_level)

            f1, f2 = self.flow
            q_miners = sum(m.power_w for m in self.miners.values() if m.producing(now))
            q_water = q_miners * (1.0 if f1 else 0.25)
            k_hx = 1700.0 if (f1 and f2) else (250.0 if (f1 or f2) else 40.0)
            q_hx = k_hx * (self.ti - self.to)
            k_rad = 1400.0 if f2 else 90.0
            q_rad = k_rad * (self.to - self.t_amb)
            q_loss = 25.0 * (self.ti - self.t_room)
            self.ti += (q_water - q_hx - q_loss) / self.C_INNER * dt
            self.to += (q_hx - q_rad) / self.C_OUTER * dt

            dt_miners = q_water / (self.FLOW_INNER_KGS * WATER_CP) if f1 else 0.0
            dt_rad = q_rad / (self.FLOW_OUTER_KGS * WATER_CP) if f2 else 0.0
            target = {
                "t1": self.ti - dt_miners / 2.0,
                "t2": self.ti + dt_miners / 2.0,
                "t3": self.to + dt_rad / 2.0,
                "t4": self.to - dt_rad / 2.0,
            }
            for key, tgt in target.items():
                self.temps[key] += (tgt - self.temps[key]) * min(1.0, dt / 4.0)

            for m in self.miners.values():
                if m.producing(now):
                    tgt, tau = (self.temps["t2"] + 24.0, 25.0) if f1 else (125.0, 70.0)
                else:
                    tgt, tau = self.ti, 90.0
                m.chip_temp += (tgt - m.chip_temp) * min(1.0, dt / tau)
                m.board_temp = self.ti + (m.chip_temp - self.ti) * 0.6

    def adc_voltage(self, channel: int):
        cfg = self.cfg_store.data
        div = cfg["hardware"]["divider"]
        vs = float(div["supply_v"])
        for sid in SENSOR_IDS:
            s = cfg["sensors"][sid]
            if int(s["channel"]) != channel:
                continue
            with self._lock:
                fault = self.faults.get(sid)
                temp = self.temps[sid]
            if fault == "open":
                return vs * 0.999
            if fault == "short":
                return vs * 0.002
            v = voltage_from_temperature(temp, vs, float(div["series_ohm"]), float(s["r25"]), float(s["beta"]))
            return v + random.gauss(0.0, 0.0006)
        return vs * 0.999  # nothing connected to this channel
