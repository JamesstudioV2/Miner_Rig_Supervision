"""Application wiring and lifecycle."""

from __future__ import annotations

import logging
import os
import threading
import time

from . import __version__
from .config import ConfigStore, new_miner_id
from .controller import Controller
from .datalog import DataLogger
from .events import EventStore
from .hardware.gpio import is_raspberry_pi
from .hardware.io import IOManager
from .hardware.sim import SimWorld
from .miners.manager import MinerManager
from .util import ChangeSignal

log = logging.getLogger(__name__)


def _demo_miners() -> list:
    return [{"id": new_miner_id(), "name": "Hydro-%02d" % i, "host": "192.168.1.%d" % (100 + i),
             "type": "simulated", "api_port": 4028, "web_port": 80, "username": "root", "password": "",
             "enabled": True} for i in range(1, 5)]


class App:
    def __init__(self, data_dir: str, port=None, simulate=None):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.signal = ChangeSignal()
        self.config = ConfigStore(os.path.join(data_dir, "config.json"))
        backend = self.config.data["hardware"]["backend"]
        if simulate is None:
            simulate = backend == "sim" or (backend == "auto" and not is_raspberry_pi())
        self.simulated = bool(simulate)
        if self.simulated and self.config.created and not self.config.data["miners"]["list"]:
            self.config.update({"miners": {"list": _demo_miners()}})
        self.events = EventStore(os.path.join(data_dir, "events.sqlite"))
        self.sim = SimWorld(self.config) if self.simulated else None
        self.io = IOManager(self.config, self.events, use_sim=self.simulated, sim_world=self.sim)
        self.miners = MinerManager(self.config, self.events, self.signal, sim_world=self.sim)
        self.controller = Controller(self.config, self.io, self.miners, self.events, self.signal,
                                     os.path.join(data_dir, "state.json"))
        self.datalog = DataLogger(os.path.join(data_dir, "logs"), self.config, self.log_snapshot, self.events)
        self.port = int(port if port is not None else self.config.data["web"]["port"])
        self.web = None
        self.stopping = False
        self.auth_cache = set()
        self.sse_lock = threading.Lock()
        self.sse_clients = 0
        self.events.add_listener(lambda _e: self.signal.bump())

    def start(self, host: str = "0.0.0.0") -> None:
        from .web.server import WebServer

        self.events.add("info", "Program indítása (v%s)%s" % (
            __version__, " – SZIMULÁCIÓS MÓD (nincs valódi hardver)" if self.simulated else ""), source="rendszer")
        self.io.start(self.controller.initial_outputs())
        if self.sim is not None:
            self.sim.start()
        self.miners.start()
        self.controller.start()
        self.datalog.start()
        self.web = WebServer(self, host, self.port)
        self.port = self.web.port
        self.web.start()
        log.info("Webes felület: http://%s:%d/", host if host != "0.0.0.0" else "<raspberry-ip>", self.port)

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        self.events.add("info", "Program leállítása (a szivattyúk állapota változatlan marad)", source="rendszer")
        if self.web is not None:
            self.web.shutdown()
        self.controller.stop()
        self.datalog.stop()
        self.miners.stop()
        if self.sim is not None:
            self.sim.stop()
        self.io.stop()
        self.events.close()

    # ------------------------------------------------------------------ views
    def live_state(self) -> dict:
        miners = self.miners.snapshot()
        return {
            "system": self.controller.snapshot(),
            "miners": miners,
            "summary": self.miners.summary(miners),
            "events": self.events.recent(12),
            "server_time": time.time(),
            "version": __version__,
            "sim": self.simulated,
        }

    def log_snapshot(self) -> dict:
        snap = self.controller.snapshot()
        miners = self.miners.snapshot()
        summary = self.miners.summary(miners)
        temps = snap["temps"]

        def flag(v):
            return 1.0 if v else 0.0

        sys_row = {
            "state": self.controller.state_code(),
            "t1": temps["t1"]["value"], "t2": temps["t2"]["value"],
            "t3": temps["t3"]["value"], "t4": temps["t4"]["value"],
            "flow1": flag(snap["flows"]["flow1"]["flow"]), "flow2": flag(snap["flows"]["flow2"]["flow"]),
            "pump1": flag(snap["pumps"]["pump1"]["on"]), "pump2": flag(snap["pumps"]["pump2"]["on"]),
            "hashrate": summary["hashrate"] if summary["online"] else None,
            "power": summary["power"],
            "online": float(summary["online"]),
            "mtemp": summary["max_temp"],
        }
        rows = []
        for m in miners:
            if not m["enabled"]:
                continue
            d = m["data"] if m["online"] else None
            rows.append({
                "mid": m["id"], "name": m["name"],
                "hashrate": d.get("hashrate") if d else None,
                "temp_chip": d.get("temp_chip") if d else None,
                "temp_board": d.get("temp_board") if d else None,
                "power": d.get("power") if d else None,
                "online": flag(m["online"]),
                "mining": flag(m["status"] == "mining"),
            })
        return {"sys": sys_row, "miners": rows}
