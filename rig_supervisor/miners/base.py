"""Common miner data model and driver interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .transport import MinerError, cgminer_command


@dataclass
class MinerData:
    model: str = ""
    firmware: str = ""
    hashrate: Optional[float] = None        # H/s (5 s átlag)
    hashrate_avg: Optional[float] = None    # H/s (hosszabb átlag)
    hashrate_ideal: Optional[float] = None  # H/s (névleges)
    temp_chip: Optional[float] = None       # legmagasabb chip hőmérséklet (°C)
    temp_board: Optional[float] = None      # legmagasabb panel (PCB) hőmérséklet (°C)
    boards: list = field(default_factory=list)
    fans: list = field(default_factory=list)
    power: Optional[float] = None           # W (ha a firmware jelenti)
    uptime: Optional[int] = None            # s
    pool: Optional[dict] = None
    mode: str = "unknown"                   # normal | sleep | unknown
    errors: list = field(default_factory=list)
    accepted: Optional[int] = None
    rejected: Optional[int] = None
    hw_errors: Optional[int] = None
    expected_boards: Optional[int] = None

    def finalize(self) -> "MinerData":
        chips = [b["chip"] for b in self.boards if b.get("chip") is not None]
        pcbs = [b["board"] for b in self.boards if b.get("board") is not None]
        if chips:
            self.temp_chip = max(chips)
        if pcbs:
            self.temp_board = max(pcbs)
        seen = set()
        unique = []
        for e in self.errors:
            if e and e not in seen:
                seen.add(e)
                unique.append(e)
        self.errors = unique
        return self

    def max_temp(self) -> Optional[float]:
        temps = [t for t in (self.temp_chip, self.temp_board) if t is not None]
        return max(temps) if temps else None

    def to_dict(self) -> dict:
        return asdict(self)


def add_derived_errors(md: MinerData) -> None:
    """Errors deduced from the raw statistics (bad chips, dead boards, fans)."""
    if md.mode == "sleep":
        return
    for b in md.boards:
        if b.get("bad_chips"):
            md.errors.append("%d. hashboard: %d hibás chip" % (b["idx"], b["bad_chips"]))
    running = (md.hashrate or 0) > 0 and (md.uptime or 0) > 900
    if running:
        for b in md.boards:
            if b.get("hashrate") is not None and b["hashrate"] <= 0:
                md.errors.append("%d. hashboard nem termel" % b["idx"])
        if md.expected_boards and len(md.boards) < md.expected_boards:
            md.errors.append("Csak %d/%d hashboard aktív" % (len(md.boards), md.expected_boards))
        if md.fans and any(f == 0 for f in md.fans):
            md.errors.append("Ventilátor hiba (0 RPM)")


class MinerDriver:
    type_id = ""
    label = ""
    note = ""
    can_control = True
    can_reboot = True
    default_username = "root"
    default_password = "root"

    def __init__(self, cfg: dict, timeout: float = 4.0, sim_world=None):
        self.cfg = cfg
        self.host = cfg["host"]
        self.api_port = int(cfg.get("api_port") or 4028)
        self.web_port = int(cfg.get("web_port") or 80)
        self.username = cfg.get("username") or self.default_username
        pw = cfg.get("password")
        self.password = self.default_password if pw is None else pw
        self.timeout = float(timeout)

    def rpc(self, command: str, parameter=None, timeout=None) -> dict:
        return cgminer_command(self.host, self.api_port, command, parameter, timeout or self.timeout)

    def web_base(self) -> str:
        if self.web_port == 80:
            return "http://%s" % self.host
        return "http://%s:%d" % (self.host, self.web_port)

    # --- interface -------------------------------------------------------
    def poll(self) -> MinerData:
        raise NotImplementedError

    def stop(self) -> str:
        raise MinerError("ez a miner típus nem vezérelhető")

    def start(self) -> str:
        raise MinerError("ez a miner típus nem vezérelhető")

    def reboot(self) -> str:
        raise MinerError("az újraindítás ennél a típusnál nem támogatott")
