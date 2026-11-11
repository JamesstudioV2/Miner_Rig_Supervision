"""Simulated miner (demo / testing without real hardware)."""

from __future__ import annotations

import random
import time

from ..hardware.sim import SimMiner
from .base import MinerData, MinerDriver
from .transport import MinerConnectionError


class SimulatedMiner(MinerDriver):
    type_id = "simulated"
    label = "Szimulált miner (teszteléshez)"
    note = "Nem valódi eszköz – a program kipróbálására szolgál."
    default_password = ""

    def __init__(self, cfg, timeout=4.0, sim_world=None):
        super().__init__(cfg, timeout, sim_world)
        self.world = sim_world
        if sim_world is not None:
            self.model = sim_world.get_miner(cfg["id"], cfg["name"])
        else:
            self.model = SimMiner(cfg["id"], cfg["name"])

    def poll(self) -> MinerData:
        time.sleep(random.uniform(0.02, 0.15))
        m = self.model
        if m.offline:
            raise MinerConnectionError("API kapcsolódás sikertelen (%s): időtúllépés (szimulált)" % self.host)
        now = time.time()
        if self.world is None:
            m.chip_temp = 68.0 if m.producing(now) else 24.0
            m.board_temp = 55.0 if m.producing(now) else 24.0
        producing = m.producing(now)
        hs = m.nominal_hs * random.uniform(0.975, 1.015) if producing else 0.0
        md = MinerData(model=m.model, firmware="szimulátor 1.0", mode=m.mode)
        md.hashrate = hs
        md.hashrate_avg = m.nominal_hs * 0.996 if producing else 0.0
        md.hashrate_ideal = m.nominal_hs
        md.boards = [{
            "idx": i + 1, "chain": i + 1, "hashrate": hs / 3.0,
            "chip": round(m.chip_temp + random.uniform(-1.2, 1.2) + i, 1),
            "board": round(m.board_temp + random.uniform(-0.8, 0.8), 1),
            "asics": 65, "bad_chips": 0,
        } for i in range(3)]
        md.power = round(m.power_w * random.uniform(0.985, 1.01)) if producing else 22.0
        md.uptime = int(now - m.booted_at)
        md.pool = {"url": "stratum+tcp://pool.example.com:3333", "user": "telep.%s" % self.cfg["name"],
                   "status": "Alive", "accepted": int((now - m.booted_at) / 7), "rejected": 0}
        if m.injected_error:
            md.errors.append(m.injected_error)
        return md.finalize()

    def stop(self) -> str:
        time.sleep(0.2)
        if self.model.offline:
            raise MinerConnectionError("a miner nem érhető el (szimulált)")
        self.model.set_mode("sleep", time.time())
        return "alvó üzemmód beállítva"

    def start(self) -> str:
        time.sleep(0.2)
        if self.model.offline:
            raise MinerConnectionError("a miner nem érhető el (szimulált)")
        self.model.set_mode("normal", time.time())
        return "normál üzemmód beállítva"

    def reboot(self) -> str:
        m = self.model
        m.booted_at = time.time()
        if m.mode == "normal":
            m.hash_start_at = time.time() + 15.0
        return "újraindítás elindítva"
