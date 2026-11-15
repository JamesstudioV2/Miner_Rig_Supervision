"""Miner polling, command execution and desired-state reconciliation.

* Every enabled miner is polled in parallel every ``poll_interval_s`` seconds.
* Commands never block the caller (the safety controller): they run on a
  worker pool, the newest request for a miner always wins.
* Each miner has a *desired* state (mining / stopped).  When a miner does not
  follow it (e.g. it was offline while the stop command was sent, or it was
  rebooted with a firmware that forgets the sleep mode) the command is resent.
* Miner errors are only reported - they never stop the cooling system.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..util import fmt_hashrate
from .registry import DRIVERS, create_driver

log = logging.getLogger(__name__)

RESEND_S = 90.0             # minimum time between automatic command repeats
OFFLINE_AFTER = 2           # consecutive failed polls before a miner counts as offline
BUSY_AFTER_START_S = 240.0  # a (re)starting miner may not answer for a while
BUSY_AFTER_STOP_S = 90.0
FORCE_STOP_MIN_S = 30.0

ACTION_LABELS = {"start": "indítás", "stop": "leállítás (alvó mód)", "reboot": "újraindítás"}


class MinerRecord:
    def __init__(self, cfg: dict, driver, timeout: float):
        self.cfg = cfg
        self.driver = driver
        self.timeout = timeout
        self.data = None
        self.online = False
        self.ever_seen = False
        self.last_seen = 0.0
        self.last_error = ""
        self.fail_count = 0
        self.desired = None
        self.last_cmd = None
        self.last_cmd_mono = -1e9
        self.busy_until = 0.0
        self.polling = False
        self.cmd_running = False
        self.next_action = None
        self.known_errors = set()
        self.offline_reported = False
        # production monitoring (W406 / W407)
        self.prod_alarm = None
        self.producing_seen = False
        self.not_producing_since = None
        self.start_mono = None

    def mark_desired(self, desired, mono: float) -> None:
        """New desired state: restart the production monitoring for this miner."""
        self.desired = desired
        self.prod_alarm = None
        self.producing_seen = False
        self.not_producing_since = None
        self.start_mono = mono if desired is True else None

    @property
    def name(self) -> str:
        return self.cfg.get("name") or self.cfg.get("host")


class MinerManager:
    def __init__(self, config_store, events, signal, sim_world=None):
        self.cfg_store = config_store
        self.events = events
        self.signal = signal
        self.sim = sim_world
        self._lock = threading.RLock()
        self._records = collections.OrderedDict()
        self._poll_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="miner-poll")
        self._cmd_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="miner-cmd")
        self.system_desired = None
        self._no_control_warned = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._sync(config_store.data)
        config_store.add_listener(lambda old, new: self._sync(new))

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="miner-manager", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._poll_pool.shutdown(wait=False)
        self._cmd_pool.shutdown(wait=False)

    def set_sim_world(self, sim_world) -> None:
        """The hardware mode changed at runtime: rebind the simulated miners to the new world."""
        with self._lock:
            self.sim = sim_world
            for rec in self._records.values():
                if rec.cfg["type"] == "simulated":
                    rec.driver = create_driver(rec.cfg, rec.timeout, sim_world)
                    rec.data, rec.online, rec.fail_count = None, False, 0
        self.signal.bump()
        self._wake.set()

    def _sync(self, cfg: dict) -> None:
        timeout = float(cfg["miners"]["timeout_s"])
        with self._lock:
            records = collections.OrderedDict()
            for mcfg in cfg["miners"]["list"]:
                rec = self._records.get(mcfg["id"])
                if rec is not None and rec.cfg == mcfg and rec.timeout == timeout:
                    records[mcfg["id"]] = rec
                    continue
                driver = create_driver(mcfg, timeout, self.sim)
                if rec is None:
                    rec = MinerRecord(mcfg, driver, timeout)
                    rec.mark_desired(self.system_desired, time.monotonic())
                else:
                    if (rec.cfg["host"], rec.cfg["type"], rec.cfg["api_port"]) != \
                            (mcfg["host"], mcfg["type"], mcfg["api_port"]):
                        rec.data, rec.online, rec.ever_seen, rec.fail_count = None, False, False, 0
                        rec.known_errors = set()
                        rec.offline_reported = False
                    rec.cfg, rec.driver, rec.timeout = mcfg, driver, timeout
                records[mcfg["id"]] = rec
            self._records = records
        self.signal.bump()
        self._wake.set()

    # -------------------------------------------------------------- polling
    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_all()
            except Exception:  # noqa: BLE001
                log.exception("Miner lekérdezési hiba")
            interval = float(self.cfg_store.data["miners"]["poll_interval_s"])
            self._wake.wait(max(0.5, interval - (time.monotonic() - started)))
            self._wake.clear()

    def poll_all(self) -> None:
        with self._lock:
            todo = [r for r in self._records.values() if r.cfg["enabled"] and not r.polling]
            for r in todo:
                r.polling = True
        for r in todo:
            try:
                self._poll_pool.submit(self._poll_one, r)
            except RuntimeError:  # pool shut down
                r.polling = False

    def _poll_one(self, rec: MinerRecord) -> None:
        driver = rec.driver
        try:
            md, err = driver.poll(), None
        except Exception as exc:  # noqa: BLE001 - any driver failure means "no data"
            md, err = None, (str(exc) or exc.__class__.__name__)
        emit = []
        action = None
        with self._lock:
            rec.polling = False
            if rec.driver is not driver:
                return  # reconfigured meanwhile
            mono = time.monotonic()
            if md is not None:
                if rec.offline_reported:
                    emit.append(("info", "%s ismét elérhető" % rec.name, ""))
                    rec.offline_reported = False
                rec.data = md.to_dict()
                rec.online, rec.ever_seen = True, True
                rec.last_seen, rec.fail_count, rec.last_error = time.time(), 0, ""
                errors = set(md.errors)
                for e in sorted(errors - rec.known_errors):
                    emit.append(("warning", "%s: %s" % (rec.name, e), "W402"))
                if rec.known_errors and not errors:
                    emit.append(("info", "%s: a miner hibajelzései megszűntek" % rec.name, ""))
                rec.known_errors = errors
                action = self._reconcile_action(rec, mono)
            else:
                rec.fail_count += 1
                rec.last_error = err
                if rec.fail_count >= OFFLINE_AFTER:
                    rec.online = False
                    if not rec.offline_reported and mono >= rec.busy_until:
                        rec.offline_reported = True
                        emit.append(("warning", "%s nem elérhető: %s" % (rec.name, err), "W401"))
            emit.extend(self._check_production(rec, mono))
        for level, msg, code in emit:
            self.events.add(level, msg, code=code, source="miner")
        if action:
            self._submit(rec, action[0], action[1], "automatika")
        self.signal.bump()

    # -------------------------------------------------- production monitoring
    def _producing(self, rec: MinerRecord) -> bool:
        if not rec.online or not rec.data:
            return False
        hs = rec.data.get("hashrate") or 0
        if hs <= 0:
            return False
        pct = float(self.cfg_store.data["miners"]["production_min_pct"])
        ideal = rec.data.get("hashrate_ideal")
        return not (ideal and pct > 0 and hs < ideal * pct / 100.0)

    def _why_not_producing(self, rec: MinerRecord) -> str:
        if not rec.online or not rec.data:
            return "nem elérhető" + (" (%s)" % rec.last_error if rec.last_error else "")
        d = rec.data
        text = "hashrate %s" % fmt_hashrate(d.get("hashrate") or 0)
        if d.get("hashrate_ideal"):
            text += " (névleges %s)" % fmt_hashrate(d["hashrate_ideal"])
        if d.get("errors"):
            text += ", hibák: " + "; ".join(d["errors"][:3])
        return text

    def _check_production(self, rec: MinerRecord, mono: float) -> list:
        """Detect a miner that stopped producing (W406) or never started (W407).

        Only active while the miner is supposed to work (system running and the miner
        not stopped individually).  Must be called with the lock held.
        """
        cfg = self.cfg_store.data["miners"]
        if not rec.cfg["enabled"] or rec.desired is not True or self._blocked_in_sim(rec, "start"):
            rec.prod_alarm, rec.not_producing_since = None, None
            return []
        if rec.start_mono is None:
            rec.start_mono = mono
        if self._producing(rec):
            emit = []
            if rec.prod_alarm:
                emit.append(("info", "%s ismét termel: %s" % (rec.name, fmt_hashrate(rec.data.get("hashrate"))),
                             "I606"))
                rec.prod_alarm = None
            rec.producing_seen = True
            rec.not_producing_since = None
            return emit
        if rec.not_producing_since is None:
            rec.not_producing_since = mono
        if rec.prod_alarm:
            return []
        if rec.producing_seen:
            if mono - rec.not_producing_since < float(cfg["production_loss_delay_s"]):
                return []
            code = "W406"
            msg = "%s abbahagyta a termelést: %s" % (rec.name, self._why_not_producing(rec))
        else:
            if mono - rec.start_mono < float(cfg["production_start_timeout_s"]):
                return []
            code = "W407"
            msg = "%s az indítás után %d perccel sem termel: %s" % (
                rec.name, round((mono - rec.start_mono) / 60.0), self._why_not_producing(rec))
        rec.prod_alarm = {"code": code, "message": msg, "since": time.time()}
        return [("warning", msg, code)]

    def _reconcile_action(self, rec: MinerRecord, mono: float):
        if rec.desired is None or not rec.driver.can_control or rec.cmd_running:
            return None
        if mono - rec.last_cmd_mono < RESEND_S:
            return None
        d = rec.data or {}
        mode = d.get("mode")
        hashing = (d.get("hashrate") or 0) > 0
        if rec.desired is False and (mode == "normal" or (mode == "unknown" and hashing)):
            return ("stop", "a miner a leállítás után is dolgozik – parancs megismételve")
        if rec.desired is True and mode == "sleep":
            return ("start", "a miner alvó módban maradt – parancs megismételve")
        return None

    # ------------------------------------------------------------- commands
    def _blocked_in_sim(self, rec: MinerRecord, action: str) -> bool:
        """In simulation mode the real pumps do not run - a REAL miner must never be started."""
        return self.sim is not None and action in ("start", "reboot") and rec.cfg["type"] != "simulated"

    def _submit(self, rec: MinerRecord, action: str, reason: str = "", source: str = "") -> None:
        if self._blocked_in_sim(rec, action):
            return
        with self._lock:
            rec.next_action = (action, reason, source)
            rec.last_cmd_mono = time.monotonic()
            if rec.cmd_running:
                return
            rec.cmd_running = True
        try:
            self._cmd_pool.submit(self._worker, rec)
        except RuntimeError:
            rec.cmd_running = False

    def _worker(self, rec: MinerRecord) -> None:
        while True:
            with self._lock:
                item, rec.next_action = rec.next_action, None
                if item is None:
                    rec.cmd_running = False
                    break
                driver, name = rec.driver, rec.name
            action, reason, source = item
            started = time.time()
            try:
                msg, ok = (getattr(driver, action)() or "OK"), True
            except Exception as exc:  # noqa: BLE001
                msg, ok = (str(exc) or exc.__class__.__name__), False
            with self._lock:
                rec.last_cmd = {"action": action, "ts": started, "ok": ok, "message": msg, "reason": reason}
                rec.last_cmd_mono = time.monotonic()
                if ok:
                    rec.busy_until = time.monotonic() + (
                        BUSY_AFTER_START_S if action in ("start", "reboot") else BUSY_AFTER_STOP_S)
            label = ACTION_LABELS.get(action, action)
            if ok:
                self.events.add("info", "%s: %s – %s%s" % (name, label, msg, (" (%s)" % reason) if reason else ""),
                                source=source or "miner")
            else:
                self.events.add("warning", "%s: %s sikertelen – %s" % (name, label, msg), code="W403",
                                source=source or "miner")
            self.signal.bump()
        self._wake.set()  # refresh the data soon

    def command_all(self, action: str, reason: str = "", source: str = "") -> int:
        """Set the desired state of every enabled miner and send the command (non-blocking)."""
        assert action in ("start", "stop")
        with self._lock:
            self.system_desired = action == "start"
            recs = [r for r in self._records.values() if r.cfg["enabled"]]
            mono = time.monotonic()
            for r in recs:
                if r.desired != self.system_desired or self.system_desired:
                    r.mark_desired(self.system_desired, mono)
        sent = 0
        blocked = [r.name for r in recs if self._blocked_in_sim(r, action)]
        if blocked:
            self.events.add("warning", "Szimulációs módban a valódi minerek nem indulnak el (a valódi szivattyúk nem "
                                       "járnak): %s" % ", ".join(blocked), code="W403", source=source or "miner")
        for r in recs:
            if self._blocked_in_sim(r, action):
                continue
            if not r.driver.can_control:
                if r.cfg["id"] not in self._no_control_warned:
                    self._no_control_warned.add(r.cfg["id"])
                    self.events.add("warning", "%s: ez a típus nem vezérelhető, kézzel kell %s!"
                                    % (r.name, "elindítani" if action == "start" else "leállítani"),
                                    code="W404", source="miner")
                continue
            self._submit(r, action, reason, source)
            sent += 1
        return sent

    def command_one(self, miner_id: str, action: str, source: str = ""):
        with self._lock:
            rec = self._records.get(miner_id)
            if rec is None:
                return False, "Ismeretlen miner"
            if self._blocked_in_sim(rec, action):
                return False, "Szimulációs módban valódi miner nem indítható (a valódi szivattyúk nem járnak)"
            if action in ("start", "stop"):
                if not rec.driver.can_control:
                    return False, "Ez a miner típus nem vezérelhető"
                rec.mark_desired(action == "start", time.monotonic())
            elif action == "reboot":
                if not rec.driver.can_reboot:
                    return False, "Az újraindítás ennél a típusnál nem támogatott"
            else:
                return False, "Ismeretlen parancs"
        self._submit(rec, action, "kézi parancs", source)
        return True, "Parancs elküldve: %s – %s" % (rec.name, ACTION_LABELS[action])

    def force_stop_hashing(self, reason: str) -> int:
        """Overheat protection: immediately resend stop to miners that still work."""
        targets = []
        with self._lock:
            mono = time.monotonic()
            for r in self._records.values():
                if not r.cfg["enabled"] or not r.driver.can_control or not r.online or not r.data:
                    continue
                hashing = (r.data.get("hashrate") or 0) > 0 or r.data.get("mode") == "normal"
                if hashing and mono - r.last_cmd_mono >= FORCE_STOP_MIN_S:
                    r.mark_desired(False, mono)
                    targets.append(r)
        for r in targets:
            self._submit(r, "stop", reason, "védelem")
        return len(targets)

    # ---------------------------------------------------------------- views
    def hottest(self):
        """``(temperature, miner_name)`` of the hottest miner with fresh data."""
        max_age = max(30.0, 3.0 * float(self.cfg_store.data["miners"]["poll_interval_s"]))
        now = time.time()
        best = (None, None)
        with self._lock:
            for r in self._records.values():
                if not r.cfg["enabled"] or not r.online or not r.data or now - r.last_seen > max_age:
                    continue
                temps = [t for t in (r.data.get("temp_chip"), r.data.get("temp_board")) if t is not None]
                if temps and (best[0] is None or max(temps) > best[0]):
                    best = (max(temps), r.name)
        return best

    @staticmethod
    def _status(rec: MinerRecord, mono: float) -> str:
        if not rec.cfg["enabled"]:
            return "disabled"
        if not rec.online:
            if mono < rec.busy_until and rec.ever_seen:
                return "restarting"
            return "offline" if (rec.ever_seen or rec.fail_count >= OFFLINE_AFTER) else "connecting"
        d = rec.data or {}
        if (d.get("hashrate") or 0) > 0:
            return "mining"
        if d.get("mode") == "sleep":
            return "sleeping"
        if rec.desired is True or d.get("mode") == "normal":
            return "starting"
        return "idle"

    def snapshot(self) -> list:
        mono = time.monotonic()
        out = []
        with self._lock:
            for r in self._records.values():
                cls = DRIVERS.get(r.cfg["type"])
                d = r.data
                out.append({
                    "id": r.cfg["id"],
                    "name": r.name,
                    "host": r.cfg["host"],
                    "type": r.cfg["type"],
                    "type_label": cls.label if cls else r.cfg["type"],
                    "enabled": r.cfg["enabled"],
                    "status": self._status(r, mono),
                    "online": r.online,
                    "has_errors": bool(d and d.get("errors")) or bool(r.prod_alarm),
                    "prod_alarm": dict(r.prod_alarm) if r.prod_alarm else None,
                    "last_seen": r.last_seen or None,
                    "last_error": r.last_error,
                    "desired": r.desired,
                    "busy": mono < r.busy_until,
                    "last_cmd": dict(r.last_cmd) if r.last_cmd else None,
                    "can_control": r.driver.can_control,
                    "can_reboot": r.driver.can_reboot,
                    "data": d,
                })
        return out

    def summary(self, snapshot=None) -> dict:
        snap = snapshot if snapshot is not None else self.snapshot()
        enabled = [m for m in snap if m["enabled"]]
        online = [m for m in enabled if m["online"] and m["data"]]
        powers = [m["data"].get("power") for m in online if m["data"].get("power") is not None]
        hottest, hottest_name = None, None
        for m in online:
            temps = [t for t in (m["data"].get("temp_chip"), m["data"].get("temp_board")) if t is not None]
            if temps and (hottest is None or max(temps) > hottest):
                hottest, hottest_name = max(temps), m["name"]
        return {
            "total": len(enabled),
            "online": len(online),
            "mining": sum(1 for m in enabled if m["status"] == "mining"),
            "with_errors": sum(1 for m in enabled if m["has_errors"]),
            "hashrate": sum((m["data"].get("hashrate") or 0) for m in online),
            "hashrate_ideal": sum((m["data"].get("hashrate_ideal") or 0) for m in online) or None,
            "power": sum(powers) if powers else None,
            "max_temp": hottest,
            "max_temp_name": hottest_name,
        }
