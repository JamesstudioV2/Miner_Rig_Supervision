"""Parsers for the CGMiner / BMMiner API and the Antminer web CGI replies."""

from __future__ import annotations

import math
import re

from .base import MinerData

_UNIT_MULT = {"H": 1.0, "KH": 1e3, "MH": 1e6, "GH": 1e9, "TH": 1e12, "PH": 1e15}
_SPLIT_RE = re.compile(r"[\s,;]+|(?<=\d)-(?=\d)")


def unit_multiplier(unit, default: float = 1e9) -> float:
    if not unit:
        return default
    u = str(unit).strip().upper()
    if u.endswith("/S"):
        u = u[:-2]
    return _UNIT_MULT.get(u, default)


def to_float(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    else:
        try:
            f = float(str(value).strip().replace(",", ""))
        except ValueError:
            return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def to_int(value):
    f = to_float(value)
    return int(f) if f is not None else None


def temp_list(value) -> list:
    """Parse temperatures given as list, number or ``"63-65-78-78"`` string (zeros dropped)."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else _SPLIT_RE.split(str(value).strip())
    out = []
    for item in items:
        f = to_float(item)
        if f is not None and 0 < f < 200:
            out.append(f)
    return out


def _first_dict(value):
    if isinstance(value, list):
        return value[0] if value and isinstance(value[0], dict) else None
    return value if isinstance(value, dict) else None


def parse_bmminer_stats(resp: dict, md: MinerData) -> bool:
    """``stats`` command of BMMiner/CGMiner based firmwares (stock Antminer, VNish, LuxOS)."""
    stats = resp.get("STATS")
    if not isinstance(stats, list):
        return False
    info, data = {}, None
    for item in stats:
        if not isinstance(item, dict):
            continue
        if data is None and any(k in item for k in ("GHS 5s", "GHS av", "chain_acn1", "chain_rate1",
                                                    "temp_chip1", "fan_num", "chain_acn6", "temp2_6")):
            data = item
        elif not info and any(k in item for k in ("Type", "BMMiner", "CGMiner", "Miner")):
            info = item
    if data is None:
        return False
    model = info.get("Type") or data.get("Type")
    if model:
        md.model = str(model)
    for key in ("BMMiner", "CGMiner", "LUXminer", "Miner"):
        if info.get(key):
            md.firmware = "%s %s" % (key, info[key])
            break
    mult = unit_multiplier(data.get("rate_unit"))
    hs = to_float(data.get("GHS 5s"))
    if hs is not None:
        md.hashrate = hs * mult
    av = to_float(data.get("GHS av"))
    if av is not None:
        md.hashrate_avg = av * mult
    ideal = to_float(data.get("total_rateideal"))
    if ideal:
        md.hashrate_ideal = ideal * mult
    elapsed = to_int(data.get("Elapsed"))
    if elapsed is not None:
        md.uptime = elapsed
    md.expected_boards = to_int(data.get("miner_count")) or None

    boards = []
    for i in range(1, 17):
        acn = to_float(data.get("chain_acn%d" % i))
        rate = to_float(data.get("chain_rate%d" % i))
        if not acn and not rate:
            continue
        chips = temp_list(data.get("temp_chip%d" % i)) or temp_list(data.get("temp2_%d" % i))
        pcbs = temp_list(data.get("temp_pcb%d" % i)) or temp_list(data.get("temp%d" % i))
        acs = str(data.get("chain_acs%d" % i) or "")
        boards.append({
            "idx": len(boards) + 1,
            "chain": i,
            "hashrate": rate * mult if rate is not None else None,
            "chip": max(chips) if chips else None,
            "board": max(pcbs) if pcbs else None,
            "asics": int(acn) if acn else None,
            "bad_chips": acs.count("x"),
        })
    if boards:
        md.boards = boards

    fan_num = to_int(data.get("fan_num")) or 0
    fans = [to_int(data.get("fan%d" % i)) or 0 for i in range(1, 9) if ("fan%d" % i) in data]
    nonzero = [f for f in fans if f > 0]
    if fan_num:
        md.fans = nonzero[:fan_num] + [0] * max(0, fan_num - len(nonzero))
    else:
        md.fans = nonzero
    return True


def parse_web_stats(resp: dict, md: MinerData) -> bool:
    """``/cgi-bin/stats.cgi`` of the modern Antminer web interface."""
    st = _first_dict(resp.get("STATS"))
    if st is None:
        return False
    info = resp.get("INFO") if isinstance(resp.get("INFO"), dict) else {}
    if info.get("type"):
        md.model = str(info["type"])
    mult = unit_multiplier(st.get("rate_unit"))
    for key, attr in (("rate_5s", "hashrate"), ("rate_avg", "hashrate_avg"), ("rate_ideal", "hashrate_ideal")):
        v = to_float(st.get(key))
        if v is not None:
            setattr(md, attr, v * mult)
    if md.hashrate_avg is None and to_float(st.get("rate_30m")) is not None:
        md.hashrate_avg = to_float(st.get("rate_30m")) * mult
    elapsed = to_int(st.get("elapsed"))
    if elapsed is not None:
        md.uptime = elapsed
    if isinstance(st.get("fan"), list):
        md.fans = [to_int(f) or 0 for f in st["fan"]]
    mode = to_int(st.get("miner-mode"))
    if mode is not None:
        md.mode = "sleep" if mode == 1 else "normal"
    boards = []
    for ch in st.get("chain") or []:
        if not isinstance(ch, dict):
            continue
        chips = temp_list(ch.get("temp_chip")) or temp_list(ch.get("temp_pic"))
        pcbs = temp_list(ch.get("temp_pcb"))
        rate = to_float(ch.get("rate_real"))
        boards.append({
            "idx": len(boards) + 1,
            "chain": to_int(ch.get("index")),
            "hashrate": rate * mult if rate is not None else None,
            "chip": max(chips) if chips else None,
            "board": max(pcbs) if pcbs else None,
            "asics": to_int(ch.get("asic_num")),
            "bad_chips": str(ch.get("asic") or "").count("x"),
        })
    if boards:
        md.boards = boards
    return True


def parse_summary(resp: dict, md: MinerData) -> bool:
    s = _first_dict(resp.get("SUMMARY"))
    if s is None:
        return False
    if md.hashrate is None:
        for key, mult in (("GHS 5s", 1e9), ("MHS 5s", 1e6), ("THS 5s", 1e12), ("KHS 5s", 1e3),
                          ("MHS 1m", 1e6), ("rate_5s", unit_multiplier(s.get("rate_unit")))):
            v = to_float(s.get(key))
            if v is not None:
                md.hashrate = v * mult
                break
    if md.hashrate_avg is None:
        for key, mult in (("GHS av", 1e9), ("MHS av", 1e6), ("THS av", 1e12),
                          ("rate_avg", unit_multiplier(s.get("rate_unit")))):
            v = to_float(s.get(key))
            if v is not None:
                md.hashrate_avg = v * mult
                break
    if md.hashrate_ideal is None and to_float(s.get("rate_ideal")):
        md.hashrate_ideal = to_float(s.get("rate_ideal")) * unit_multiplier(s.get("rate_unit"))
    if md.uptime is None:
        md.uptime = to_int(s.get("Elapsed", s.get("elapsed")))
    md.accepted = to_int(s.get("Accepted"))
    md.rejected = to_int(s.get("Rejected"))
    md.hw_errors = to_int(s.get("Hardware Errors", s.get("hw_all")))
    return True


def parse_pools(resp: dict, md: MinerData) -> bool:
    pools = [p for p in (resp.get("POOLS") or []) if isinstance(p, dict)]
    if not pools:
        return False
    active = next((p for p in pools if p.get("Stratum Active") is True), None)
    if active is None:
        alive = [p for p in pools if p.get("Status") == "Alive" and p.get("URL")]
        alive.sort(key=lambda p: to_int(p.get("Priority")) or 0)
        active = alive[0] if alive else next((p for p in pools if p.get("URL")), pools[0])
    md.pool = {
        "url": active.get("URL") or "",
        "user": active.get("User") or "",
        "status": active.get("Status") or "",
        "accepted": to_int(active.get("Accepted")),
        "rejected": to_int(active.get("Rejected")),
    }
    configured = [p for p in pools if p.get("URL")]
    if configured and all(p.get("Status") != "Alive" for p in configured):
        md.errors.append("Egyik pool sem elérhető")
    return True


_STATUS_LABELS = {"rate": "Hashrate", "network": "Hálózat", "fans": "Ventilátor", "temp": "Hőmérséklet"}


def parse_web_summary_errors(resp: dict) -> list:
    """Error list of the stock Antminer ``summary.cgi`` (status != "s")."""
    s = _first_dict(resp.get("SUMMARY"))
    errors = []
    if s is None:
        return errors
    for item in s.get("status") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status", "s")).lower() == "s":
            continue
        label = _STATUS_LABELS.get(item.get("type"), item.get("type") or "Hiba")
        msg = str(item.get("msg") or "").strip() or "hibás állapot"
        code = item.get("code")
        errors.append("%s: %s%s" % (label, msg, (" (kód %s)" % code) if code not in (None, "", 0) else ""))
    return errors


def mode_from_conf(conf: dict) -> str:
    wm = conf.get("bitmain-work-mode") if isinstance(conf, dict) else None
    if wm is None or str(wm).strip() == "":
        return "unknown"
    try:
        return "sleep" if int(str(wm).strip()) == 1 else "normal"
    except ValueError:
        return "unknown"
