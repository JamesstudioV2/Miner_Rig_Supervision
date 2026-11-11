"""Drivers for alternative firmwares and a read-only generic driver.

* Braiins OS (BOSminer): CGMiner API ``pause`` / ``resume``
* VNish: REST API ``/api/v1/unlock`` -> token, ``mining/stop`` / ``mining/start``
* LuxOS: CGMiner API ``logon`` -> session, ``curtail`` sleep / wakeup
* Generic CGMiner: reading only, no control
"""

from __future__ import annotations

import json

from .base import MinerData, MinerDriver, add_derived_errors
from .parsing import parse_bmminer_stats, parse_pools, parse_summary, to_float, to_int
from .transport import MinerAuthError, MinerError, http_request


def _bmminer_poll(driver: MinerDriver) -> MinerData:
    """Read a BMMiner/CGMiner compatible API (stats + summary + pools)."""
    md = MinerData()
    stats_ok = False
    try:
        stats_ok = parse_bmminer_stats(driver.rpc("stats"), md)
    except MinerError as exc:
        stats_error = exc
    else:
        stats_error = None
    try:
        summary_ok = parse_summary(driver.rpc("summary"), md)
    except MinerError:
        if stats_error is not None:
            raise stats_error
        summary_ok = False
    if not stats_ok and not summary_ok:
        raise MinerError("ismeretlen API válasz")
    try:
        parse_pools(driver.rpc("pools"), md)
    except MinerError:
        pass
    return md


class GenericCGMiner(MinerDriver):
    type_id = "cgminer"
    label = "Egyéb CGMiner-kompatibilis (csak olvasás)"
    note = "Csak adatlekérdezés (4028-as API). Leállítani/indítani nem tudja a program!"
    can_control = False
    can_reboot = False

    def poll(self):
        md = _bmminer_poll(self)
        if (md.hashrate or 0) > 0:
            md.mode = "normal"
        add_derived_errors(md)
        return md.finalize()


class LuxOS(MinerDriver):
    type_id = "luxos"
    label = "LuxOS firmware"
    note = "Leállítás = curtail sleep, indítás = curtail wakeup (4028-as API)."
    default_password = ""

    def poll(self):
        md = _bmminer_poll(self)
        try:
            power = self.rpc("power")
            item = (power.get("POWER") or [{}])[0]
            md.power = to_float(item.get("Watts"))
        except (MinerError, LookupError, AttributeError):
            pass
        md.mode = "normal" if (md.hashrate or 0) > 0 else "unknown"
        add_derived_errors(md)
        return md.finalize()

    def _session(self) -> str:
        try:
            sid = self.rpc("session")["SESSION"][0]["SessionID"]
            if sid:
                return sid
        except (MinerError, LookupError, TypeError):
            pass
        try:
            return self.rpc("logon")["SESSION"][0]["SessionID"]
        except (LookupError, TypeError):
            raise MinerError("LuxOS bejelentkezés (logon) sikertelen")

    def stop(self):
        self.rpc("curtail", "%s,sleep" % self._session())
        return "alvó mód (curtail sleep)"

    def start(self):
        self.rpc("curtail", "%s,wakeup" % self._session())
        return "ébresztés (curtail wakeup)"

    def reboot(self):
        self.rpc("rebootdevice", self._session())
        return "újraindítás elindítva"


