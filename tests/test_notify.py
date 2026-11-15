"""Notification system: classification, batching, limits, retries and the real senders
against fake servers (SMTP, HTTP) and an emulated GSM modem on a pseudo terminal."""

import base64
import json
import os
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from email import message_from_bytes
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from rig_supervisor import notify as N
from rig_supervisor.config import ConfigStore
from rig_supervisor.miners import manager as M
from rig_supervisor.util import ChangeSignal

NO_PROXY = {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}


def ev(code="", level="info", message="x", source="teszt"):
    return {"id": 1, "ts": time.time(), "code": code, "level": level, "message": message, "source": source}


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ----------------------------------------------------------------- fake servers
class FakeSMTP:
    """Tiny ESMTP server with AUTH PLAIN (no TLS) - records every accepted message."""

    def __init__(self, user="rig@example.com", password="titok"):
        self.messages = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line):
                self.wfile.write((line + "\r\n").encode())

            def handle(self):
                self.reply("220 fake ESMTP")
                rcpt, mail_from, authed = [], None, False
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    cmd = line.decode().strip()
                    up = cmd.upper()
                    if up.startswith("EHLO"):
                        self.wfile.write(b"250-fake\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
                    elif up.startswith("AUTH PLAIN"):
                        parts = base64.b64decode(cmd.split()[2]).split(b"\0")
                        if parts[1].decode() == user and parts[2].decode() == password:
                            authed = True
                            self.reply("235 ok")
                        else:
                            self.reply("535 5.7.8 Authentication failed")
                    elif up.startswith("MAIL FROM"):
                        mail_from = cmd
                        self.reply("250 ok" if authed else "530 auth required")
                    elif up.startswith("RCPT TO"):
                        rcpt.append(cmd.split(":", 1)[1].strip("<> "))
                        self.reply("250 ok")
                    elif up == "DATA":
                        self.reply("354 go")
                        data = b""
                        while True:
                            chunk = self.rfile.readline()
                            if chunk in (b".\r\n", b".\n", b""):
                                break
                            data += chunk
                        outer.messages.append({"from": mail_from, "rcpt": list(rcpt), "data": data})
                        self.reply("250 queued")
                    elif up == "QUIT":
                        self.reply("221 bye")
                        return
                    else:
                        self.reply("250 ok")

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeHTTP:
    def __init__(self, status=201, reply=b'{"sid": "SM1"}'):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                       "body": self.rfile.read(length).decode()})
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.reply)))
                self.end_headers()
                self.wfile.write(outer.reply)

            do_GET = do_POST = _do

        self.status, self.reply = status, reply
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeModem:
    """GSM modem on the master side of a pseudo terminal (SIM PIN, text mode SMS)."""

    def __init__(self, pin="1234"):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        self._slave = slave
        self.pin = pin
        self.unlocked = pin is None
        self.sent = []
        self.commands = []
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _w(self, text):
        os.write(self.master, text.encode())

    def _run(self):
        buf = b""
        sms_to = None
        while not self._stop:
            try:
                data = os.read(self.master, 1024)
            except OSError:
                return
            buf += data
            while True:
                if sms_to is not None:
                    if b"\x1a" not in buf:
                        break
                    text, buf = buf.split(b"\x1a", 1)
                    self.sent.append((sms_to, text.decode("ascii")))
                    sms_to = None
                    self._w("\r\n+CMGS: %d\r\n\r\nOK\r\n" % len(self.sent))
                    continue
                if b"\r" not in buf:
                    break
                line, buf = buf.split(b"\r", 1)
                cmd = line.decode().strip()
                if not cmd:
                    continue
                self.commands.append(cmd)
                if cmd == "AT+CPIN?":
                    self._w("\r\n+CPIN: %s\r\n\r\nOK\r\n" % ("READY" if self.unlocked else "SIM PIN"))
                elif cmd.startswith("AT+CPIN="):
                    ok = cmd == 'AT+CPIN="%s"' % self.pin
                    self.unlocked = self.unlocked or ok
                    self._w("\r\nOK\r\n" if ok else "\r\n+CME ERROR: 16\r\n")
                elif cmd.startswith("AT+CMGS="):
                    sms_to = cmd.split('"')[1]
                    self._w("\r\n> ")
                else:
                    self._w("\r\nOK\r\n")

    def close(self):
        self._stop = True
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass


