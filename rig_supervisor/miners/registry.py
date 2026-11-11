"""Driver registry."""

from __future__ import annotations

from ..config import MINER_TYPES
from .antminer import AntminerStock
from .others import BraiinsOS, GenericCGMiner, LuxOS, VNish
from .simulated import SimulatedMiner

DRIVERS = {cls.type_id: cls for cls in (AntminerStock, BraiinsOS, VNish, LuxOS, GenericCGMiner, SimulatedMiner)}
assert set(DRIVERS) == set(MINER_TYPES), "a driver lista és a config.MINER_TYPES eltér"


def driver_types() -> list:
    return [{
        "id": key,
        "label": cls.label,
        "note": cls.note,
        "can_control": cls.can_control,
        "can_reboot": cls.can_reboot,
        "default_username": cls.default_username,
        "default_password": cls.default_password,
    } for key, cls in DRIVERS.items()]


def create_driver(cfg: dict, timeout: float, sim_world=None):
    return DRIVERS[cfg["type"]](cfg, timeout, sim_world)
