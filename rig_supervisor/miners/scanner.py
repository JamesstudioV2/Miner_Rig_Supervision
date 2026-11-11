"""LAN scanner: finds miners by probing the CGMiner API port (4028)."""

from __future__ import annotations

import ipaddress
import json
import socket
from concurrent.futures import ThreadPoolExecutor

from .transport import MinerError, cgminer_command

MAX_HOSTS = 1024


def local_ipv4() -> str:
    """Best guess of the Pi's own LAN address (no packet is sent)."""
    for target in ("10.255.255.255", "192.168.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    return ""


def default_subnet() -> str:
    ip = local_ipv4()
    if not ip:
        return "192.168.1.0/24"
    return str(ipaddress.ip_network(ip + "/24", strict=False))


def identify(version: dict, stats: dict) -> dict:
    """Guess model and driver type from ``version`` / ``stats`` replies."""
    text = (json.dumps(version) + json.dumps(stats)).lower()
    ver = ((version or {}).get("VERSION") or [{}])[0] if isinstance((version or {}).get("VERSION"), list) else {}
    keys = {k.lower() for k in ver} if isinstance(ver, dict) else set()
    model = ""
    for source in (ver, *((stats or {}).get("STATS") or [])):
        if isinstance(source, dict) and source.get("Type"):
            model = str(source["Type"])
            break
    if "vnish" in text:
        kind = "vnish"
    elif any(k.startswith("bos") for k in keys) or "braiins" in text or "bosminer" in text:
        kind = "braiins"
    elif "luxminer" in keys or "luxminer" in text:
        kind = "luxos"
    elif "antminer" in model.lower() or "bmminer" in keys:
        kind = "antminer"
    else:
        kind = "cgminer"
    return {"model": model, "type": kind}


def probe(host: str, port: int = 4028, timeout: float = 0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    version, stats = {}, {}
    try:
        version = cgminer_command(host, port, "version", timeout=2.5)
    except MinerError:
        pass
    try:
        stats = cgminer_command(host, port, "stats", timeout=3.0)
    except MinerError:
        pass
    info = identify(version, stats)
    info.update({"host": host, "port": port})
    return info


def scan(subnet: str, port: int = 4028, timeout: float = 0.6, known_hosts=()) -> list:
    net = ipaddress.ip_network(subnet.strip(), strict=False)
    if net.version != 4:
        raise ValueError("csak IPv4 tartomány kereshető")
    if net.num_addresses > MAX_HOSTS + 2:
        raise ValueError("túl nagy tartomány – legfeljebb /22 (1024 cím) kereshető egyszerre")
    hosts = [str(h) for h in net.hosts()] or [str(net.network_address)]
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = [r for r in pool.map(lambda h: probe(h, port, timeout), hosts) if r]
    known = {h.lower() for h in known_hosts}
    for r in results:
        r["known"] = r["host"].lower() in known
    results.sort(key=lambda r: tuple(int(p) for p in r["host"].split(".")))
    return results
