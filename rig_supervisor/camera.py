"""Webcam snapshots of the rig when the KILLSWITCH trips.

When the KILLSWITCH activates, pictures are taken 0, 1, 3 and 5 seconds after the
activation and attached to the KILLSWITCH email (see notify.py).

No Python packages are needed: the camera is read through a command line tool
that writes an MJPEG stream (a series of complete JPEG pictures) to its stdout:

  * USB webcam (V4L2, /dev/videoN): ``ffmpeg`` - with MJPEG cameras the frames
    are only copied, not re-encoded, so it costs almost no CPU on a Pi 2,
  * Raspberry Pi camera module: ``rpicam-vid`` / ``libcamera-vid``,
  * without ffmpeg: ``fswebcam`` single shots (timing less exact).

In "standby" mode the stream runs all the time at a low frame rate, so the
0 s picture really shows the moment of the activation; otherwise the camera is
started on demand (the first picture then comes ~0.5-1.5 s late).
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

log = logging.getLogger(__name__)

OFFSETS = (0.0, 1.0, 3.0, 5.0)
FPS = 5
RESOLUTIONS = ("640x480", "1280x720", "1920x1080")
KEEP_FILES = 120                 # ~30 KILLSWITCH events
FIRST_FRAME_TIMEOUT = 12.0
SERIES_GRACE = 8.0               # a series may finish this much after its last offset
VIDIOC_QUERYCAP = 0x80685600     # _IOR('V', 0, struct v4l2_capability) - 104 bytes, same on 32/64 bit
CAP_VIDEO_CAPTURE = 0x00000001
CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
CAP_M2M = 0x00008000 | 0x00004000
CAP_DEVICE_CAPS = 0x80000000
CSI_DRIVERS = ("unicam", "rp1-cfe")              # Pi camera behind libcamera
SKIP_DRIVERS = CSI_DRIVERS + ("bcm2835-isp", "pispbe", "bcm2835-codec", "rpivid", "hevc")
_SCAN_END = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")


class CameraError(Exception):
    pass


# --------------------------------------------------------------- detection
def _video_index(path: str) -> int:
    m = re.search(r"(\d+)$", path)
    return int(m.group(1)) if m else 0


def query_device(path: str):
    """``{"path", "name", "driver", "bus", "capture"}`` of a /dev/videoN node, or None."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(104)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf, True)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = lambda a, b: bytes(buf[a:b]).split(b"\0", 1)[0].decode("utf-8", "replace")  # noqa: E731
    caps = int.from_bytes(buf[84:88], "little")
    if caps & CAP_DEVICE_CAPS:
        caps = int.from_bytes(buf[88:92], "little")
    capture = bool(caps & (CAP_VIDEO_CAPTURE | CAP_VIDEO_CAPTURE_MPLANE)) and not caps & CAP_M2M
    return {"path": path, "driver": text(0, 16), "name": text(16, 48), "bus": text(48, 80), "capture": capture}


def list_devices() -> list:
    """Video capture devices usable with ffmpeg (USB webcams, legacy Pi camera driver)."""
    out = []
    for path in sorted(glob.glob("/dev/video*"), key=_video_index):
        info = query_device(path)
        if info and info["capture"] and info["driver"] not in SKIP_DRIVERS:
            out.append(info)
    return out


def csi_camera_present() -> bool:
    for path in glob.glob("/dev/video*"):
        info = query_device(path)
        if info and info["driver"] in CSI_DRIVERS:
            return True
    return False


def tools() -> dict:
    return {name: shutil.which(name) for name in ("ffmpeg", "rpicam-vid", "libcamera-vid", "fswebcam")}


