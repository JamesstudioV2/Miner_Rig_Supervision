"""Safety controller - the heart of the system.

States
  stopped    leállítva (szivattyúk ki, kivéve kézi légtelenítés / utóhűtés)
  starting   indítás: szivattyúk be -> ``miner_start_delay_s`` múlva minerek indítása
  running    üzem: áramlás- és hőfokfigyelés
  stopping   leállítás: minerek leállítva, szivattyúk még ``post_stop_pump_s`` ideig járnak
  emergency  vészleállás: minerek leállítva, szivattyúk ki, hibakód nyugtázásig megmarad

Protections
  * no flow within ``flow_start_timeout_s`` after the pumps start   -> emergency (E101 / E103)
  * flow lost for ``flow_loss_delay_s`` while running                -> emergency (E102 / E104)
  * temperature outside the configured range (per sensor, optional)  -> emergency (E201..E204)
  * sensor fault (per sensor, optional)                              -> emergency (E211..E214)
  * after a stop: a miner (or the T2 water) above ``overheat_temp_c`` -> pumps on for a while,
    the stop command is resent to the miners that still work
  * miner errors are displayed only, they never stop the system
"""

from __future__ import annotations

import logging
import os
import threading
import time

from . import errors as E
from .config import IO_INPUTS, IO_LABELS, SENSOR_IDS
from .util import atomic_write_json, read_json

log = logging.getLogger(__name__)

STOPPED, STARTING, RUNNING, STOPPING, EMERGENCY = "stopped", "starting", "running", "stopping", "emergency"
STATE_LABELS = {
    STOPPED: "Leállítva",
    STARTING: "Indítás…",
    RUNNING: "Üzemel",
    STOPPING: "Leállítás…",
    EMERGENCY: "VÉSZLEÁLLÁS",
}
STATE_CODES = {STOPPED: 0, STARTING: 1, RUNNING: 2, STOPPING: 3, EMERGENCY: 4}
ACTIVE_STATES = (STARTING, RUNNING)
PUMP_LABELS = {"pump1": "P1 (belső kör)", "pump2": "P2 (külső kör)"}

TICK_S = 0.05
AUTO_RESUME_SOURCE = "automatikus visszaállás"


def _c(value) -> str:
    """Hungarian decimal format for user facing messages (21,5)."""
    return ("%.1f" % value).replace(".", ",")


def _g(value) -> str:
    return ("%g" % value).replace(".", ",")
STUCK_BUTTON_S = 30.0
FORCE_STOP_EVERY_S = 5.0


