"""Bitmain Antminer with stock firmware (S19 / S21 / T21 / Hydro / ... series).

Reading : CGMiner compatible API on TCP 4028 (stats, summary, pools) with the
          web CGI (``stats.cgi``) as fallback; errors from ``summary.cgi``.
Control : the web interface (HTTP digest auth, default root/root):
          ``get_miner_conf.cgi`` + ``set_miner_conf.cgi`` with ``"miner-mode"``
          0 = normal, 1 = sleep (the hashboards are powered down),
          ``reboot.cgi`` for a reboot.
"""

from __future__ import annotations

import time

from .base import MinerData, MinerDriver, add_derived_errors
from .parsing import (mode_from_conf, parse_bmminer_stats, parse_pools, parse_summary, parse_web_stats,
                      parse_web_summary_errors)
from .transport import MinerAuthError, MinerConnectionError, MinerError, http_json, http_request

MODE_REFRESH_S = 30.0


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def build_conf_payload(conf: dict, miner_mode: int, as_string: bool = False) -> dict:
    pools = []
    for p in (conf.get("pools") or [])[:3]:
        if isinstance(p, dict):
            pools.append({"url": p.get("url", ""), "user": p.get("user", ""), "pass": p.get("pass", "")})
    while len(pools) < 3:
        pools.append({"url": "", "user": "", "pass": ""})
    return {
        "bitmain-fan-ctrl": _as_bool(conf.get("bitmain-fan-ctrl", False)),
        "bitmain-fan-pwm": str(conf.get("bitmain-fan-pwm", "100")),
        "freq-level": str(conf.get("bitmain-freq-level", "100")),
        "miner-mode": str(miner_mode) if as_string else int(miner_mode),
        "pools": pools,
    }


class AntminerStock(MinerDriver):
    type_id = "antminer"
    label = "Bitmain Antminer – gyári firmware (S19/S21/T21/Hydro…)"
    note = ("Leállítás = alvó mód a webes felületen keresztül (set_miner_conf). "
            "Alapértelmezett belépés: root / root.")
    default_username = "root"
    default_password = "root"
    verify_delay_s = 1.5

    def __init__(self, cfg, timeout=4.0, sim_world=None):
        super().__init__(cfg, timeout, sim_world)
        self._mode = "unknown"
        self._mode_ts = -1e9

    def _url(self, cgi: str) -> str:
        return "%s/cgi-bin/%s.cgi" % (self.web_base(), cgi)

    def web_get(self, cgi: str, timeout=None):
        return http_json(self._url(cgi), username=self.username, password=self.password,
                         timeout=timeout or self.timeout)

    def web_post(self, cgi: str, payload: dict, timeout=None):
        return http_json(self._url(cgi), method="POST", data=payload, username=self.username,
                         password=self.password, timeout=timeout or max(self.timeout, 10.0), allow_empty=True)

    def poll(self) -> MinerData:
        md = MinerData()
        rpc_error = None
        try:
            if not parse_bmminer_stats(self.rpc("stats"), md):
                raise MinerError("ismeretlen stats formátum")
            try:
                parse_summary(self.rpc("summary"), md)
            except MinerError:
                pass
            try:
                parse_pools(self.rpc("pools"), md)
            except MinerError:
                pass
        except MinerError as exc:
            rpc_error = exc

        web_ok = False
        try:
            if rpc_error is not None:
                parse_web_stats(self.web_get("stats"), md)
            summary = self.web_get("summary")
            if rpc_error is not None:
                parse_summary(summary, md)
            md.errors.extend(parse_web_summary_errors(summary))
            web_ok = True
            if time.monotonic() - self._mode_ts > MODE_REFRESH_S:
                self._mode = mode_from_conf(self.web_get("get_miner_conf"))
                self._mode_ts = time.monotonic()
        except MinerAuthError:
            md.errors.append("Webes bejelentkezés sikertelen – ellenőrizd a felhasználónevet/jelszót")
        except MinerError:
            pass
        if rpc_error is not None and not web_ok:
            raise rpc_error
        if self._mode != "unknown":
            md.mode = self._mode
        add_derived_errors(md)
        return md.finalize()

    def _set_mode(self, miner_mode: int) -> str:
        want = "sleep" if miner_mode == 1 else "normal"
        want_label = "alvó" if miner_mode == 1 else "normál"
        conf = self.web_get("get_miner_conf")
        if not isinstance(conf, dict):
            raise MinerError("érvénytelen konfiguráció a minertől")
        if mode_from_conf(conf) == want:
            self._mode, self._mode_ts = want, time.monotonic()
            return "már %s üzemmódban volt" % want_label
        if "bitmain-work-mode" not in conf:
            raise MinerError("ez a firmware nem támogatja az alvó módot (bitmain-work-mode hiányzik)")
        self.web_post("set_miner_conf", build_conf_payload(conf, miner_mode))
        for attempt in range(2):
            time.sleep(self.verify_delay_s)
            try:
                check = mode_from_conf(self.web_get("get_miner_conf"))
            except MinerError:
                check = "unknown"
            if check == want:
                self._mode, self._mode_ts = want, time.monotonic()
                return "%s üzemmód beállítva" % want_label
            if attempt == 0:
                # some firmware versions only accept the mode as a string
                self.web_post("set_miner_conf", build_conf_payload(conf, miner_mode, as_string=True))
        raise MinerError("a miner nem erősítette meg az üzemmód váltást")

    def stop(self) -> str:
        return self._set_mode(1)

    def start(self) -> str:
        return self._set_mode(0)

    def reboot(self) -> str:
        try:
            http_request(self._url("reboot"), username=self.username, password=self.password,
                         timeout=max(self.timeout, 8.0))
        except MinerConnectionError:
            pass  # the miner often drops the connection while rebooting
        self._mode_ts = -1e9
        return "újraindítás elindítva"
