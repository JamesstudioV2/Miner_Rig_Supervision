import json
import os
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from email import message_from_bytes
from email.header import decode_header, make_header

from rig_supervisor.config import ConfigStore
from rig_supervisor.datalog import DataLogger, day_of


class _Events:
    def __init__(self):
        self.items = []

    def add(self, level, message, code="", source=""):
        self.items.append(message)


def _snapshot(value):
    def fn():
        return {"sys": {"state": 2, "t1": value, "t2": value + 5, "t3": value + 4, "t4": value - 1, "flow1": 1.0,
                        "flow2": 1.0, "pump1": 1.0, "pump2": 1.0, "hashrate": 3.3e14, "power": 5000.0, "online": 1.0,
                        "mtemp": 60.0},
                "miners": [{"mid": "a", "name": "Hydro-01", "hashrate": 3.3e14, "temp_chip": 60.0, "temp_board": 50.0,
                            "power": 5000.0, "online": 1.0, "mining": 1.0}]}
    return fn


class DataLogTest(unittest.TestCase):
    def test_history_raw_and_rollup(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(os.path.join(d, "c.json"))
            log = DataLogger(os.path.join(d, "logs"), store, _snapshot(20.0), _Events())
            base = 1700000000 - 1700000000 % 86400 + 86400 - 600  # 10 minutes before UTC midnight
            for i in range(0, 1200, 10):  # 20 minutes, crossing midnight
                log.snapshot_fn = _snapshot(20.0 + i / 100.0)
                log.sample(ts=base + i)
            log.flush()
            self.assertEqual(len(log._files()), 2)
            h = log.history(base, base + 1200, points=600)
            self.assertEqual(h["source"], "raw")
            self.assertEqual(h["bucket"], 10)
            self.assertEqual(len(h["sys"]["t"]), 120)
            self.assertAlmostEqual(h["sys"]["t1"][0], 20.0)
            self.assertAlmostEqual(h["sys"]["t2"][-1], 20.0 + 1190 / 100.0 + 5, places=2)
            self.assertEqual(h["sys"]["run"][0], 1.0)
            self.assertEqual(h["miners"][0]["name"], "Hydro-01")
            # long range -> 5 minute averages
            log._close_conn()
            h2 = log.history(base - 30 * 86400, base + 1200, points=600)
            self.assertEqual(h2["source"], "5m")
            self.assertGreaterEqual(len(h2["sys"]["t"]), 1)
            rows = list(log.export_rows(base, base + 1200))
            self.assertEqual(len(rows), 120)
            log.stop()

    def test_size_limit_deletes_oldest(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(os.path.join(d, "c.json"))
            events = _Events()
            logs = os.path.join(d, "logs")
            log = DataLogger(logs, store, _snapshot(20.0), events)
            today = int(time.time())
            for days_back in (5, 4, 3, 2, 1):
                log.sample(ts=today - days_back * 86400)
            log.sample(ts=today)
            log._close_conn()
            files = log._files()
            self.assertEqual(len(files), 6)
            per_file = os.path.getsize(files[0])
            # limit = room for ~3 files -> the 3 oldest must go, today's file is kept
            store.update({"logging": {"max_size_gb": max(0.05, 3.5 * per_file / 1024 ** 3)}})
            limit = store.data["logging"]["max_size_gb"] * 1024 ** 3
            if limit < 6 * per_file:
                deleted = log.enforce_limits()
                self.assertTrue(deleted)
                remaining = [os.path.basename(f)[:10] for f in log._files()]
                self.assertIn(day_of(today), remaining)
                self.assertEqual(deleted[0], day_of(today - 5 * 86400))
                self.assertTrue(any("törölve" in m for m in events.items))
            else:  # tiny files: emulate a huge limit violation by patching the size function
                orig = DataLogger._file_size
                DataLogger._file_size = staticmethod(lambda p: int(limit / 3.5))
                try:
                    deleted = log.enforce_limits()
                finally:
                    DataLogger._file_size = staticmethod(orig)
                self.assertEqual(len(deleted), 3)
                self.assertEqual(deleted[0], day_of(today - 5 * 86400))
                self.assertIn(day_of(today), [os.path.basename(f)[:10] for f in log._files()])
            log.stop()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class WebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from rig_supervisor.app import App
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = App(cls.tmp.name, port=_free_port(), simulate=True)
        cls.app.start("127.0.0.1")
        cls.base = "http://127.0.0.1:%d" % cls.app.port
        time.sleep(1.5)

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        cls.tmp.cleanup()

    def req(self, path, body=None, ctype="application/json"):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET")
        if body is not None and ctype:
            r.add_header("Content-Type", ctype)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_state_and_static(self):
        st, d = self.req("/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(d["system"]["system"]["state"], "stopped")
        self.assertEqual(len(d["miners"]), 4)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.base + "/", timeout=5) as resp:
            self.assertIn(b"Rig Supervisor", resp.read())

    def test_post_requires_json(self):
        st, _ = self.req("/api/control/stop", {}, ctype="text/plain")
        self.assertEqual(st, 415)

    def test_config_masking_and_validation(self):
        st, d = self.req("/api/config")
        self.assertEqual(st, 200)
        self.assertNotIn("password_hash", d["config"]["web"])
        self.assertTrue(all(m["password"] is None for m in d["config"]["miners"]["list"]))
        st, d = self.req("/api/config", {"patch": {"sensors": {"t1": {"min": 80, "max": 10}}}})
        self.assertEqual(st, 400)
        self.assertTrue(d["errors"])
        st, d = self.req("/api/config", {"patch": {"safety": {"post_stop_pump_s": 12}}})
        self.assertEqual(st, 200)
        self.assertEqual(self.app.config.data["safety"]["post_stop_pump_s"], 12.0)

    def test_miner_passwords_kept_when_not_sent(self):
        st, d = self.req("/api/config")
        lst = d["config"]["miners"]["list"]
        self.app.config.update({"miners": {"list": [dict(m, password="secret%d" % i) for i, m in enumerate(
            self.app.config.data["miners"]["list"])]}})
        lst[0]["name"] = "Átnevezett"
        st, _ = self.req("/api/config", {"patch": {"miners": {"list": lst}}})
        self.assertEqual(st, 200)
        self.assertEqual(self.app.config.data["miners"]["list"][0]["password"], "secret0")
        self.assertEqual(self.app.config.data["miners"]["list"][0]["name"], "Átnevezett")

    def test_history_events_hw(self):
        now = int(time.time())
        st, d = self.req("/api/history?start=%d&end=%d&points=100" % (now - 600, now))
        self.assertEqual(st, 200)
        self.assertIn("t1", d["sys"])
        st, d = self.req("/api/events?limit=5")
        self.assertEqual(st, 200)
        self.assertLessEqual(len(d["events"]), 5)
        st, d = self.req("/api/hw")
        self.assertEqual(st, 200)
        self.assertIn("pump1", d["pins"])

    def test_calibrate(self):
        time.sleep(1.2)
        st, d = self.req("/api/calibrate", {"sensor": "t1", "reference": 30.0})
        self.assertEqual(st, 200, d)
        raw = self.app.io.sensor_snapshot()["t1"]["raw_value"]
        self.assertAlmostEqual(self.app.config.data["sensors"]["t1"]["offset"], round(30.0 - raw, 2), delta=0.5)
        self.req("/api/config", {"patch": {"sensors": {"t1": {"offset": 0}}}})

    def test_miner_start_requires_running_system(self):
        mid = self.app.config.data["miners"]["list"][0]["id"]
        st, d = self.req("/api/miners/%s/start" % mid, {})
        self.assertEqual(st, 409)

    def test_notification_secrets_and_test_send(self):
        from tests.test_notify import FakeSMTP

        srv = FakeSMTP(user="rig@example.com", password="titok")
        try:
            email = {"smtp_host": "127.0.0.1", "smtp_port": srv.port, "security": "none",
                     "username": "rig@example.com", "password": "titok", "recipients": "op@example.com"}
            self.app.config.update({"notifications": {"email": email}})
            st, d = self.req("/api/config")
            em = d["config"]["notifications"]["email"]
            self.assertIsNone(em["password"])                      # secrets never reach the browser
            self.assertTrue(em["has_password"])
            self.assertTrue(d["config"]["notifications"]["sms"]["has_gsm_pin"] is False)
            em["enabled"] = True
            st, d = self.req("/api/config", {"patch": {"notifications": {"email": em}}})
            self.assertEqual(st, 200, d)
            self.assertEqual(self.app.config.data["notifications"]["email"]["password"], "titok")  # null = keep
            self.assertNotIn("has_password", self.app.config.data["notifications"]["email"])

            # test send with the form's (unsaved) values, stored password used
            st, d = self.req("/api/notify/test", {"channel": "email", "settings": {"email": dict(
                em, recipients="other@example.com")}})
            self.assertEqual(st, 200, d)
            self.assertEqual(srv.messages[-1]["rcpt"], ["other@example.com"])
            subject = message_from_bytes(srv.messages[-1]["data"])["Subject"]
            self.assertIn("Teszt értesítés", str(make_header(decode_header(subject))))
            st, d = self.req("/api/notify/test", {"channel": "email", "settings": {"email": dict(em, password="rossz")}})
            self.assertEqual(st, 502)
            self.assertIn("bejelentkezés", d["error"])
            st, d = self.req("/api/notify/test", {"channel": "email", "settings": {"email": dict(em, smtp_host="")}})
            self.assertEqual(st, 400)
            self.assertIn("SMTP szerver", d["error"])
            st, d = self.req("/api/notify/test", {"channel": "sms", "settings": {"sms": {
                "provider": "gsm", "gsm_port": "/dev/nincs-ilyen-port", "recipients": "06 30 123 4567"}}})
            self.assertEqual(st, 502)
            self.assertIn("nem található", d["error"])
            st, d = self.req("/api/notify")
            self.assertEqual(st, 200)
            self.assertEqual([h["ok"] for h in d["history"][:3]], [False, False, True])
            self.assertTrue(all(h["test"] for h in d["history"][:3]))

            # an explicit empty string clears a secret
            st, d = self.req("/api/config", {"patch": {"notifications": {"email": {"enabled": False, "password": ""}}}})
            self.assertEqual(st, 200, d)
            self.assertEqual(self.app.config.data["notifications"]["email"]["password"], "")
        finally:
            self.app.config.update({"notifications": {"email": {"enabled": False}}})
            srv.stop()


if __name__ == "__main__":
    unittest.main()