# ------------------------------------------------------------ MJPEG stream
class MjpegSplitter:
    """Cuts an MJPEG byte stream into complete JPEG pictures."""

    def __init__(self, max_buffer: int = 8 * 1024 * 1024):
        self.buf = bytearray()
        self.max_buffer = max_buffer

    def feed(self, data: bytes) -> list:
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(b"\xff\xd8")
            if start < 0:
                del self.buf[:max(0, len(self.buf) - 1)]
                break
            if start:
                del self.buf[:start]
            end = self._end(self.buf)
            if end is None:
                if len(self.buf) > self.max_buffer:
                    del self.buf[:2]  # garbage - resynchronise
                    continue
                break
            if end < 0:
                del self.buf[:2]
                continue
            frames.append(bytes(self.buf[:end]))
            del self.buf[:end]
        return frames

    @staticmethod
    def _end(buf) -> "int | None":
        """End offset of the JPEG starting at 0; None = incomplete, -1 = corrupt."""
        i, n = 2, len(buf)
        while True:
            if i + 2 > n:
                return None
            if buf[i] != 0xFF:
                return -1
            marker = buf[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker == 0xD9:
                return i + 2
            if 0xD0 <= marker <= 0xD7 or marker == 0x01:
                i += 2
                continue
            if i + 4 > n:
                return None
            seglen = (buf[i + 2] << 8) | buf[i + 3]
            if seglen < 2:
                return -1
            i += 2 + seglen
            if marker == 0xDA:  # start of scan: entropy coded data until the next real marker
                m = _SCAN_END.search(buf, i)
                if m is None:
                    return None
                i = m.start()


def stream_commands(source: dict, resolution: str) -> list:
    """Candidate command lines (tried in order) that write an MJPEG stream to stdout."""
    w, h = resolution.split("x")
    t = tools()
    if source["kind"] == "picam":
        exe = t["rpicam-vid"] or t["libcamera-vid"]
        if not exe:
            return []
        return [[exe, "-t", "0", "-n", "--codec", "mjpeg", "--framerate", str(FPS), "--width", w, "--height", h,
                 "--quality", "85", "-o", "-"]]
    if not t["ffmpeg"]:
        return []
    base = [t["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "v4l2"]
    return [
        # MJPEG camera: the frames are copied as they are (almost no CPU)
        base + ["-input_format", "mjpeg", "-framerate", str(FPS), "-video_size", resolution, "-i", source["path"],
                "-c:v", "copy", "-bsf:v", "mjpeg2jpeg", "-f", "mjpeg", "-"],
        # raw (YUYV) camera: encoded to JPEG at a low frame rate
        base + ["-video_size", resolution, "-i", source["path"], "-vf", "fps=%d" % FPS, "-q:v", "4", "-f", "mjpeg", "-"],
    ]


class FrameStream:
    """Runs a capture command and calls ``on_frame(jpeg, mono)`` for every picture."""

    def __init__(self, commands: list, on_frame, on_exit=None):
        self.commands = commands
        self.on_frame = on_frame
        self.on_exit = on_exit
        self.proc = None
        self.error = ""
        self.frames = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def running(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(2)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except OSError:
                    pass

    def _run(self) -> None:
        try:
            for cmd in self.commands:
                if self._stop.is_set():
                    return
                got = self._run_one(cmd)
                if got or self._stop.is_set():
                    return  # it worked (and ended later) - no fallback to a worse variant
        finally:
            if self.on_exit:
                self.on_exit(self)

    def _run_one(self, cmd: list) -> bool:
        try:
            self.proc = proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            self.error = "%s nem indítható: %s" % (os.path.basename(cmd[0]), exc)
            return False
        err_chunks = []
        def read_err():
            with proc.stderr:
                err_chunks.append(proc.stderr.read())
        err_thread = threading.Thread(target=read_err, daemon=True)
        err_thread.start()
        splitter = MjpegSplitter()
        got = False
        fd = proc.stdout.fileno()
        while not self._stop.is_set():
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            for jpeg in splitter.feed(data):
                got = True
                self.frames += 1
                try:
                    self.on_frame(jpeg, time.monotonic())
                except Exception:  # noqa: BLE001
                    log.exception("Kamera kép feldolgozási hiba")
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(3)
        proc.stdout.close()
        err_thread.join(1.0)
        if not self._stop.is_set():
            text = b"".join(c for c in err_chunks if c).decode("utf-8", "replace").strip()
            last = text.splitlines()[-1] if text else "kilépési kód %s" % proc.returncode
            self.error = "%s: %s" % (os.path.basename(cmd[0]), last[:200])
        return got


# ------------------------------------------------------------------ series
class Series:
    """Pictures of one KILLSWITCH event at fixed offsets after ``t0``."""

    def __init__(self, tag: str, t0_mono: float, t0_wall: float, offsets=OFFSETS):
        self.id = uuid.uuid4().hex[:10]
        self.tag = tag
        self.t0 = t0_mono
        self.t0_wall = t0_wall
        self.offsets = tuple(offsets)
        self.deadline = t0_mono + max(self.offsets) + SERIES_GRACE
        self.shots = {}          # offset -> {"offset", "path", "actual", "file"}
        self.error = ""
        self.done = threading.Event()
        self.callbacks = []

    def pending(self) -> list:
        return [o for o in self.offsets if o not in self.shots]

    def result(self) -> list:
        return [self.shots[o] for o in self.offsets if o in self.shots]


class Camera:
    def __init__(self, cfg_store, data_dir: str, events=None):
        self.cfg_store = cfg_store
        self.dir = os.path.join(data_dir, "snapshots")
        self.events = events
        self._lock = threading.RLock()
        self._stream = None
        self._stream_key = None
        self._latest = None       # (jpeg, mono)
        self._series = []
        self._waiters = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.last_error = ""
        self.last_series = None
        self._next_try = 0.0
        self._fail_delay = 5.0

    # ---------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="camera-standby", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._stop_stream()

    def config_changed(self) -> None:
        with self._lock:
            self._next_try, self._fail_delay = 0.0, 5.0
            if self._stream is not None and self._stream_key != self._key():
                self._stop_stream()
        self._wake.set()

    @property
    def cfg(self) -> dict:
        return self.cfg_store.data["camera"]

    def source(self):
        """The camera to use: {"kind": "v4l2"|"picam", "path", "name"} or None."""
        dev = self.cfg["device"]
        if dev == "picam":
            return {"kind": "picam", "path": "", "name": "Raspberry Pi kamera"}
        devices = list_devices()
        if dev != "auto":
            for d in devices:
                if d["path"] == dev:
                    return {"kind": "v4l2", "path": dev, "name": d["name"]}
            return {"kind": "v4l2", "path": dev, "name": dev} if os.path.exists(dev) else None
        if devices:
            d = devices[0]
            return {"kind": "v4l2", "path": d["path"], "name": d["name"]}
        if csi_camera_present() and (shutil.which("rpicam-vid") or shutil.which("libcamera-vid")):
            return {"kind": "picam", "path": "", "name": "Raspberry Pi kamera"}
        return None

    def _key(self):
        return (self.cfg["device"], self.cfg["resolution"])

    # ------------------------------------------------------------- stream
    def _start_stream(self) -> bool:
        """Start the capture process if not running. Lock held."""
        if self._stream is not None and self._stream.running():
            return True
        if self._stop.is_set():
            return False
        src = self.source()
        if src is None:
            self.last_error = "nincs csatlakoztatott kamera"
            return False
        cmds = stream_commands(src, self.cfg["resolution"])
        if not cmds and src["kind"] == "v4l2" and shutil.which("fswebcam"):
            return False  # single shot fallback (see _fswebcam_series)
        if not cmds:
            self.last_error = ("hiányzik a kamera program: sudo apt install ffmpeg" if src["kind"] == "v4l2"
                               else "hiányzik az rpicam-vid / libcamera-vid program")
            return False
        self._latest = None
        self._stream = FrameStream(cmds, self._on_frame, self._on_exit)
        self._stream_key = self._key()
        self._stream.start()
        return True

    def _stop_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream = None

    def _on_exit(self, stream) -> None:
        with self._lock:
            if stream.error:
                self.last_error = stream.error
                log.warning("Kamera leállt: %s", stream.error)
            if self._stream is stream:
                self._stream = None
            for s in list(self._series):
                if not s.done.is_set():
                    s.error = s.error or (stream.error or "a kamera kép nélkül leállt")
        self._wake.set()

    def _on_frame(self, jpeg: bytes, mono: float) -> None:
        with self._lock:
            self._latest = (jpeg, mono)
            self.last_error = ""
            self._fail_delay = 5.0
            for s in list(self._series):
                due = [off for off in s.pending() if mono >= s.t0 + off - 0.05]
                if due:  # one frame fills one picture only: a late first frame does not cover two offsets
                    self._save(s, due[0], jpeg, mono)
                if not s.pending():
                    self._finish(s)
            waiters, self._waiters = self._waiters, []
        for w in waiters:
            w(jpeg)

    # ------------------------------------------------------------- series
    def capture_series(self, tag: str, t0_mono=None, t0_wall=None, offsets=OFFSETS, callback=None):
        """Start a picture series; returns the Series or None when the camera is off / missing."""
        if not self.cfg["enabled"]:
            return None
        t0_mono = time.monotonic() if t0_mono is None else t0_mono
        s = Series(tag, t0_mono, time.time() if t0_wall is None else t0_wall, offsets)
        if callback:
            s.callbacks.append(callback)
        with self._lock:
            latest = self._latest
            if latest is not None and 0.0 in s.offsets and t0_mono - latest[1] < 1.5 / FPS + 0.1:
                self._save(s, 0.0, latest[0], latest[1])   # standby: the frame of the activation moment
            self._series.append(s)
            started = self._start_stream()
        if not started:
            if self.source() and shutil.which("fswebcam") and not shutil.which("ffmpeg"):
                threading.Thread(target=self._fswebcam_series, args=(s,), daemon=True).start()
            else:
                with self._lock:
                    s.error = self.last_error or "a kamera nem indítható"
                    self._finish(s)
        self._wake.set()
        return s

    def _save(self, s: Series, off: float, jpeg: bytes, mono: float) -> None:
        os.makedirs(self.dir, exist_ok=True)
        actual = mono - s.t0
        name = "%s_%s_%s_%gs.jpg" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(s.t0_wall)), s.tag, s.id, off)
        path = os.path.join(self.dir, name)
        try:
            with open(path, "wb") as f:
                f.write(jpeg)
        except OSError as exc:
            s.error = "a kép nem menthető: %s" % exc
            s.shots[off] = {"offset": off, "path": None, "file": None, "actual": actual}
            return
        s.shots[off] = {"offset": off, "path": path, "file": name, "actual": actual}

    def _finish(self, s: Series) -> None:
        """Lock held."""
        if s.done.is_set():
            return
        if s in self._series:
            self._series.remove(s)
        self.last_series = {"tag": s.tag, "ts": s.t0_wall, "error": s.error if s.pending() else "",
                            "shots": [{"offset": x["offset"], "file": x["file"], "actual": round(x["actual"], 2)}
                                      for x in s.result() if x["file"]]}
        s.done.set()
        self._cleanup()
        for cb in s.callbacks:
            threading.Thread(target=cb, args=(s,), daemon=True).start()
        if s.pending() and self.events is not None:  # reported outside the camera lock
            msg = "Webkamera: a KILLSWITCH képek közül %d nem készült el (%s)" % (
                len(s.pending()), s.error or "időtúllépés")
            threading.Thread(target=self.events.add, args=("warning", msg, "W603", "kamera"), daemon=True).start()

    def _fswebcam_series(self, s: Series) -> None:
        src = self.source()
        for off in s.offsets:
            wait = s.t0 + off - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            tmp = os.path.join(self.dir, ".shot-%s.jpg" % s.id)
            os.makedirs(self.dir, exist_ok=True)
            try:
                subprocess.run(["fswebcam", "-q", "-d", src["path"], "-r", self.cfg["resolution"], "--no-banner",
                                "-S", "2", "--jpeg", "85", tmp], timeout=10, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                with open(tmp, "rb") as f:
                    data = f.read()
                os.unlink(tmp)
                with self._lock:
                    self._save(s, off, data, time.monotonic())
            except (OSError, subprocess.SubprocessError) as exc:
                s.error = "fswebcam: %s" % exc
        with self._lock:
            self._finish(s)

    def snapshot(self, timeout: float = FIRST_FRAME_TIMEOUT) -> bytes:
        """One picture right now (test button)."""
        got = []
        ev = threading.Event()

        def waiter(jpeg):
            got.append(jpeg)
            ev.set()

        with self._lock:
            latest = self._latest
            if latest is not None and time.monotonic() - latest[1] < 1.0:
                return latest[0]
            self._waiters.append(waiter)
            started = self._start_stream()
        if not started:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
            src = self.source()
            if src and src["kind"] == "v4l2" and shutil.which("fswebcam"):
                r = subprocess.run(["fswebcam", "-q", "-d", src["path"], "-r", self.cfg["resolution"], "--no-banner",
                                    "-S", "2", "--jpeg", "85", "-"], timeout=15, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
                if r.returncode == 0 and r.stdout:
                    return r.stdout
                raise CameraError("fswebcam: %s" % r.stderr.decode("utf-8", "replace").strip()[-200:])
            raise CameraError(self.last_error or "a kamera nem indítható")
        self._wake.set()
        if not ev.wait(timeout):
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
            raise CameraError(self.last_error or "a kamera %d mp alatt nem adott képet" % timeout)
        return got[0]

    # --------------------------------------------------------- background
    def _loop(self) -> None:
        idle_since = None
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            now = time.monotonic()
            with self._lock:
                for s in list(self._series):
                    if now >= s.deadline:
                        s.error = s.error or self.last_error or "időtúllépés"
                        self._finish(s)
                busy = bool(self._series) or bool(self._waiters)
                cfg = self.cfg
                want = cfg["enabled"] and (cfg["standby"] or busy)
                running = self._stream is not None and self._stream.running()
                if running and self._stream_key != self._key():
                    self._stop_stream()
                    running = False
                if want and not running and (busy or now >= self._next_try):
                    if not self._start_stream() or self._stream is None:
                        self._next_try = now + self._fail_delay
                        self._fail_delay = min(self._fail_delay * 2, 120.0)
                    idle_since = None
                elif running and not want:
                    idle_since = idle_since or now
                    if now - idle_since > 3.0:
                        self._stop_stream()
                        idle_since = None
                else:
                    idle_since = None
                if running and not cfg["enabled"]:
                    self._stop_stream()

    def _cleanup(self) -> None:
        try:
            files = sorted(f for f in os.listdir(self.dir) if f.endswith(".jpg"))
        except OSError:
            return
        for f in files[:-KEEP_FILES]:
            try:
                os.unlink(os.path.join(self.dir, f))
            except OSError:
                pass

    def status(self) -> dict:
        with self._lock:
            running = self._stream is not None and self._stream.running()
            src = None
            try:
                src = self.source()
            except Exception:  # noqa: BLE001
                pass
            return {"enabled": self.cfg["enabled"], "source": src, "devices": list_devices(), "tools": tools(),
                    "running": running, "frames": self._stream.frames if running else 0,
                    "error": "" if running and self._latest else self.last_error,
                    "last_series": self.last_series}

    def recent(self, limit: int = 12) -> list:
        try:
            files = sorted((f for f in os.listdir(self.dir) if f.endswith(".jpg")), reverse=True)
        except OSError:
            return []
        return files[:limit]

    def file_path(self, name: str):
        if not re.match(r"^[0-9A-Za-z_.+-]+\.jpg$", name):
            return None
        path = os.path.join(self.dir, name)
        return path if os.path.isfile(path) else None