# ------------------------------------------------------------------------ tests
class HelpersTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(N.classify(ev("E001", "error")), "killswitch")
        self.assertEqual(N.classify(ev("E102", "error")), "emergency")
        self.assertEqual(N.classify(ev("E213", "error")), "emergency")
        self.assertEqual(N.classify(ev("I601")), "manual")
        self.assertEqual(N.classify(ev("I603")), "manual")
        self.assertEqual(N.classify(ev("W406", "warning")), "miner_production")
        self.assertEqual(N.classify(ev("I606")), "miner_production")
        self.assertIsNone(N.classify(ev("I606"), notify_recovery=False))
        self.assertEqual(N.classify(ev("I604")), "restart")
        self.assertEqual(N.classify(ev("E301", "error")), "errors")
        self.assertEqual(N.classify(ev("W402", "warning")), "warnings")
        self.assertIsNone(N.classify(ev("", "info")))

    def test_ascii_and_sms_text(self):
        self.assertEqual(N.to_ascii("Árvíztűrő tükörfúrógép – 24,5 °C [ok]"), "Arvizturo tukorfurogep - 24,5 C (ok)")
        items = [{"ts": time.time(), "code": "W402", "message": "Hydro-01: ventilátor", "category": "warnings",
                  "level": "warning", "source": ""},
                 {"ts": time.time(), "code": "E102", "message": "VÉSZLEÁLLÁS E102 – megszűnt az áramlás",
                  "category": "emergency", "level": "error", "source": ""}]
        text = N.build_sms("Hydro telep", items, True, 160)
        self.assertTrue(text.startswith("(Hydro telep) VESZLEALLAS E102 - megszunt az aramlas"))
        self.assertIn("+1 tovabbi", text)
        self.assertLessEqual(len(N.build_sms("x" * 50, items * 20, True, 160)), 160)
        subject, body = N.build_email("Hydro telep", items, ["Rendszer: VÉSZLEÁLLÁS"], "http://10.0.0.5:8080/")
        self.assertEqual(subject, "[Hydro telep] VÉSZLEÁLLÁS: E102 Belső kör: megszűnt a vízáramlás üzem közben "
                                  "(+1 további)")
        self.assertIn("Hydro-01: ventilátor", body)
        self.assertIn("http://10.0.0.5:8080/", body)
        prod = {"ts": time.time(), "code": "W406", "message": "Hydro-02 abbahagyta a termelést: nem elérhető",
                "category": "miner_production", "level": "warning", "source": "miner"}
        subject, _ = N.build_email("Telep", [prod], [], "")
        self.assertEqual(subject, "[Telep] MINER TERMELÉS: W406 Hydro-02 abbahagyta a termelést: nem elérhető")

    def test_e164(self):
        self.assertEqual(N.e164("06301234567"), "+36301234567")
        self.assertEqual(N.e164("36301234567"), "+36301234567")
        self.assertEqual(N.e164("+447700900123"), "+447700900123")