class BraiinsOS(MinerDriver):
    type_id = "braiins"
    label = "Braiins OS / OS+ (BOSminer)"
    note = ("Leállítás/indítás a 4028-as API pause/resume parancsával. "
            "Figyelem: a Braiins a szüneteltetést újraindítás után nem jegyzi meg!")
    default_password = ""
    can_reboot = False

    def __init__(self, cfg, timeout=4.0, sim_world=None):
        super().__init__(cfg, timeout, sim_world)
        self._paused = None

    def poll(self):
        md = MinerData()
        if not parse_summary(self.rpc("summary"), md):
            raise MinerError("ismeretlen API válasz")
        boards = {}
        try:
            for t in self.rpc("temps").get("TEMPS") or []:
                bid = to_int(t.get("ID"))
                boards.setdefault(bid, {})
                boards[bid]["chip"] = to_float(t.get("Chip"))
                boards[bid]["board"] = to_float(t.get("Board"))
        except (MinerError, AttributeError):
            pass
        try:
            for d in self.rpc("devs").get("DEVS") or []:
                bid = to_int(d.get("ID"))
                boards.setdefault(bid, {})
                v = to_float(d.get("MHS 1m", d.get("MHS 5s")))
                boards[bid]["hashrate"] = v * 1e6 if v is not None else None
        except (MinerError, AttributeError):
            pass
        md.boards = [{"idx": i + 1, "chain": bid, "hashrate": b.get("hashrate"), "chip": b.get("chip"),
                      "board": b.get("board"), "asics": None, "bad_chips": 0}
                     for i, (bid, b) in enumerate(sorted(boards.items(), key=lambda kv: kv[0] or 0))]
        try:
            md.fans = [to_int(f.get("RPM")) or 0 for f in self.rpc("fans").get("FANS") or []]
        except (MinerError, AttributeError):
            pass
        try:
            tuner = (self.rpc("tunerstatus").get("TUNERSTATUS") or [{}])[0]
            md.power = to_float(tuner.get("ApproximateMinerPowerConsumption"))
            for ch in tuner.get("TunerChainStatus") or []:
                status = str(ch.get("Status") or "")
                if status and status.lower() not in ("stable", "testing performance profile", "tuning individual chips"):
                    md.errors.append("Lánc %s: %s" % (ch.get("HashchainIndex"), status))
        except (MinerError, LookupError, AttributeError):
            pass
        try:
            parse_pools(self.rpc("pools"), md)
        except MinerError:
            pass
        try:
            ver = (self.rpc("version").get("VERSION") or [{}])[0]
            for key in ("BOSminer", "BOSer", "BOSminer+"):
                if ver.get(key):
                    md.firmware = "%s %s" % (key, ver[key])
                    break
        except (MinerError, LookupError, AttributeError):
            pass
        try:
            dd = (self.rpc("devdetails").get("DEVDETAILS") or [{}])[0]
            md.model = str(dd.get("Model") or "")
        except (MinerError, LookupError, AttributeError):
            pass
        hashing = (md.hashrate or 0) > 0
        if self._paused and not hashing:
            md.mode = "sleep"
        elif hashing:
            md.mode = "normal"
        add_derived_errors(md)
        return md.finalize()

    def stop(self):
        self.rpc("pause")
        self._paused = True
        return "szüneteltetve (pause)"

    def start(self):
        self.rpc("resume")
        self._paused = False
        return "folytatva (resume)"


class VNish(MinerDriver):
    type_id = "vnish"
    label = "VNish firmware"
    note = "Leállítás/indítás a VNish REST API-n (mining/stop, mining/start). Alapértelmezett jelszó: admin."
    default_username = "admin"
    default_password = "admin"

    def __init__(self, cfg, timeout=4.0, sim_world=None):
        super().__init__(cfg, timeout, sim_world)
        self._token = None

    def _login(self) -> str:
        status, body = http_request(self.web_base() + "/api/v1/unlock", "POST", {"pw": self.password},
                                    timeout=self.timeout)
        if status != 200:
            raise MinerAuthError("VNish bejelentkezés sikertelen (HTTP %d)" % status)
        try:
            self._token = json.loads(body.decode("utf-8", "replace"))["token"]
        except (ValueError, KeyError, TypeError):
            raise MinerAuthError("VNish bejelentkezés sikertelen (nincs token)")
        return self._token

    def _api(self, path: str, method: str = "GET", bearer: bool = False):
        for attempt in (0, 1):
            token = self._token if (self._token and attempt == 0) else self._login()
            headers = {"Authorization": ("Bearer " + token) if bearer else token}
            status, body = http_request(self.web_base() + "/api/v1/" + path, method,
                                        {} if method == "POST" else None, headers=headers,
                                        timeout=max(self.timeout, 8.0))
            if status == 401 and attempt == 0:
                self._token = None
                continue
            if status != 200:
                raise MinerError("VNish API hiba: HTTP %d (%s)" % (status, path))
            text = body.decode("utf-8", "replace").strip()
            try:
                return json.loads(text) if text else {}
            except ValueError:
                return {}
        raise MinerAuthError("VNish bejelentkezés sikertelen")

    def poll(self):
        md = _bmminer_poll(self)
        try:
            summary = self._api("summary")
            miner = summary.get("miner") or {}
            md.power = to_float(miner.get("power_usage"))
            state = str((miner.get("miner_status") or {}).get("miner_state") or "")
            if state:
                md.mode = "sleep" if state in ("stopped", "shutting-down") else "normal"
        except MinerAuthError:
            md.errors.append("VNish bejelentkezés sikertelen – ellenőrizd a jelszót")
        except (MinerError, AttributeError):
            pass
        if md.mode == "unknown" and (md.hashrate or 0) > 0:
            md.mode = "normal"
        add_derived_errors(md)
        return md.finalize()

    def stop(self):
        self._api("mining/stop", "POST")
        return "bányászat leállítva"

    def start(self):
        try:
            self._api("mining/start", "POST")
        except MinerAuthError:
            raise
        except MinerError:
            self._api("mining/resume", "POST")
        return "bányászat elindítva"

    def reboot(self):
        self._api("system/reboot", "POST", bearer=True)
        return "újraindítás elindítva"
