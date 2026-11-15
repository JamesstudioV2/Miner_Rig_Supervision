"""Webcam pictures for the KILLSWITCH email: MJPEG splitting, picture timing, email attachments."""

import os
import random
import sys
import tempfile
import textwrap
import time
import unittest
from email import message_from_bytes, policy
from unittest import mock

from rig_supervisor import camera as C
from rig_supervisor import notify as N
from rig_supervisor.config import ConfigStore


def jpeg(n: int) -> bytes:
    """A structurally valid baseline JPEG: segments with tricky bytes, stuffed 0xFF and RST markers."""
    app0 = b"\xff\xe0" + (2 + 6).to_bytes(2, "big") + b"\xff\xd9\xff\xd8" + n.to_bytes(2, "big")
    sos = b"\xff\xda" + (2 + 4).to_bytes(2, "big") + b"\x01\x02\x03\x04"
    scan = b"\x12\xff\x00\x34" + bytes([n % 200]) + b"\xff\xd0\x56\xff\x00"
    return b"\xff\xd8" + app0 + sos + scan + b"\xff\xd9"


FAKE_STREAM = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, %r)
    from tests.test_camera import jpeg
    out = sys.stdout.buffer
    n = 0
    while True:
        out.write(b"garbage" if n == 0 else b"")
        out.write(jpeg(n)); out.flush()
        n += 1
        time.sleep(0.05)
''')


class SplitterTest(unittest.TestCase):
    def test_split_random_chunks(self):
        frames = [jpeg(i) for i in range(50)]
        data = b"junk" + b"".join(frames)
        sp = C.MjpegSplitter()
        got, i = [], 0
        rnd = random.Random(1)
        while i < len(data):
            step = rnd.randint(1, 9)
            got += sp.feed(data[i:i + step])
            i += step
        self.assertEqual(got, frames)


class CameraTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ConfigStore(os.path.join(self.tmp.name, "config.json"))
        script = os.path.join(self.tmp.name, "fakecam.py")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(script, "w") as f:
            f.write(FAKE_STREAM % root)
        self.patches = [
            mock.patch.object(C, "stream_commands", lambda src, res: [[sys.executable, script]]),
            mock.patch.object(C.Camera, "source", lambda self: {"kind": "v4l2", "path": "/dev/video0", "name": "teszt"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _series(self, standby):
        self.store.update({"camera": {"standby": standby}})
        cam = C.Camera(self.store, self.tmp.name)
        cam.start()
        try:
            if standby:
                end = time.time() + 10
                while time.time() < end and not cam.status()["frames"]:
                    time.sleep(0.05)
                self.assertTrue(cam.status()["running"])
            s = cam.capture_series("E001", offsets=(0.0, 0.5, 1.0))
            self.assertTrue(s.done.wait(10))
            return cam, s
        finally:
            cam.stop()

    def test_standby_series(self):
        cam, s = self._series(True)
        shots = s.result()
        self.assertEqual([x["offset"] for x in shots], [0.0, 0.5, 1.0])
        self.assertLess(abs(shots[0]["actual"]), 0.2)        # the frame of the activation moment
        for x in shots:
            self.assertGreater(x["actual"], x["offset"] - 0.2)
            self.assertLess(x["actual"], x["offset"] + 0.3)
            with open(x["path"], "rb") as f:
                self.assertTrue(f.read().startswith(b"\xff\xd8"))
        self.assertEqual(len(cam.last_series["shots"]), 3)
        self.assertEqual(cam.recent()[0], shots[-1]["file"])

    def test_on_demand_series(self):
        _, s = self._series(False)
        self.assertEqual(len(s.result()), 3)
        self.assertEqual(len({x["path"] for x in s.result()}), 3)  # three different frames

    def test_disabled_or_missing(self):
        self.store.update({"camera": {"enabled": False}})
        self.assertIsNone(C.Camera(self.store, self.tmp.name).capture_series("E001"))
        self.store.update({"camera": {"enabled": True}})
        with mock.patch.object(C.Camera, "source", lambda self: None):
            cam = C.Camera(self.store, self.tmp.name)
            s = cam.capture_series("E001")
            self.assertTrue(s.done.is_set())
            self.assertIn("nincs csatlakoztatott kamera", s.error)


class _Events:
    def __init__(self):
        self.listeners, self.items = [], []

    def add_listener(self, cb):
        self.listeners.append(cb)

    def add(self, level, message, code="", source=""):
        e = {"id": 1, "ts": time.time(), "level": level, "code": code, "message": message, "source": source}
        self.items.append(e)
        for cb in self.listeners:
            cb(e)


class KillswitchEmailTest(CameraTest):
    def test_killswitch_email_has_pictures_sms_does_not_wait(self):
        self.store.update({"notifications": {
            "email": {"enabled": True, "smtp_host": "x", "username": "a@example.com", "recipients": "b@example.com"},
            "sms": {"enabled": True, "provider": "http", "recipients": "+36301234567",
                    "http_url": "https://sms.example.com/?to={to}&m={message}"}}})
        sent = []

        def fake_email(cfg, subject, body, timeout=25.0, attachments=None):
            sent.append(("email", time.monotonic(), body, list(attachments or [])))
            return "ok"

        def fake_sms(cfg, text, site=""):
            sent.append(("sms", time.monotonic(), text, []))
            return "ok"

        events = _Events()
        cam = C.Camera(self.store, self.tmp.name, events)
        cam.start()
        n = N.Notifier(self.store, events, self.tmp.name, camera=cam)
        n.start()
        try:
            with mock.patch.object(N, "send_email", fake_email), mock.patch.object(N, "send_sms", fake_sms):
                t0 = time.monotonic()
                events.add("error", "ABSZOLÚT VÉSZLEÁLLÁS E001 – KILLSWITCH", code="E001", source="KILLSWITCH")
                end = time.time() + 20
                while time.time() < end and len(sent) < 2:
                    time.sleep(0.05)
        finally:
            n.stop()
            cam.stop()
        kinds = {k: (t, body, att) for k, t, body, att in sent}
        self.assertLess(kinds["sms"][0] - t0, 1.0)              # SMS immediately
        t, body, att = kinds["email"]
        self.assertGreater(t - t0, 4.8)                           # email after the 5 s picture
        self.assertEqual(len(att), 4)
        self.assertIn("Webkamera képek (csatolva)", body)
        for off in ("+0 mp", "+1 mp", "+3 mp", "+5 mp"):
            self.assertIn(off, body)


class AttachmentTest(unittest.TestCase):
    def test_smtp_attachments(self):
        from tests.test_notify import FakeSMTP

        srv = FakeSMTP()
        try:
            with tempfile.TemporaryDirectory() as d:
                paths = []
                for i in range(2):
                    paths.append(os.path.join(d, "kep%d.jpg" % i))
                    with open(paths[-1], "wb") as f:
                        f.write(jpeg(i))
                cfg = {"smtp_host": "127.0.0.1", "smtp_port": srv.port, "security": "none",
                       "username": "rig@example.com", "password": "titok", "recipients": "a@example.com"}
                res = N.send_email(cfg, "KILLSWITCH", "szöveg", attachments=paths + [os.path.join(d, "nincs.jpg")])
            self.assertIn("2 kép csatolva", res)
            msg = message_from_bytes(srv.messages[-1]["data"], policy=policy.default)
            atts = [p for p in msg.iter_attachments()]
            self.assertEqual([a.get_filename() for a in atts], ["kep0.jpg", "kep1.jpg"])
            self.assertEqual(atts[1].get_payload(decode=True), jpeg(1))
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