class SendersTest(unittest.TestCase):
    def test_smtp(self):
        srv = FakeSMTP()
        try:
            cfg = {"smtp_host": "127.0.0.1", "smtp_port": srv.port, "security": "none", "username": "rig@example.com",
                   "password": "titok", "sender": "", "recipients": "a@example.com, b@example.com"}
            N.send_email(cfg, "[Telep] VÉSZLEÁLLÁS: E102", "Árvíztűrő tükörfúrógép\n")
            msg = srv.messages[0]
            self.assertEqual(msg["rcpt"], ["a@example.com", "b@example.com"])
            parsed = message_from_bytes(msg["data"])
            self.assertEqual(str(make_header(decode_header(parsed["Subject"]))), "[Telep] VÉSZLEÁLLÁS: E102")
            self.assertIn("Árvíztűrő", parsed.get_payload(decode=True).decode("utf-8"))
            cfg["password"] = "rossz"
            with self.assertRaises(N.NotifyError) as ctx:
                N.send_email(cfg, "s", "b")
            self.assertIn("bejelentkezés", str(ctx.exception))
        finally:
            srv.stop()
        with self.assertRaises(N.NotifyError) as ctx:
            N.send_email(cfg, "s", "b", timeout=2)
        self.assertIn("nem érhető el", str(ctx.exception))

    def test_twilio_and_http_gateway(self):
        srv = FakeHTTP()
        try:
            with mock.patch.dict(os.environ, NO_PROXY):
                base = "http://127.0.0.1:%d/2010-04-01/Accounts/{sid}/Messages.json" % srv.port
                N.send_twilio({"twilio_sid": "AC123", "twilio_token": "tok", "twilio_from": "+15005550006"},
                              "Teszt uzenet", ["06301234567"], base_url=base)
                req = srv.requests[-1]
                self.assertEqual(req["path"], "/2010-04-01/Accounts/AC123/Messages.json")
                self.assertEqual(req["headers"]["Authorization"], "Basic " + base64.b64encode(b"AC123:tok").decode())
                self.assertIn("To=%2B36301234567", req["body"])
                self.assertIn("Body=Teszt+uzenet", req["body"])

                srv.status, srv.reply = 200, b"OK"
                cfg = {"http_method": "GET", "http_url": "http://127.0.0.1:%d/send?to={to_plain}&msg={message}" % srv.port,
                       "http_headers": "X-Api-Key: abc"}
                N.send_http(cfg, "Hő: 60 °C & más", ["+36301234567", "+36201112222"])
                self.assertEqual(len(srv.requests), 3)
                self.assertEqual(srv.requests[-2]["path"], "/send?to=36301234567&msg=H%C5%91%3A%2060%20%C2%B0C%20%26%20m%C3%A1s")
                self.assertEqual(srv.requests[-1]["headers"]["X-Api-Key"], "abc")

                cfg = {"http_method": "POST", "http_url": "http://127.0.0.1:%d/api" % srv.port,
                       "http_body": '{"to": "{to}", "text": "{message}"}', "http_content_type": "application/json"}
                N.send_http(cfg, 'Idéző "jel"', ["+36301234567"])
                body = json.loads(srv.requests[-1]["body"])
                self.assertEqual(body, {"to": "+36301234567", "text": 'Idéző "jel"'})

                # no {to}: one request, phone numbers are not needed (e.g. a Telegram bot)
                cfg2 = {"provider": "http", "recipients": "", "http_method": "GET",
                        "http_url": "http://127.0.0.1:%d/bot/sendMessage?chat_id=42&text={message}" % srv.port}
                N.send_sms(cfg2, "Riasztás", "Telep")
                self.assertEqual(srv.requests[-1]["path"], "/bot/sendMessage?chat_id=42&text=Riaszt%C3%A1s")
                with self.assertRaises(N.NotifyError):
                    N.send_sms(dict(cfg2, http_url=cfg2["http_url"] + "&to={to}"), "x", "Telep")

                srv.status, srv.reply = 401, b'{"message": "invalid api key"}'
                with self.assertRaises(N.NotifyError) as ctx:
                    N.send_http(cfg, "x", ["+36301234567"])
                self.assertIn("invalid api key", str(ctx.exception))
        finally:
            srv.stop()

    def test_gsm_modem(self):
        modem = FakeModem(pin="1234")
        try:
            cfg = {"gsm_port": modem.path, "gsm_baud": 115200, "gsm_pin": "1234"}
            N.send_gsm(cfg, "[Telep] VÉSZLEÁLLÁS E102 – áramlás", ["+36301234567", "+36201112222"])
            self.assertEqual([to for to, _ in modem.sent], ["+36301234567", "+36201112222"])
            self.assertEqual(modem.sent[0][1], "(Telep) VESZLEALLAS E102 - aramlas")
            self.assertIn("AT+CMGF=1", modem.commands)
            self.assertIn('AT+CPIN="1234"', modem.commands)
        finally:
            modem.close()
        modem = FakeModem(pin="1234")
        try:
            with self.assertRaises(N.NotifyError) as ctx:
                N.send_gsm({"gsm_port": modem.path, "gsm_baud": 115200, "gsm_pin": ""}, "x", ["+36301234567"])
            self.assertIn("PIN", str(ctx.exception))
        finally:
            modem.close()
        with self.assertRaises(N.NotifyError) as ctx:
            N.send_gsm({"gsm_port": "/dev/nincs-ilyen", "gsm_baud": 115200}, "x", ["+36301234567"])
        self.assertIn("nem található", str(ctx.exception))