class Controller:
    def __init__(self, config_store, io, miners, events, signal, state_path: str):
        self.cfg_store = config_store
        self.io = io
        self.miners = miners
        self.events = events
        self.signal = signal
        self.state_path = state_path
        self._lock = threading.RLock()
        self.state = STOPPED
        self.state_since = time.time()
        self._state_mono = time.monotonic()
        self.alarm = None
        self.killed = False          # KILLSWITCH latch: pumps forced off until acknowledged
        self.kill_input = False
        self.pumps = {"pump1": False, "pump2": False}
        self.manual = {"pump1": None, "pump2": None}
        self.assist = {"active": False, "since": None, "since_mono": 0.0, "reason": ""}
        self.inputs = {name: False for name in IO_INPUTS}
        self._pumps_on_mono = 0.0
        self._flow_seen = {1: False, 2: False}
        self._flow_lost_since = {1: None, 2: None}
        self._miners_started = False
        self._start_source = ""
        self._limit_since = {sid: None for sid in SENSOR_IDS}
        self._fault_since = {sid: None for sid in SENSOR_IDS}
        self._button_since = {}
        self._warnings = {}
        self._last_force_stop = 0.0
        self._stop_evt = threading.Event()
        self._thread = None
        self._restored = self._load_state()

    # ------------------------------------------------------------ persistence
    def _load_state(self) -> dict:
        try:
            if os.path.exists(self.state_path):
                data = read_json(self.state_path)
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            log.error("Az állapotfájl nem olvasható: %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            atomic_write_json(self.state_path, {"state": self.state, "alarm": self.alarm, "ts": time.time()})
        except OSError as exc:
            log.error("Az állapotfájl nem írható: %s", exc)

    def initial_outputs(self) -> dict:
        """Pump states applied when the GPIO is initialised (before the first tick)."""
        prev = self._restored.get("state")
        on = bool(self.cfg_store.data["safety"]["auto_resume"] and prev in (STARTING, RUNNING))
        return {"pump1": on, "pump2": on}

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.restore()
        self._thread = threading.Thread(target=self._loop, name="controller", daemon=True)
        self._thread.start()

    def restore(self) -> None:
        """Restore the state saved before the last shutdown / power loss."""
        cfg = self.cfg_store.data
        prev = self._restored.get("state")
        with self._lock:
            if prev == EMERGENCY and self._restored.get("alarm"):
                self.state = EMERGENCY
                self.alarm = self._restored["alarm"]
                self.killed = self.alarm.get("code") in ("E001", "E002")
                self.events.add("warning", "Program indulás: a korábbi vészleállás (%s) továbbra is aktív – "
                                           "nyugtázás szükséges" % self.alarm.get("code"), source="rendszer")
                self.miners.command_all("stop", reason="vészleállás állapot visszaállítva", source="rendszer")
            elif prev in (STARTING, RUNNING) and cfg["safety"]["auto_resume"]:
                self.events.add("warning", "Program indulás üzem közben (pl. áramszünet / újraindulás) – "
                                           "a rendszer automatikusan visszaáll üzembe", source="rendszer")
                self._begin_start(AUTO_RESUME_SOURCE)
            else:
                if prev in (STARTING, RUNNING):
                    self.events.add("warning", "Program indulás: a rendszer üzemben volt, de az automatikus "
                                               "visszaállás ki van kapcsolva – leállított állapot", source="rendszer")
                self.state = STOPPED
                self.miners.command_all("stop", reason="program indulás leállított állapotban", source="rendszer")
            self._save_state()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        next_t = time.monotonic()
        errors_in_row = 0
        while not self._stop_evt.is_set():
            try:
                self.tick()
                errors_in_row = 0
            except Exception:  # noqa: BLE001 - the control loop must never die
                errors_in_row += 1
                if errors_in_row <= 3 or errors_in_row % 200 == 0:
                    log.exception("Vezérlési hiba (%d. egymás után)", errors_in_row)
            next_t += TICK_S
            delay = next_t - time.monotonic()
            if delay < 0:
                next_t = time.monotonic()
                delay = 0
            self._stop_evt.wait(delay)

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        mono = time.monotonic()
        inputs, edges = self.io.poll_inputs(mono)
        temps = self.io.sensor_snapshot()
        cfg = self.cfg_store.data
        sf = cfg["safety"]
        io_cfg = cfg["hardware"]["io"]
        bump = bool(edges)
        with self._lock:
            self.inputs = inputs
            # KILLSWITCH first - it overrides everything else in this tick
            self._check_killswitch(inputs, io_cfg)
            start_pressed = stop_pressed = False
            for name, value in edges:
                if not value:
                    continue
                if name == "btn_stop" and io_cfg["btn_stop"]["enabled"]:
                    stop_pressed = True
                elif name == "btn_start" and io_cfg["btn_start"]["enabled"]:
                    start_pressed = True
            if stop_pressed:
                self._request_stop("fizikai STOP gomb")
            elif start_pressed:
                self._request_start("fizikai START gomb")
            self._check_buttons(mono, inputs, io_cfg)

            self._run_sequences(mono, sf)
            self._supervise_flow(mono, sf, inputs, io_cfg)
            self._supervise_temps(mono, sf, temps, cfg)
            self._overheat_assist(mono, sf, temps)
            self._manual_watchdog(mono, sf)
            self._status_warnings(cfg)

            pumps = self._compute_pumps()
            if pumps != self.pumps:
                self.pumps = pumps
                bump = True
            self.io.set_outputs(pumps, mono)
        if bump:
            self.signal.bump()

    def _compute_pumps(self) -> dict:
        if self.killed:  # absolute emergency stop: no post-circulation, no overheat assist, no manual run
            return {"pump1": False, "pump2": False}
        if self.state in (STARTING, RUNNING, STOPPING):
            return {"pump1": True, "pump2": True}
        assist = self.assist["active"]
        return {p: bool(assist or self.manual[p] is not None) for p in ("pump1", "pump2")}

    def _set_state(self, new_state: str, message=None, level: str = "info", code: str = "", source: str = "rendszer"):
        self.state = new_state
        self.state_since = time.time()
        self._state_mono = time.monotonic()
        if message:
            self.events.add(level, message, code=code, source=source)
        self._save_state()
        self.signal.bump()

    # -------------------------------------------------------------- sequences
    def _begin_start(self, source: str) -> None:
        for p in ("pump1", "pump2"):
            self.manual[p] = None
        self._assist_off(None)
        self._pumps_on_mono = time.monotonic()
        self._flow_seen = {1: False, 2: False}
        self._flow_lost_since = {1: None, 2: None}
        self._miners_started = False
        self._start_source = source
        delay = self.cfg_store.data["safety"]["miner_start_delay_s"]
        code = "I605" if source == AUTO_RESUME_SOURCE else "I601"
        self._set_state(STARTING, "Rendszer indítása (%s): szivattyúk bekapcsolva, a minerek %s mp múlva indulnak"
                        % (source, _g(delay)), code=code, source=source)

    def _run_sequences(self, mono: float, sf: dict) -> None:
        if self.state == STARTING:
            if not self._miners_started and mono - self._state_mono >= sf["miner_start_delay_s"]:
                self._miners_started = True
                n = self.miners.command_all("start", reason="rendszer indítás", source=self._start_source)
                self._set_state(RUNNING, "Rendszer üzemel – indítási parancs elküldve %d minernek" % n)
        elif self.state == STOPPING:
            if mono - self._state_mono >= sf["post_stop_pump_s"]:
                self._set_state(STOPPED, "Rendszer leállítva – szivattyúk kikapcsolva")

    def _check_killswitch(self, inputs: dict, io_cfg: dict) -> None:
        ks = io_cfg.get("killswitch") or {}
        if not ks.get("enabled"):
            self.kill_input = False
            self._clear_warn("kill:input", log_event=False)
            return
        err = self.io.input_error("killswitch") if self.io.gpio_ok else None
        active = bool(inputs.get("killswitch")) or bool(err)
        if active and not self.kill_input:
            if err:
                self._kill("E002", "a KILLSWITCH bemenet (GPIO%d) nem olvasható: %s" % (ks["gpio"], err))
            else:
                self._kill("E001", "a KILLSWITCH bemenet (GPIO%d) aktív" % ks["gpio"])
        self.kill_input = active
        if active:
            self._warn("kill:input", "E001" if not err else "E002",
                       "KILLSWITCH aktív – a rendszer nem indítható, a szivattyúk tiltva", "error", log_event=False)
        else:
            self._clear_warn("kill:input", log_event=False)

    def _kill(self, code: str, message: str) -> None:
        """Absolute emergency stop - overrides every other state and setting."""
        self.killed = True
        for p in ("pump1", "pump2"):
            self.manual[p] = None
        self._assist_off(None)
        self.alarm = {"code": code, "title": E.describe(code), "message": message, "ts": time.time(),
                      "prev_state": self.state}
        self.pumps = {"pump1": False, "pump2": False}
        self.io.set_outputs(self.pumps, time.monotonic())  # immediately, before anything else runs
        self._set_state(EMERGENCY, "ABSZOLÚT VÉSZLEÁLLÁS %s – %s: minerek és szivattyúk azonnal leállítva"
                        % (code, message), level="error", code=code, source="KILLSWITCH")
        self.miners.command_all("stop", reason="KILLSWITCH (%s)" % code, source="KILLSWITCH")

    def _emergency(self, code: str, message: str) -> None:
        if self.state == EMERGENCY:
            return
        self.alarm = {"code": code, "title": E.describe(code), "message": message, "ts": time.time(),
                      "prev_state": self.state}
        for p in ("pump1", "pump2"):
            self.manual[p] = None
        self._set_state(EMERGENCY, "VÉSZLEÁLLÁS %s – %s" % (code, message), level="error", code=code,
                        source="védelem")
        self.miners.command_all("stop", reason="vészleállás (%s)" % code, source="védelem")

    # ------------------------------------------------------------ supervision
    def _supervise_flow(self, mono: float, sf: dict, inputs: dict, io_cfg: dict) -> None:
        if self.state not in ACTIVE_STATES:
            self._flow_lost_since = {1: None, 2: None}
            return
        for loop in (1, 2):
            name = "flow%d" % loop
            if not io_cfg[name]["enabled"]:
                continue
            circuit = "belső" if loop == 1 else "külső"
            if inputs.get(name):
                self._flow_seen[loop] = True
                self._flow_lost_since[loop] = None
                continue
            if not self._flow_seen[loop]:
                if mono - self._pumps_on_mono >= sf["flow_start_timeout_s"]:
                    self._emergency(E.flow_code(loop, True),
                                    "A P%d szivattyú bekapcsolása után %s mp-ig sem jelzett áramlást az F%d "
                                    "áramláskapcsoló (%s kör)" % (loop, _g(sf["flow_start_timeout_s"]), loop, circuit))
                    return
            else:
                if self._flow_lost_since[loop] is None:
                    self._flow_lost_since[loop] = mono
                elif mono - self._flow_lost_since[loop] >= sf["flow_loss_delay_s"]:
                    self._emergency(E.flow_code(loop, False),
                                    "Az F%d áramláskapcsoló %s mp óta nem jelez áramlást (%s kör)"
                                    % (loop, _g(sf["flow_loss_delay_s"]), circuit))
                    return

    @staticmethod
    def _limit_violation(s: dict, value):
        if not s["limit_enabled"] or value is None:
            return None
        if value > s["max"]:
            return "magas"
        if value < s["min"]:
            return "alacsony"
        return None

    def _supervise_temps(self, mono: float, sf: dict, temps: dict, cfg: dict) -> None:
        for sid in SENSOR_IDS:
            s = cfg["sensors"][sid]
            r = temps.get(sid) or {}
            value, fault = r.get("value"), r.get("fault")
            label = "%s (%s)" % (sid.upper(), s["name"])

            key = "fault:" + sid
            if fault:
                if self._fault_since[sid] is None:
                    self._fault_since[sid] = mono
                if mono - self._fault_since[sid] >= sf["sensor_fault_delay_s"]:
                    msg = "%s szenzorhiba: %s" % (label, r.get("fault_text") or fault)
                    if s["stop_on_fault"] and self.state in ACTIVE_STATES:
                        self._emergency(E.fault_code(sid, True), msg)
                    self._warn(key, E.fault_code(sid, False), msg, "error" if s["stop_on_fault"] else "warning")
            else:
                self._fault_since[sid] = None
                self._clear_warn(key)

            key = "limit:" + sid
            violation = self._limit_violation(s, value)
            if violation:
                if self._limit_since[sid] is None:
                    self._limit_since[sid] = mono
                if mono - self._limit_since[sid] >= sf["limit_trip_delay_s"]:
                    msg = "%s túl %s: %s °C (megengedett: %s … %s °C)" % (label, violation, _c(value), _g(s["min"]),
                                                                          _g(s["max"]))
                    if self.state in ACTIVE_STATES:
                        self._emergency(E.limit_code(sid, True), msg)
                    self._warn(key, E.limit_code(sid, False), msg + " – indítás tiltva", "error")
            else:
                self._limit_since[sid] = None
                self._clear_warn(key)

    def _overheat_assist(self, mono: float, sf: dict, temps: dict) -> None:
        allowed = not self.killed and sf["overheat_enabled"] and (
            self.state == STOPPED or (self.state == EMERGENCY and sf["overheat_after_emergency"]))
        if not allowed:
            if self.assist["active"]:
                self._assist_off(None)
            return
        threshold = sf["overheat_temp_c"]
        candidates = []
        hot_t, hot_name = self.miners.hottest()
        if hot_t is not None:
            candidates.append((hot_t, hot_name))
        if sf["overheat_use_water_temp"]:
            t2 = (temps.get("t2") or {}).get("value")
            if t2 is not None:
                candidates.append((t2, "T2 víz (minerek után)"))
        hottest = max(candidates) if candidates else None
        if not self.assist["active"]:
            if hottest and hottest[0] >= threshold:
                self.assist = {"active": True, "since": time.time(), "since_mono": mono,
                               "reason": "%s: %s °C" % (hottest[1], _c(hottest[0]))}
                self.events.add("warning", "Utóhűtés: %s hőmérséklete %s °C (≥ %s °C) leállított állapotban – "
                                           "szivattyúk bekapcsolva" % (hottest[1], _c(hottest[0]), _g(threshold)),
                                code="I501", source="védelem")
                self._last_force_stop = mono
                self.miners.force_stop_hashing("túlmelegedés leállított állapotban")
                self.signal.bump()
            return
        if hottest:
            self.assist["reason"] = "%s: %s °C" % (hottest[1], _c(hottest[0]))
        cool = hottest is None or hottest[0] < threshold - sf["overheat_hysteresis_c"]
        if cool and mono - self.assist["since_mono"] >= sf["overheat_min_run_s"]:
            self._assist_off("Utóhűtés vége (%s) – szivattyúk kikapcsolva" % (
                "legmagasabb: %s °C" % _c(hottest[0]) if hottest else "nincs hőmérséklet adat"))
        elif hottest and hottest[0] >= threshold and mono - self._last_force_stop >= FORCE_STOP_EVERY_S:
            self._last_force_stop = mono
            self.miners.force_stop_hashing("túlmelegedés leállított állapotban")

    def _assist_off(self, message) -> None:
        if not self.assist["active"]:
            return
        self.assist = {"active": False, "since": None, "since_mono": 0.0, "reason": ""}
        if message:
            self.events.add("info", message, source="védelem")
        self.signal.bump()

    def _manual_watchdog(self, mono: float, sf: dict) -> None:
        for p in ("pump1", "pump2"):
            m = self.manual[p]
            if m is not None and mono - m["last_mono"] > sf["manual_hold_timeout_s"]:
                self._manual_off(p, "a gomb elengedése nem érkezett meg (kapcsolat megszakadt?)")

    def _manual_off(self, pump: str, reason) -> None:
        m = self.manual[pump]
        if m is None:
            return
        self.manual[pump] = None
        self.events.add("info", "Kézi légtelenítés: %s KI (%.0f mp)%s" % (
            PUMP_LABELS[pump], time.monotonic() - m["since_mono"], (" – " + reason) if reason else ""),
            source=m["source"])
        self.signal.bump()

    def _check_buttons(self, mono: float, inputs: dict, io_cfg: dict) -> None:
        for name in ("btn_start", "btn_stop"):
            key = "stuck:" + name
            if inputs.get(name) and io_cfg[name]["enabled"]:
                since = self._button_since.setdefault(name, mono)
                if mono - since >= STUCK_BUTTON_S:
                    self._warn(key, "W304", "%s %d mp óta lenyomva érzékelhető – ellenőrizd a bekötést és az aktív "
                                            "szintet" % (IO_LABELS[name], STUCK_BUTTON_S), "warning")
            else:
                self._button_since.pop(name, None)
                self._clear_warn(key)

    def _status_warnings(self, cfg: dict) -> None:
        hw = self.io.status()
        if not hw["gpio_ok"]:
            parts = ["%s – %s" % (label, err) for label, err in hw["pin_errors"].items()
                     if err != "nincs használható GPIO könyvtár"] + hw["gpio_errors"]
            detail = "; ".join(dict.fromkeys(parts)) or "ismeretlen hiba"
            self._warn("hw:gpio", "E301", "GPIO hiba – a szivattyúk nem vezérelhetők: %s" % detail, "error")
        else:
            self._clear_warn("hw:gpio")
        if hw["adc_error"]:
            self._warn("hw:adc", "W301", hw["adc_error"], "warning")
        else:
            self._clear_warn("hw:adc")
        mismatch = hw.get("output_mismatch") or {}
        for name in ("pump1", "pump2"):
            key = "mismatch:" + name
            m = mismatch.get(name)
            if m:
                self._warn(key, "W305", "%s (GPIO%d): a program %s szintet ad ki, de a lábon %s mérhető – rövidzár, "
                                        "túlterhelt kimenet vagy hibás bekötés" % (
                                            m["label"], m["gpio"], "magas (3,3 V)" if m["expected"] else "alacsony (0 V)",
                                            "magas" if m["actual"] else "alacsony"), "warning")
            else:
                self._clear_warn(key)
        for loop in (1, 2):
            key = "flowdis:%d" % loop
            if not cfg["hardware"]["io"]["flow%d" % loop]["enabled"]:
                self._warn(key, "W303", "Az F%d áramláskapcsoló figyelése ki van kapcsolva – nincs védelem a "
                                        "%s kör áramlás kimaradása ellen" % (loop, "belső" if loop == 1 else "külső"),
                           "warning")
            else:
                self._clear_warn(key)
        for i, text in enumerate(self.cfg_store.load_warnings):
            self._warn("cfg:%d" % i, "W302", text, "warning")
        if hw["sim"]:
            self._warn("sim", "I503", "Szimulációs mód – a program NEM vezérli a valódi GPIO lábakat és nem olvassa "
                                      "a valódi szenzorokat", "info", log_event=False)
        else:
            self._clear_warn("sim", log_event=False)
        if self.assist["active"]:
            self._warn("assist", "I501", "Utóhűtés aktív – %s" % self.assist["reason"], "warning", log_event=False)
        else:
            self._clear_warn("assist", log_event=False)
        active_manual = [PUMP_LABELS[p] for p in ("pump1", "pump2") if self.manual[p] is not None]
        if active_manual:
            self._warn("manual", "I502", "Kézi szivattyú üzem: " + ", ".join(active_manual), "info", log_event=False)
        else:
            self._clear_warn("manual", log_event=False)

    def _warn(self, key: str, code: str, message: str, level: str = "warning", log_event: bool = True) -> None:
        w = self._warnings.get(key)
        if w is None:
            self._warnings[key] = {"key": key, "code": code, "level": level, "message": message,
                                   "since": time.time()}
            if log_event:
                self.events.add(level, message, code=code, source="felügyelet")
            self.signal.bump()
        else:
            w.update(code=code, level=level, message=message)

    def _clear_warn(self, key: str, log_event: bool = True) -> None:
        w = self._warnings.pop(key, None)
        if w is not None:
            if log_event:
                self.events.add("info", "Megszűnt: %s – %s" % (w["code"], E.describe(w["code"])), source="felügyelet")
            self.signal.bump()

    # -------------------------------------------------------------- requests
    def _start_inhibit(self):
        reasons = []
        if self.kill_input:
            reasons.append("a KILLSWITCH bemenet aktív")
        if not self.io.gpio_ok:
            reasons.append("a GPIO nem működik, a szivattyúk nem vezérelhetők")
        cfg = self.cfg_store.data
        temps = self.io.sensor_snapshot()
        for sid in SENSOR_IDS:
            s = cfg["sensors"][sid]
            r = temps.get(sid) or {}
            violation = self._limit_violation(s, r.get("value"))
            if violation:
                reasons.append("%s túl %s (%s °C, megengedett: %s … %s °C)" % (
                    sid.upper(), violation, _c(r["value"]), _g(s["min"]), _g(s["max"])))
            if s["stop_on_fault"] and r.get("fault"):
                reasons.append("%s szenzorhiba (%s)" % (sid.upper(), r.get("fault_text") or r.get("fault")))
        return "; ".join(reasons) or None

    def _request_start(self, source: str):
        if self.state in ACTIVE_STATES:
            return True, "A rendszer már üzemel"
        inhibit = self._start_inhibit()
        if inhibit:
            msg = "Indítás nem lehetséges: " + inhibit
            self.events.add("warning", msg, source=source)
            return False, msg
        if self.state == EMERGENCY and self.alarm:
            self.events.add("info", "Vészleállás nyugtázva újraindítással (%s)" % self.alarm["code"], source=source)
            self.alarm = None
        self.killed = False
        self._begin_start(source)
        return True, "Indítás folyamatban"

    def _request_stop(self, source: str):
        if self.state in ACTIVE_STATES:
            n = self.miners.command_all("stop", reason="rendszer leállítás", source=source)
            delay = self.cfg_store.data["safety"]["post_stop_pump_s"]
            self._set_state(STOPPING, "Rendszer leállítása (%s): leállítási parancs %d minernek, a szivattyúk %s mp "
                                      "utókeringetés után állnak le" % (source, n, _g(delay)), code="I602", source=source)
            return True, "Leállítás folyamatban"
        if self.state == STOPPING:
            return True, "A leállítás már folyamatban van"
        n = self.miners.command_all("stop", reason="ismételt leállítás", source=source)
        self.events.add("info", "A rendszer már le van állítva – leállítási parancs újraküldve %d minernek" % n,
                        source=source)
        return True, "A rendszer már le van állítva – a leállítási parancs újra elküldve a minereknek"

    def request_start(self, source: str = "web"):
        with self._lock:
            return self._request_start(source)

    def request_stop(self, source: str = "web"):
        with self._lock:
            return self._request_stop(source)

    def acknowledge(self, source: str = "web"):
        with self._lock:
            if self.state != EMERGENCY:
                return False, "Nincs nyugtázandó vészleállás"
            if self.kill_input:
                return False, "A KILLSWITCH bemenet még aktív – előbb oldd fel"
            code = self.alarm["code"] if self.alarm else ""
            self.alarm = None
            self.killed = False
            self._set_state(STOPPED, "Vészleállás nyugtázva (%s)" % code, code="I603", source=source)
            return True, "Vészleállás nyugtázva"

    def manual_pump(self, pump: str, action: str, source: str = "web"):
        if pump not in ("pump1", "pump2"):
            return False, "Ismeretlen szivattyú"
        with self._lock:
            if action in ("press", "hold"):
                if self.killed:
                    return False, "KILLSWITCH vészleállás – a szivattyúk tiltva, amíg nyugtázod"
                if self.state not in (STOPPED, EMERGENCY):
                    if self.manual[pump] is not None:
                        self._manual_off(pump, "a rendszer üzembe lépett")
                    return False, "A kézi szivattyú gomb csak leállított rendszernél használható"
                m = self.manual[pump]
                mono = time.monotonic()
                if m is None:
                    self.manual[pump] = {"since": time.time(), "since_mono": mono, "last_mono": mono,
                                         "source": source}
                    self.events.add("info", "Kézi légtelenítés: %s BE" % PUMP_LABELS[pump], code="I502",
                                    source=source)
                    self.signal.bump()
                else:
                    m["last_mono"] = mono
                return True, "OK"
            if action == "release":
                self._manual_off(pump, None)
                return True, "OK"
            return False, "Ismeretlen művelet"

    # -------------------------------------------------------------- snapshot
    def _flow_status(self, loop: int, io_cfg: dict, mono: float, sf: dict) -> str:
        name = "flow%d" % loop
        if not io_cfg[name]["enabled"]:
            return "disabled"
        if self.inputs.get(name):
            return "ok"
        if not self.pumps["pump%d" % loop]:
            return "idle"
        if self.state in ACTIVE_STATES and not self._flow_seen[loop] and \
                mono - self._pumps_on_mono < sf["flow_start_timeout_s"]:
            return "waiting"
        if self.state in ACTIVE_STATES:
            return "fault"
        return "noflow"

    def snapshot(self) -> dict:
        cfg = self.cfg_store.data
        sf = cfg["safety"]
        io_cfg = cfg["hardware"]["io"]
        temps = self.io.sensor_snapshot()
        mono = time.monotonic()
        with self._lock:
            phase = None
            if self.state == STARTING:
                phase = "A minerek %d mp múlva indulnak" % max(0, round(sf["miner_start_delay_s"] -
                                                                        (mono - self._state_mono)))
            elif self.state == STOPPING:
                phase = "Utókeringetés – a szivattyúk %d mp múlva állnak le" % max(
                    0, round(sf["post_stop_pump_s"] - (mono - self._state_mono)))
            inhibit = self._start_inhibit() if self.state not in ACTIVE_STATES else None
            t_out = {}
            for sid in SENSOR_IDS:
                s = cfg["sensors"][sid]
                r = temps.get(sid) or {}
                violation = self._limit_violation(s, r.get("value"))
                if r.get("fault"):
                    status = "fault"
                elif violation:
                    status = "high" if violation == "magas" else "low"
                else:
                    status = "ok" if s["limit_enabled"] else "nolimit"
                t_out[sid] = {
                    "name": s["name"], "short": s["short"], "value": r.get("value"),
                    "raw_value": r.get("raw_value"), "voltage": r.get("voltage"), "ohm": r.get("ohm"),
                    "fault": r.get("fault"), "fault_text": r.get("fault_text"), "status": status,
                    "limit": {"enabled": s["limit_enabled"], "min": s["min"], "max": s["max"]},
                    "channel": s["channel"],
                }
            vals = {sid: t_out[sid]["value"] for sid in SENSOR_IDS}
            derived = {
                "dt_miners": (vals["t2"] - vals["t1"]) if None not in (vals["t1"], vals["t2"]) else None,
                "dt_radiator": (vals["t3"] - vals["t4"]) if None not in (vals["t3"], vals["t4"]) else None,
            }
            warnings = sorted(self._warnings.values(),
                              key=lambda w: ({"error": 0, "warning": 1, "info": 2}[w["level"]], w["since"]))
            return {
                "ts": time.time(),
                "site_name": cfg["site_name"],
                "system": {
                    "state": self.state,
                    "label": STATE_LABELS[self.state],
                    "since": self.state_since,
                    "alarm": dict(self.alarm) if self.alarm else None,
                    "phase": phase,
                    "start_inhibit": inhibit,
                    "start_allowed": self.state not in ACTIVE_STATES and inhibit is None,
                    "stop_allowed": True,
                    "manual_allowed": self.state in (STOPPED, EMERGENCY) and not self.killed,
                    "killswitch": {"enabled": bool(io_cfg["killswitch"]["enabled"]), "active": self.kill_input,
                                   "latched": self.killed},
                    "start_delay": sf["miner_start_delay_s"],
                    "post_stop": sf["post_stop_pump_s"],
                },
                "pumps": {p: {"on": self.pumps[p], "manual": self.manual[p] is not None,
                              "assist": self.assist["active"]} for p in ("pump1", "pump2")},
                "flows": {"flow%d" % i: {"flow": bool(self.inputs.get("flow%d" % i)),
                                         "enabled": io_cfg["flow%d" % i]["enabled"],
                                         "status": self._flow_status(i, io_cfg, mono, sf)} for i in (1, 2)},
                "buttons": {"btn_start": bool(self.inputs.get("btn_start")),
                            "btn_stop": bool(self.inputs.get("btn_stop"))},
                "assist": {"active": self.assist["active"], "since": self.assist["since"],
                           "reason": self.assist["reason"], "threshold": sf["overheat_temp_c"]},
                "temps": t_out,
                "derived": derived,
                "warnings": [dict(w) for w in warnings],
                "hardware": self.io.status(),
            }

    def state_code(self) -> int:
        return STATE_CODES[self.state]
