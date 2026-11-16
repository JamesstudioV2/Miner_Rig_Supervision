"""IO manager: owns every GPIO pin and the ADC.

 * digital inputs are polled by the controller (20 Hz) and debounced here,
 * the 4 thermistors are sampled by a background thread once per second,
 * pin assignments can be changed live from the web interface.
"""

from __future__ import annotations

import collections
import logging
import statistics
import threading
import time

from ..config import GPIO_TO_PIN, IO_INPUTS, IO_LABELS, IO_OUTPUTS, SENSOR_IDS
from . import owner, thermistor
from .adc import ADCError, NoADC, create_adc
from .gpio import NullGPIOBackend, create_gpio_backend

log = logging.getLogger(__name__)

DEBOUNCE_S = {"flow1": 0.3, "flow2": 0.3, "btn_start": 0.05, "btn_stop": 0.05, "killswitch": 0.03}
REASSERT_S = 5.0          # outputs are re-written periodically (glitch recovery)
NO_GPIO_TEXT = "nincs használható GPIO könyvtár"
READBACK_S = 1.0          # outputs are read back periodically (does the pin really follow?)
MISMATCH_AFTER = 3        # consecutive differing read-backs before a mismatch is reported
SENSOR_PERIOD_S = 1.0
EMA_ALPHA = 0.5
ADC_RETRY_S = 30.0
OWNER_RETRY_S = 10.0      # a blocked copy checks this often whether the other copy has stopped
FLAP_WINDOW_S = 60.0      # outputs / inputs that change on their own this often are reported
OUTPUT_FLAPS = 3          # read-backs differing from the commanded level within the window
INPUT_FLAPS = 12          # debounced input changes within the window


def _polarity_text(active: str) -> str:
    return "magas = aktív (normál)" if active == "high" else "alacsony = aktív (negált)"


class Debouncer:
    def __init__(self, initial: bool, delay_s: float):
        self.stable = initial
        self.delay = delay_s
        self._candidate = initial
        self._since = None

    def update(self, raw: bool, now: float):
        """Feed a raw sample; return the new stable value on a change, else None."""
        if raw == self.stable:
            self._since = None
            return None
        if self._since is None or raw != self._candidate:
            self._candidate = raw
            self._since = now
            return None
        if now - self._since >= self.delay:
            self.stable = raw
            self._since = None
            return raw
        return None