class _Events:
    def __init__(self):
        self.items = []
        self.listeners = []

    def add_listener(self, cb):
        self.listeners.append(cb)

    def add(self, level, message, code="", source=""):
        e = {"id": len(self.items) + 1, "ts": time.time(), "level": level, "code": code, "message": message,
             "source": source}
        self.items.append(e)
        for cb in self.listeners:
            cb(e)
        return e


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(os.path.join(self.tmp.name, "config.json"))
        self.store.update({"notifications": {
            "batch_s": 0.3,
            "email": {"enabled": True, "smtp_host": "smtp.example.com", "username": "rig@example.com",
                      "recipients": "op@example.com", "max_per_hour": 3},
            "sms": {"enabled": True, "provider": "http", "recipients": "+36301234567",
                    "http_url": "https://sms.example.com/?to={to}&m={message}"},
            "rules": {"warnings": {"email": True, "sms": False}}}})
        self.events = _Events()
        self.sent = {"email": [], "sms": []}
        self.fail = {"email": 0, "sms": 0}

        def fake_email(cfg, subject, body, timeout=25.0, attachments=None):
            if self.fail["email"]:
                self.fail["email"] -= 1
                raise N.NotifyError("nincs internet")
            self.sent["email"].append((subject, body))
            return "ok"

        def fake_sms(cfg, text, site=""):
            self.sent["sms"].append(text)
            return "ok"

        self.patches = [mock.patch.object(N, "send_email", fake_email), mock.patch.object(N, "send_sms", fake_sms),
                        mock.patch.object(N, "RETRY_DELAYS", (0.2, 0.2))]
        for p in self.patches:
            p.start()
        self.n = self._make()

    def _make(self):
        n = N.Notifier(self.store, self.events, self.tmp.name, status_fn=lambda: ["Rendszer: Üzemel"],
                       url_fn=lambda: "http://pi:8080/")
        n.start()
        return n

    def tearDown(self):
        self.n.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_batching_and_rules(self):
        self.events.add("info", "Rendszer indítása (web)", code="I601", source="web")
        self.events.add("warning", "Hydro-01: Ventilátor hiba", code="W402", source="miner")
        self.events.add("info", "Beállítások módosítva", source="web")  # not notified at all
        self.assertTrue(wait_for(lambda: self.sent["email"] and self.sent["sms"]))
        time.sleep(0.4)
        self.assertEqual(len(self.sent["email"]), 1)              # merged into one email
        subject, body = self.sent["email"][0]
        self.assertIn("Kézi művelet: I601", subject)
        self.assertIn("(+1 további)", subject)
        self.assertIn("Ventilátor hiba", body)
        self.assertIn("Rendszer: Üzemel", body)
        self.assertEqual(len(self.sent["sms"]), 1)                 # warnings are not sent as SMS
        self.assertNotIn("Ventil", self.sent["sms"][0])

    def test_critical_is_immediate_and_not_limited(self):
        self.store.update({"notifications": {"batch_s": 60}})
        self.events.add("error", "ABSZOLÚT VÉSZLEÁLLÁS E001 – KILLSWITCH", code="E001", source="KILLSWITCH")
        self.assertTrue(wait_for(lambda: self.sent["sms"], 2.0))
        self.assertIn("E001", self.sent["sms"][0])
        for i in range(5):  # hourly email limit is 3 - critical messages still go out
            self.events.add("error", "VÉSZLEÁLLÁS E102 – %d" % i, code="E102", source="védelem")
            wait_for(lambda: len(self.sent["email"]) >= i + 2, 2.0)
        self.assertEqual(len(self.sent["email"]), 6)

    def test_dedup_limit_and_own_events(self):
        for _ in range(3):
            self.events.add("warning", "Hydro-01 abbahagyta a termelést", code="W406", source="miner")
        self.assertTrue(wait_for(lambda: self.sent["email"]))
        time.sleep(0.5)
        self.assertEqual(len(self.sent["email"]), 1)               # identical message only once
        self.store.update({"notifications": {"batch_s": 0}})       # every event in its own message
        for i in range(4):
            self.events.add("info", "Rendszer leállítása (web)", code="I602", source="web")
            wait_for(lambda: len(self.sent["email"]) >= min(i + 2, 3), 1.0)
            time.sleep(0.1)
        self.assertTrue(wait_for(lambda: any("W602" == e["code"] for e in self.events.items)))
        self.assertEqual(len(self.sent["email"]), 3)               # max_per_hour = 3
        n_before = len(self.sent["email"])
        self.events.add("warning", "Email értesítés küldése sikertelen", code="W601", source=N.SOURCE)
        time.sleep(0.5)
        self.assertEqual(len(self.sent["email"]), n_before)        # never notifies about itself

    def test_retry_and_persistence(self):
        self.fail["email"] = 1
        self.events.add("error", "GPIO hiba", code="E301", source="felügyelet")
        self.assertTrue(wait_for(lambda: any(e["code"] == "W601" for e in self.events.items)))
        self.assertTrue(wait_for(lambda: self.sent["email"], 3.0))  # delivered on the retry
        self.assertEqual(self.n.status()["outbox"]["email"], 0)
        hist = [h for h in self.n.status()["history"] if h["channel"] == "email"]
        self.assertEqual([h["ok"] for h in hist], [True, False])   # newest first
        self.assertEqual(hist[1]["detail"], "nincs internet")
        # a queued message survives a restart
        self.fail["email"] = 99
        self.events.add("error", "Második hiba", code="E301", source="x")
        self.assertTrue(wait_for(lambda: self.n.status()["outbox"]["email"] == 1))
        self.n.stop()
        self.fail["email"] = 0
        self.n = self._make()
        self.assertTrue(wait_for(lambda: any("Második hiba" in b for _, b in self.sent["email"]), 3.0))

    def test_master_switch(self):
        self.store.update({"notifications": {"enabled": False}})
        self.events.add("error", "ABSZOLÚT VÉSZLEÁLLÁS", code="E001", source="KILLSWITCH")
        time.sleep(0.6)
        self.assertEqual(self.sent, {"email": [], "sms": []})


