import os
import tempfile
import time
import unittest

from rig_supervisor.config import ConfigStore
from rig_supervisor.events import EventStore
from rig_supervisor.miners import antminer as A
from rig_supervisor.miners import manager as M
from rig_supervisor.miners.base import MinerData
from rig_supervisor.miners.parsing import (mode_from_conf, parse_bmminer_stats, parse_pools, parse_summary,
                                           parse_web_stats, parse_web_summary_errors)
from rig_supervisor.miners.scanner import identify
from rig_supervisor.miners.transport import MinerAuthError, MinerError, parse_cgminer_json
from rig_supervisor.util import ChangeSignal

from .fake_antminer import FakeAntminer, POOLS, STATS, SUMMARY


class ParsingTest(unittest.TestCase):
    def test_json_repairs(self):
        broken = b'{"STATUS":[{"STATUS":"S"}],"STATS":[{"Type":"Antminer S9"}{"GHS 5s":"13500.1","temp":inf}],"id":1}\x00'
        d = parse_cgminer_json(broken)
        self.assertEqual(len(d["STATS"]), 2)
        self.assertEqual(d["STATS"][1]["temp"], 0)
        with self.assertRaises(MinerError):
            parse_cgminer_json(b'{"STATUS":[{"STATUS":"E","Msg":"Access denied"}]}')

    def test_s19_stats(self):
        md = MinerData()
        self.assertTrue(parse_bmminer_stats(STATS, md))
        md.finalize()
        self.assertEqual(md.model, "Antminer S19j Pro")
        self.assertAlmostEqual(md.hashrate, 104283.72e9)
        self.assertAlmostEqual(md.hashrate_ideal, 104000.0e9)
        self.assertEqual(len(md.boards), 3)
        self.assertEqual(md.temp_chip, 79)
        self.assertEqual(md.temp_board, 64)
        self.assertEqual(md.boards[1]["bad_chips"], 1)
        self.assertEqual(md.fans, [5400, 5460, 5400, 0])

    def test_s9_stats(self):
        s9 = {"STATS": [{"Type": "Antminer S9"}, {
            "GHS 5s": "13500.5", "fan_num": 2, "fan3": 4800, "fan6": 4920,
            "temp6": 56, "temp7": 57, "temp8": 55, "temp2_6": 71, "temp2_7": 72, "temp2_8": 70,
            "chain_acn6": 63, "chain_acn7": 63, "chain_acn8": 63,
            "chain_rate6": "4500.1", "chain_rate7": "4500.2", "chain_rate8": "4500.2"}]}
        md = MinerData()
        parse_bmminer_stats(s9, md)
        md.finalize()
        self.assertEqual([b["chain"] for b in md.boards], [6, 7, 8])
        self.assertEqual(md.temp_chip, 72)
        self.assertEqual(md.temp_board, 57)
        self.assertEqual(md.fans, [4800, 4920])

    def test_web_stats_and_summary(self):
        web = {"INFO": {"type": "Antminer S21 Hyd."}, "STATS": [{
            "elapsed": 100, "rate_5s": 335000.0, "rate_avg": 334000.0, "rate_ideal": 335000.0, "rate_unit": "GH/s",
            "fan": [], "miner-mode": 1,
            "chain": [{"index": 0, "rate_real": 111000.0, "asic_num": 65, "temp_pcb": [40, 41, 52, 53],
                       "temp_chip": [60, 61, 70, 71], "asic": "oooo"}]}]}
        md = MinerData()
        parse_web_stats(web, md)
        md.finalize()
        self.assertEqual(md.model, "Antminer S21 Hyd.")
        self.assertEqual(md.mode, "sleep")
        self.assertAlmostEqual(md.hashrate, 335e12)
        self.assertEqual(md.temp_chip, 71)
        md2 = MinerData()
        parse_summary(SUMMARY, md2)
        parse_pools(POOLS, md2)
        self.assertEqual(md2.accepted, 1234)
        self.assertEqual(md2.pool["url"], "stratum+tcp://pool.example.com:3333")
        errors = parse_web_summary_errors({"SUMMARY": [{"status": [
            {"type": "rate", "status": "s"}, {"type": "fans", "status": "e", "code": 2, "msg": "Fan lost"}]}]})
        self.assertEqual(errors, ["Ventilátor: Fan lost (kód 2)"])

    def test_mode_and_payload(self):
        self.assertEqual(mode_from_conf({"bitmain-work-mode": "1"}), "sleep")
        self.assertEqual(mode_from_conf({"bitmain-work-mode": "0"}), "normal")
        self.assertEqual(mode_from_conf({}), "unknown")
        p = A.build_conf_payload({"bitmain-fan-ctrl": "true", "bitmain-fan-pwm": 80,
                                  "pools": [{"url": "u", "user": "w", "pass": "x"}]}, 1)
        self.assertEqual(p["miner-mode"], 1)
        self.assertIs(p["bitmain-fan-ctrl"], True)
        self.assertEqual(p["bitmain-fan-pwm"], "80")
        self.assertEqual(len(p["pools"]), 3)
        self.assertEqual(A.build_conf_payload({}, 0, as_string=True)["miner-mode"], "0")

    def test_identify(self):
        self.assertEqual(identify({"VERSION": [{"BMMiner": "2.0", "Type": "Antminer S19"}]}, {})["type"], "antminer")
        self.assertEqual(identify({"VERSION": [{"BOSminer": "0.2"}]}, {})["type"], "braiins")
        self.assertEqual(identify({"VERSION": [{"LUXminer": "2024"}]}, {})["type"], "luxos")
        self.assertEqual(identify({"VERSION": [{"BMMiner": "2", "Type": "Antminer S19 (Vnish 1.2.4)"}]}, {})["type"], "vnish")


class FakeAntminerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAntminer().start()

    def tearDown(self):
        self.fake.stop()

    def test_poll(self):
        drv = A.AntminerStock(self.fake.miner_cfg(), timeout=3)
        md = drv.poll()
        self.assertEqual(md.model, "Antminer S19j Pro")
        self.assertEqual(md.mode, "normal")
        self.assertIn("Ventilátor: Fan lost (kód 2)", md.errors)
        self.assertEqual(md.pool["user"], "worker.1")

    def test_stop_and_start(self):
        A.AntminerStock.verify_delay_s = 0.05
        drv = A.AntminerStock(self.fake.miner_cfg(), timeout=3)
        self.assertIn("alvó", drv.stop())
        self.assertEqual(self.fake.conf["bitmain-work-mode"], "1")
        post = self.fake.posts[-1]
        self.assertEqual(post["miner-mode"], 1)
        self.assertEqual(post["pools"][0]["url"], "stratum+tcp://pool.example.com:3333")
        self.assertEqual(drv.poll().mode, "sleep")
        # idempotent: no second config write when already sleeping
        n = len(self.fake.posts)
        self.assertIn("már", drv.stop())
        self.assertEqual(len(self.fake.posts), n)
        drv.start()
        self.assertEqual(self.fake.conf["bitmain-work-mode"], "0")
        drv.reboot()
        self.assertEqual(self.fake.reboots, 1)

    def test_string_mode_firmware(self):
        self.fake.stop()
        self.fake = FakeAntminer(string_mode_only=True).start()
        A.AntminerStock.verify_delay_s = 0.05
        drv = A.AntminerStock(self.fake.miner_cfg(), timeout=3)
        drv.stop()
        self.assertEqual(self.fake.conf["bitmain-work-mode"], "1")
        self.assertEqual([p["miner-mode"] for p in self.fake.posts], [1, "1"])

    def test_wrong_password(self):
        drv = A.AntminerStock(self.fake.miner_cfg(password="wrong"), timeout=3)
        md = drv.poll()  # reading still works through the 4028 API
        self.assertTrue(any("bejelentkezés" in e for e in md.errors))
        with self.assertRaises(MinerAuthError):
            drv.stop()

    def test_offline(self):
        cfg = self.fake.miner_cfg()
        self.fake.stop()
        drv = A.AntminerStock(cfg, timeout=0.5)
        with self.assertRaises(MinerError):
            drv.poll()
        self.fake = FakeAntminer().start()  # for tearDown


class _Events:
    def __init__(self):
        self.items = []

    def add(self, level, message, code="", source=""):
        self.items.append((level, code, message))


class ManagerTest(unittest.TestCase):
    def test_commands_and_reconcile(self):
        from rig_supervisor.hardware.sim import SimWorld
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(os.path.join(d, "c.json"))
            store.update({"miners": {"list": [
                {"id": "a", "name": "A", "host": "10.0.0.1", "type": "simulated"},
                {"id": "b", "name": "B", "host": "10.0.0.2", "type": "simulated"}]}})
            world = SimWorld(store)
            events = _Events()
            mgr = M.MinerManager(store, events, ChangeSignal(), sim_world=world)
            try:
                self.assertEqual(mgr.command_all("start", reason="teszt"), 2)
                deadline = time.time() + 5
                while time.time() < deadline and any(m.mode != "normal" for m in world.miners.values()):
                    time.sleep(0.05)
                self.assertTrue(all(m.mode == "normal" for m in world.miners.values()))
                # a miner that ignores the stop command gets it again
                world.miners["b"].ignore_stop = True
                mgr.command_all("stop")
                time.sleep(0.6)
                self.assertEqual(world.miners["a"].mode, "sleep")
                self.assertEqual(world.miners["b"].mode, "normal")
                old = M.RESEND_S
                M.RESEND_S = 0.0
                try:
                    mgr.poll_all()
                    time.sleep(1.0)
                finally:
                    M.RESEND_S = old
                stop_msgs = [m for lvl, _, m in events.items if m.startswith("B: leállítás")]
                self.assertGreaterEqual(len(stop_msgs), 2)
                snap = {m["id"]: m for m in mgr.snapshot()}
                self.assertEqual(snap["a"]["status"], "sleeping")
                self.assertFalse(snap["b"]["desired"])
                # per miner command
                ok, _ = mgr.command_one("a", "start")
                self.assertTrue(ok)
                ok, msg = mgr.command_one("zzz", "start")
                self.assertFalse(ok)
            finally:
                mgr.stop()


if __name__ == "__main__":
    unittest.main()