class IOManager:
    def __init__(self, config_store, events, use_sim: bool, sim_world=None):
        self.cfg_store = config_store
        self.events = events
        self.use_sim = use_sim
        self.sim = sim_world
        self._lock = threading.RLock()
        self.gpio = None
        self.gpio_name = ""
        self.gpio_errors = []
        self.pin_errors = {}
        self._outputs = {name: False for name in IO_OUTPUTS}
        self._out_written = {}
        self._out_pins = {}
        self._in_pins = {}
        self._debouncers = {}
        self._raw_levels = {}
        self._inputs = {name: False for name in IO_INPUTS}
        self._last_reassert = 0.0
        self._last_readback = -1e9
        self._out_actual = {}
        self._mismatch = {}
        self._mismatch_times = {}
        self._input_changes = {}
        self.pin_warnings = {}
        self.owner_blocked = False
        self._owns_gpio = False
        self._owner_retry_at = 0.0
        self.adc = None
        self.adc_error = None
        self._adc_cfg_key = None
        self._adc_retry_at = 0.0
        self._temps_lock = threading.Lock()
        self._temps = {sid: self._empty_reading() for sid in SENSOR_IDS}
        self._ema = {}
        self._sensor_thread = None
        self._stop = threading.Event()
        config_store.add_listener(self._on_config_change)

    @staticmethod
    def _empty_reading():
        return {"value": None, "raw_value": None, "voltage": None, "ohm": None, "fault": "adc",
                "fault_text": thermistor.FAULT_TEXT["adc"], "ts": 0.0}

    # ------------------------------------------------------------------ setup
    def start(self, initial_outputs: dict) -> None:
        with self._lock:
            for name, on in initial_outputs.items():
                self._outputs[name] = bool(on)
            self._open_backend()
        self._sensor_thread = threading.Thread(target=self._sensor_loop, name="sensors", daemon=True)
        self._sensor_thread.start()

    def _open_backend(self) -> None:
        """Create the GPIO backend + ADC for the current mode and configure every pin."""
        self.owner_blocked = False
        if not self.use_sim and not self._owns_gpio:
            ok, other = owner.acquire()
            if not ok:
                # another copy of the program drives the pins: never touch them (nor the ADC)
                self.owner_blocked = True
                self._owner_retry_at = time.monotonic() + OWNER_RETRY_S
                log.error("A GPIO vezérlés nem vehető át: %s", other)
                self.gpio, self.gpio_errors = NullGPIOBackend(other), [other]
                self.gpio_name = self.gpio.name
                self.pin_errors, self.pin_warnings = {}, {}
                self._out_pins, self._in_pins = {}, {}
                self._out_written, self._raw_levels = {}, {}
                for name in IO_OUTPUTS + IO_INPUTS:
                    self.pin_errors[name] = "másik példány vezérli"
                    if name in IO_INPUTS:
                        self._debouncers[name] = Debouncer(False, DEBOUNCE_S[name])
                        self._inputs[name] = False
                if self.adc is not None:
                    self.adc.close()
                self.adc, self.adc_error = NoADC(), "az ADC-t egy másik Rig Supervisor példány használja"
                self._adc_cfg_key = self._adc_key(self.cfg_store.data)
                self._adc_retry_at = float("inf")
                return
            self._owns_gpio = True
        self.gpio, self.gpio_errors = create_gpio_backend("sim" if self.use_sim else "rpi")
        self.gpio_name = self.gpio.name
        self.pin_warnings = {}
        if self.sim is not None:
            self.sim.attach_gpio(self.gpio)
        self.pin_errors = {}
        self._out_pins, self._in_pins = {}, {}
        self._out_written, self._raw_levels = {}, {}
        self._out_actual, self._mismatch = {}, {}
        self._mismatch_times, self._input_changes = {}, {}
        self._last_readback = -1e9
        cfg = self.cfg_store.data
        for name in IO_OUTPUTS:
            self._setup_output(name, cfg["hardware"]["io"][name])
        for name in IO_INPUTS:
            self._setup_input(name, cfg["hardware"]["io"][name])
        self._init_adc(cfg)
        if self.use_sim:
            log.info("Hardver mód: SZIMULÁCIÓ - a GPIO lábak nem változnak")
        else:
            log.info("Hardver mód: valódi hardver, GPIO könyvtár: %s, kimenetek: %s", self.gpio_name, ", ".join(
                "%s=GPIO%d" % (n, p[0]) for n, p in self._out_pins.items()))

    def switch_backend(self, use_sim: bool, sim_world=None) -> None:
        """Switch between simulation and the real hardware at runtime.

        The pins of the old backend are driven inactive and released first; the logical
        output states are carried over to the new backend.
        """
        with self._lock:
            if self.gpio is not None and self.gpio.name != "nincs":
                for name, (pin, active_high) in list(self._out_pins.items()):
                    self._release(name, pin, inactive_level=(not active_high))
                for name, (pin, _active_low) in list(self._in_pins.items()):
                    self._release(name, pin)
                try:
                    self.gpio.close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("GPIO lezárási hiba: %s", exc)
            if self._owns_gpio and use_sim:
                owner.release()
                self._owns_gpio = False
            self.use_sim, self.sim = bool(use_sim), sim_world
            self._ema = {}
            self._open_backend()

    def stop(self) -> None:
        self._stop.set()
        if self._sensor_thread:
            self._sensor_thread.join(timeout=3)
        with self._lock:
            if self.adc:
                self.adc.close()
            # GPIO outputs are deliberately left untouched (see module docstring of gpio.py)
            if self._owns_gpio:
                owner.release()
                self._owns_gpio = False

    @property
    def gpio_ok(self) -> bool:
        return self.gpio is not None and self.gpio.name != "nincs" and not any(
            n in self.pin_errors for n in IO_OUTPUTS)

    def _check_conflict(self, name, pin) -> None:
        try:
            other = self.gpio.conflict(pin)
        except Exception:  # noqa: BLE001 - the check is informative only
            other = None
        if other:
            self.pin_warnings[name] = "GPIO%d: %s" % (pin, other)
            log.warning("%s (GPIO%d): %s", IO_LABELS[name], pin, other)
        else:
            self.pin_warnings.pop(name, None)

    def _setup_output(self, name, item):
        pin, active_high = int(item["gpio"]), item["active"] == "high"
        level = self._outputs[name] == active_high
        self._check_conflict(name, pin)
        try:
            self.gpio.setup_output(pin, level)
            self._out_pins[name] = (pin, active_high)
            self._out_written[name] = self._outputs[name]
            self.pin_errors.pop(name, None)
        except Exception as exc:  # noqa: BLE001
            self._out_pins[name] = (pin, active_high)
            self._pin_failed(name, pin, exc)

    def _pin_failed(self, name, pin, exc) -> None:
        if self.gpio.name == "nincs":  # no library at all: reported once through gpio_errors
            self.pin_errors[name] = NO_GPIO_TEXT
        else:
            self.pin_errors[name] = "GPIO%d: %s" % (pin, exc)
            log.error("%s (GPIO%d) beállítása sikertelen: %s", IO_LABELS[name], pin, exc)

    def _setup_input(self, name, item):
        pin, active_low = int(item["gpio"]), item["active"] == "low"
        self._check_conflict(name, pin)
        try:
            self.gpio.setup_input(pin, item.get("pull", "off"))
            self._in_pins[name] = (pin, active_low)
            self.pin_errors.pop(name, None)
            time.sleep(0.002)
            level = self.gpio.read(pin)
            logical = (not level) if active_low else bool(level)
            self._raw_levels[name] = level
        except Exception as exc:  # noqa: BLE001
            self._in_pins[name] = (pin, active_low)
            self._pin_failed(name, pin, exc)
            logical = False
        # no edge at start-up: the debouncer starts from the current level
        self._debouncers[name] = Debouncer(logical, DEBOUNCE_S[name])
        self._inputs[name] = logical

    def _invert_output(self, name, item):
        """New active level on the same output pin: rewrite the level, keep the logical state."""
        pin, active_high = int(item["gpio"]), item["active"] == "high"
        self._out_pins[name] = (pin, active_high)
        on = self._outputs[name]
        try:
            self.gpio.write(pin, on == active_high)
            self._out_written[name] = on
            self.pin_errors.pop(name, None)
        except Exception as exc:  # noqa: BLE001 - set_outputs() retries the pin setup
            self._out_written.pop(name, None)
            self.pin_errors[name] = str(exc)
            log.error("%s (GPIO%d) írása sikertelen: %s", IO_LABELS[name], pin, exc)

    def _invert_input(self, name, item):
        """New active level on the same input pin.

        The debouncer restarts from the new logical state WITHOUT an edge: flipping the
        polarity of a push button must never be taken as a START/STOP press.
        """
        pin, active_low = int(item["gpio"]), item["active"] == "low"
        self._in_pins[name] = (pin, active_low)
        level = self._raw_levels.get(name)
        if name not in self.pin_errors:
            try:
                level = self.gpio.read(pin)
                self._raw_levels[name] = level
            except Exception as exc:  # noqa: BLE001 - reported by poll_inputs() as well
                log.error("%s (GPIO%d) olvasása sikertelen: %s", IO_LABELS[name], pin, exc)
                level = None
        logical = False if level is None else ((not level) if active_low else bool(level))
        self._debouncers[name] = Debouncer(logical, DEBOUNCE_S[name])
        self._inputs[name] = logical

    def _release(self, name, pin, inactive_level=None):
        try:
            if inactive_level is not None:
                self.gpio.write(pin, inactive_level)
            self.gpio.release(pin)
        except Exception as exc:  # noqa: BLE001
            log.warning("GPIO%d felszabadítása sikertelen: %s", pin, exc)

    def _adc_key(self, cfg):
        return repr((cfg["hardware"]["adc"], cfg["hardware"]["divider"]["supply_v"]))

    def _init_adc(self, cfg):
        if self.adc is not None:
            self.adc.close()
        self.adc, self.adc_error = create_adc(cfg["hardware"]["adc"], cfg["hardware"]["divider"]["supply_v"],
                                              self.sim if self.use_sim else None)
        self._adc_cfg_key = self._adc_key(cfg)
        self._adc_retry_at = time.monotonic() + ADC_RETRY_S

    # ------------------------------------------------------- live reconfigure
    def _on_config_change(self, old, new):
        if self.gpio is None or self.owner_blocked:
            return
        with self._lock:
            old_io, new_io = old["hardware"]["io"], new["hardware"]["io"]
            # pin (or pull resistor) changed -> the pin has to be released and set up again
            changed_out = [n for n in IO_OUTPUTS if old_io[n]["gpio"] != new_io[n]["gpio"]]
            changed_in = [n for n in IO_INPUTS if (old_io[n]["gpio"], old_io[n]["pull"]) !=
                          (new_io[n]["gpio"], new_io[n]["pull"])]
            # only the active level changed (software inversion) -> handled in place, without
            # releasing the pin, so a running pump does not even glitch
            inverted_out = [n for n in IO_OUTPUTS if n not in changed_out
                            and old_io[n]["active"] != new_io[n]["active"]]
            inverted_in = [n for n in IO_INPUTS if n not in changed_in
                           and old_io[n]["active"] != new_io[n]["active"]]
            for n in inverted_out:
                self._invert_output(n, new_io[n])
            for n in inverted_in:
                self._invert_input(n, new_io[n])
            if inverted_out or inverted_in:
                self.events.add("info", "Aktív szint (negálás) módosítva: " + ", ".join(
                    "%s → %s" % (IO_LABELS[n], _polarity_text(new_io[n]["active"]))
                    for n in inverted_out + inverted_in), source="beállítások")
            if changed_out or changed_in:
                # 1) release the old pins (validation guarantees that an unchanged
                #    function never uses a pin that is released here)
                for n in changed_out:
                    pin, active_high = self._out_pins.get(n, (old_io[n]["gpio"], old_io[n]["active"] == "high"))
                    # drive the old pin to the inactive level before letting it go
                    self._release(n, pin, inactive_level=(not active_high))
                for n in changed_in:
                    pin = self._in_pins.get(n, (old_io[n]["gpio"], True))[0]
                    self._release(n, pin)
                # 2) set up the new assignments
                for n in changed_out:
                    self._setup_output(n, new_io[n])
                for n in changed_in:
                    self._setup_input(n, new_io[n])
                self.events.add("info", "GPIO kiosztás módosítva: " + ", ".join(
                    "%s → GPIO%d, %s" % (IO_LABELS[n], new_io[n]["gpio"], _polarity_text(new_io[n]["active"]))
                    for n in changed_out + changed_in), source="beállítások")
            if self._adc_key(new) != self._adc_cfg_key:
                self._init_adc(new)
                self.events.add("info", "ADC beállítások módosítva (%s)" % self.adc.name, source="beállítások")

    # ------------------------------------------------------------- digital IO
    def poll_inputs(self, now: float):
        """Sample and debounce all digital inputs.

        Returns ``(states, edges)`` where ``edges`` is a list of ``(name, new_state)``.
        """
        edges = []
        with self._lock:
            for name in IO_INPUTS:
                pin_info = self._in_pins.get(name)
                if pin_info is None or name in self.pin_errors:
                    continue
                pin, active_low = pin_info
                try:
                    level = self.gpio.read(pin)
                except Exception as exc:  # noqa: BLE001
                    # fail-safe: an unreadable flow switch means "no flow", a button "not pressed"
                    self.pin_errors[name] = str(exc)
                    log.error("%s olvasási hiba: %s", IO_LABELS[name], exc)
                    if self._inputs[name]:
                        self._inputs[name] = False
                        self._debouncers[name] = Debouncer(False, DEBOUNCE_S[name])
                        edges.append((name, False))
                    continue
                self._raw_levels[name] = level
                logical = (not level) if active_low else bool(level)
                changed = self._debouncers[name].update(logical, now)
                if changed is not None:
                    self._inputs[name] = changed
                    edges.append((name, changed))
                    times = self._input_changes.setdefault(name, collections.deque())
                    times.append(now)
                    while times and now - times[0] > FLAP_WINDOW_S:
                        times.popleft()
            return dict(self._inputs), edges

    def set_outputs(self, states: dict, now: float) -> None:
        with self._lock:
            if self.owner_blocked:
                for name, on in states.items():
                    self._outputs[name] = bool(on)
                if now >= self._owner_retry_at:
                    self._owner_retry_at = now + OWNER_RETRY_S
                    ok, _other = owner.acquire()
                    if ok:
                        self._owns_gpio = True
                        log.warning("A másik példány leállt – a GPIO vezérlést ez a példány vette át")
                        self._open_backend()
                        self.events.add("warning", "A másik Rig Supervisor példány leállt – a GPIO lábak és az ADC "
                                                   "vezérlését ez a példány vette át", source="hardver")
                return
            # read back BEFORE writing: shows what happened to the pins since the last write
            # (a pin that some other program overwrote in the meantime)
            if now - self._last_readback >= READBACK_S or now < self._last_readback:
                self._last_readback = now
                self._readback()
            reassert = now - self._last_reassert >= REASSERT_S
            if reassert:
                self._last_reassert = now
            for name, on in states.items():
                on = bool(on)
                self._outputs[name] = on
                if self._out_written.get(name) == on and not reassert:
                    continue
                pin_info = self._out_pins.get(name)
                if pin_info is None:
                    continue
                pin, active_high = pin_info
                try:
                    self.gpio.write(pin, on == active_high)
                    if self._out_written.get(name) != on and not self.use_sim:
                        log.info("%s (GPIO%d) -> %s (láb: %d)", IO_LABELS[name], pin, "BE" if on else "KI",
                                 1 if on == active_high else 0)
                    self._out_written[name] = on
                    if name in self.pin_errors:
                        self.pin_errors.pop(name, None)
                except Exception as exc:  # noqa: BLE001
                    if name not in self.pin_errors:
                        log.error("%s írása sikertelen: %s", IO_LABELS[name], exc)
                    self.pin_errors[name] = str(exc)
                    # try to configure the pin again next time
                    try:
                        self.gpio.setup_output(pin, on == active_high)
                        self._out_written[name] = on
                        self.pin_errors.pop(name, None)
                    except Exception:  # noqa: BLE001
                        pass

    def _readback(self) -> None:
        """Read the output pins back and count how long they differ from the commanded level."""
        for name, (pin, active_high) in self._out_pins.items():
            if name in self.pin_errors:
                self._out_actual[name], self._mismatch[name] = None, 0
                continue
            try:
                level = bool(self.gpio.read(pin))
            except Exception:  # noqa: BLE001 - reading an output back is optional
                self._out_actual[name], self._mismatch[name] = None, 0
                continue
            self._out_actual[name] = level
            written = self._out_written.get(name)
            if written is None:
                continue
            if level != (written == active_high):
                self._mismatch[name] = self._mismatch.get(name, 0) + 1
                # differs now and then (not continuously): something else writes the pin too
                times = self._mismatch_times.setdefault(name, collections.deque())
                times.append(time.monotonic())
            else:
                self._mismatch[name] = 0
            times = self._mismatch_times.get(name)
            while times and time.monotonic() - times[0] > FLAP_WINDOW_S:
                times.popleft()

    def input_error(self, name: str):
        """Setup/read error of an input pin (None when the pin works)."""
        with self._lock:
            return self.pin_errors.get(name)

    def outputs(self) -> dict:
        with self._lock:
            return dict(self._outputs)

    def pin_states(self) -> dict:
        """Electrical + logical state of every pin (for the calibration page)."""
        cfg = self.cfg_store.data["hardware"]["io"]
        out = {}
        with self._lock:
            for name in IO_OUTPUTS + IO_INPUTS:
                gpio = cfg[name]["gpio"]
                item = {"gpio": gpio, "pin": GPIO_TO_PIN.get(gpio), "label": IO_LABELS[name],
                        "direction": "out" if name in IO_OUTPUTS else "in",
                        "error": self.pin_errors.get(name), "warning": self.pin_warnings.get(name)}
                if name in IO_OUTPUTS:
                    on = self._outputs.get(name, False)
                    item["active"] = on
                    item["level"] = on == (cfg[name]["active"] == "high")  # commanded level
                    actual = None
                    if name not in self.pin_errors and name in self._out_pins:
                        try:
                            actual = bool(self.gpio.read(self._out_pins[name][0]))  # level on the pin itself
                        except Exception:  # noqa: BLE001
                            actual = None
                    item["actual"] = actual
                    item["mismatch"] = actual is not None and actual != item["level"]
                else:
                    item["active"] = self._inputs.get(name, False)
                    item["level"] = self._raw_levels.get(name)
                out[name] = item
        return out

    # ---------------------------------------------------------------- sensors
    def _sensor_loop(self):
        while not self._stop.wait(SENSOR_PERIOD_S):
            try:
                self.read_sensors_once()
            except Exception:  # noqa: BLE001
                log.exception("Szenzor olvasási hiba")

    def read_sensors_once(self) -> None:
        cfg = self.cfg_store.data
        with self._lock:
            if self.adc_error and time.monotonic() >= self._adc_retry_at:
                self._init_adc(cfg)
            adc = self.adc
        div = cfg["hardware"]["divider"]
        samples = int(cfg["hardware"]["adc"]["samples"])
        results = {}
        for sid in SENSOR_IDS:
            s = cfg["sensors"][sid]
            voltage, saturated, err = None, False, None
            try:
                values = []
                sat_count = 0
                for _ in range(samples):
                    v, sat = adc.read(int(s["channel"]))
                    values.append(v)
                    sat_count += 1 if sat else 0
                voltage = statistics.median(values)
                saturated = sat_count > samples // 2
            except ADCError as exc:
                err = str(exc)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            if err is not None:
                reading = self._empty_reading()
                reading["fault"] = "noadc" if adc.name == "nincs" else "adc"
                reading["fault_text"] = "%s: %s" % (thermistor.FAULT_TEXT[reading["fault"]], err)
                self._ema.pop(sid, None)
            else:
                reading = thermistor.evaluate(voltage, float(div["supply_v"]), float(div["series_ohm"]),
                                              float(s["r25"]), float(s["beta"]), float(s["offset"]),
                                              float(s["gain"]), saturated)
                reading["fault_text"] = thermistor.FAULT_TEXT.get(reading["fault"]) if reading["fault"] else None
                if reading["value"] is not None:
                    prev = self._ema.get(sid)
                    if prev is not None and abs(reading["value"] - prev) < 10:
                        reading["value"] = prev + EMA_ALPHA * (reading["value"] - prev)
                    self._ema[sid] = reading["value"]
                else:
                    self._ema.pop(sid, None)
            reading["ts"] = time.time()
            results[sid] = reading
        with self._temps_lock:
            self._temps = results

    def sensor_snapshot(self) -> dict:
        with self._temps_lock:
            return {k: dict(v) for k, v in self._temps.items()}

    def status(self) -> dict:
        mono = time.monotonic()
        with self._lock:
            return {
                "sim": self.use_sim,
                "gpio_backend": self.gpio_name,
                "gpio_ok": self.gpio_ok,
                "gpio_errors": list(self.gpio_errors),
                "pin_errors": {IO_LABELS[k]: v for k, v in self.pin_errors.items()},
                "output_mismatch": {
                    name: {"label": IO_LABELS[name], "gpio": self._out_pins[name][0],
                           "expected": self._outputs[name] == self._out_pins[name][1],
                           "actual": self._out_actual.get(name)}
                    for name, count in self._mismatch.items() if count >= MISMATCH_AFTER and name in self._out_pins},
                "output_flapping": {
                    name: {"label": IO_LABELS[name], "gpio": self._out_pins[name][0], "count": len(times)}
                    for name, times in self._mismatch_times.items()
                    if len(times) >= OUTPUT_FLAPS and self._mismatch.get(name, 0) < MISMATCH_AFTER
                    and name in self._out_pins},
                "input_flapping": {
                    name: {"label": IO_LABELS[name], "gpio": self._in_pins[name][0], "count": n}
                    for name, n in ((k, sum(1 for t in v if mono - t <= FLAP_WINDOW_S))
                                    for k, v in self._input_changes.items())
                    if n >= INPUT_FLAPS and name in self._in_pins},
                "pin_warnings": {IO_LABELS[k]: v for k, v in self.pin_warnings.items()},
                "owner_blocked": self.owner_blocked,
                "adc": self.adc.name if self.adc else "",
                "adc_ok": self.adc_error is None,
                "adc_error": self.adc_error,
            }