class ProductionMonitorTest(unittest.TestCase):
    def test_w407_w406_and_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(os.path.join(d, "c.json"))
            store.update({"miners": {"list": [{"id": "a", "name": "Hydro-01", "host": "10.0.0.1",
                                               "type": "simulated"}]}})
            mgr = M.MinerManager(store, _Events(), ChangeSignal(), sim_world=None)
            try:
                rec = mgr._records["a"]
                check = lambda t: mgr._check_production(rec, t)  # noqa: E731
                self.assertEqual(check(10.0), [])                      # not supposed to work yet
                rec.mark_desired(True, 0.0)
                rec.online, rec.data = True, {"hashrate": 0, "hashrate_ideal": 335e12, "errors": []}
                self.assertEqual(check(899.0), [])
                out = check(901.0)
                self.assertEqual(out[0][2], "W407")
                self.assertIn("15 perccel sem termel", out[0][1])
                self.assertEqual(check(950.0), [])                     # reported once
                rec.data = {"hashrate": 330e12, "hashrate_ideal": 335e12, "errors": []}
                out = check(960.0)
                self.assertEqual(out[0][2], "I606")
                rec.data = {"hashrate": 20e12, "hashrate_ideal": 335e12, "errors": []}  # < 10 % of nominal
                self.assertEqual(check(1000.0), [])
                self.assertEqual(check(1179.0), [])
                out = check(1181.0)
                self.assertEqual(out[0][2], "W406")
                self.assertIn("abbahagyta a termelést", out[0][1])
                self.assertTrue(mgr.snapshot()[0]["prod_alarm"])
                self.assertTrue(mgr.snapshot()[0]["has_errors"])
                rec.mark_desired(False, 1200.0)                        # stopped on purpose
                self.assertEqual(check(1300.0), [])
                self.assertIsNone(mgr.snapshot()[0]["prod_alarm"])
                rec.mark_desired(True, 2000.0)
                rec.data = {"hashrate": 330e12, "hashrate_ideal": 335e12, "errors": []}
                check(2010.0)
                rec.online, rec.last_error = False, "időtúllépés"
                check(2020.0)
                out = check(2201.0)
                self.assertEqual(out[0][2], "W406")
                self.assertIn("nem elérhető (időtúllépés)", out[0][1])
            finally:
                mgr.stop()


if __name__ == "__main__":
    unittest.main()
