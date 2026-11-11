"""HTTP server: JSON API, Server-Sent Events live stream and the static UI."""

from __future__ import annotations

import base64
import copy
import gzip
import json
import logging
import mimetypes
import os
import re
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__
from ..config import (ADC_TYPES, GPIO_TO_PIN, IO_INPUTS, IO_LABELS, IO_OUTPUTS, SENSOR_IDS, ConfigError,
                      bus_pins)
from ..errors import CATALOG
from ..miners.registry import create_driver, driver_types
from ..miners.scanner import default_subnet, scan
from .auth import hash_password, verify_password

log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 1024 * 1024
MAX_SSE_CLIENTS = 24
GZIP_MIN = 1500
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
         ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json",
         ".webmanifest": "application/manifest+json", ".woff2": "font/woff2"}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def masked_config(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    out["web"]["has_password"] = bool(out["web"].pop("password_hash", ""))
    for m in out["miners"]["list"]:
        m["has_password"] = bool(m.get("password"))
        m["password"] = None
    return out


def config_meta() -> dict:
    return {
        "driver_types": driver_types(),
        "adc_types": [{"id": k, "label": v["label"], "channels": v["channels"]} for k, v in ADC_TYPES.items()],
        "gpio_to_pin": {str(k): v for k, v in GPIO_TO_PIN.items()},
        "io_labels": IO_LABELS,
        "io_outputs": list(IO_OUTPUTS),
        "io_inputs": list(IO_INPUTS),
        "sensor_ids": list(SENSOR_IDS),
        "error_catalog": CATALOG,
    }


class _Gzip:
    _cache = {}
    _lock = threading.Lock()

    @classmethod
    def static(cls, path: str, mtime: float, data: bytes) -> bytes:
        key = (path, mtime)
        with cls._lock:
            hit = cls._cache.get(key)
            if hit is None:
                hit = gzip.compress(data, 6)
                cls._cache = {k: v for k, v in cls._cache.items() if k[0] != path}
                cls._cache[key] = hit
            return hit


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "RigSupervisor/" + __version__
        timeout = 30

        # ------------------------------------------------------------ helpers
        def log_message(self, fmt, *args):  # noqa: N802 - stdlib name
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _accepts_gzip(self) -> bool:
            return "gzip" in (self.headers.get("Accept-Encoding") or "")

        def _send(self, status: int, body: bytes, ctype: str, headers=None, compress=True):
            if compress and len(body) >= GZIP_MIN and self._accepts_gzip():
                body = gzip.compress(body, 5)
                headers = dict(headers or {})
                headers["Content-Encoding"] = "gzip"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if "Cache-Control" not in (headers or {}):
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _read_json(self) -> dict:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise HttpError(415, "JSON tartalom szükséges")
            origin = self.headers.get("Origin")
            if origin:
                host = urllib.parse.urlparse(origin).netloc
                if host and host != self.headers.get("Host"):
                    raise HttpError(403, "Idegen eredetű kérés elutasítva")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise HttpError(413, "Túl nagy kérés")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                raise HttpError(400, "Érvénytelen JSON")
            if not isinstance(data, dict):
                raise HttpError(400, "JSON objektum szükséges")
            return data

        def _check_auth(self) -> bool:
            web = app.config.data["web"]
            if not web["auth_enabled"] or not web["password_hash"]:
                return True
            header = self.headers.get("Authorization") or ""
            if header and header in app.auth_cache:
                return True
            if header.startswith("Basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                except (ValueError, UnicodeDecodeError):
                    user, pw = "", ""
                if user == web["username"] and verify_password(pw, web["password_hash"]):
                    if len(app.auth_cache) > 64:
                        app.auth_cache.clear()
                    app.auth_cache.add(header)
                    return True
                time.sleep(1.0)  # slow down password guessing
            body = "Bejelentkezés szükséges".encode("utf-8")
            self._send(401, body, "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="Rig Supervisor", charset="UTF-8"'}, compress=False)
            return False

        # ------------------------------------------------------------ routing
        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str):
            try:
                url = urllib.parse.urlparse(self.path)
                path = url.path
                query = dict(urllib.parse.parse_qsl(url.query))
                if path == "/api/health":
                    return self._json({"ok": True, "version": __version__})
                if not self._check_auth():
                    return
                if path.startswith("/api/"):
                    return self._api(method, path, query)
                if method != "GET":
                    raise HttpError(405, "Nem támogatott metódus")
                return self._static(path)
            except HttpError as exc:
                self._json({"ok": False, "error": exc.message}, exc.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # noqa: BLE001
                log.exception("Webes kéréskezelési hiba: %s %s", method, self.path)
                try:
                    self._json({"ok": False, "error": "Belső hiba: %s" % exc}, 500)
                except OSError:
                    pass

        def _static(self, path: str):
            if path in ("", "/"):
                path = "/index.html"
            rel = os.path.normpath(urllib.parse.unquote(path)).lstrip("/\\")
            full = os.path.join(STATIC_DIR, rel)
            if not os.path.abspath(full).startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
                # single page app: unknown non-asset paths serve the index
                if "." not in os.path.basename(rel):
                    full = os.path.join(STATIC_DIR, "index.html")
                else:
                    raise HttpError(404, "Nem található")
            st = os.stat(full)
            etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
            headers = {"ETag": etag, "Cache-Control": "no-cache"}
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ext = os.path.splitext(full)[1].lower()
            ctype = TYPES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
            if len(data) >= GZIP_MIN and self._accepts_gzip() and ext in (".html", ".css", ".js", ".svg", ".json"):
                data = _Gzip.static(full, st.st_mtime, data)
                headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
            self._send(200, data, ctype, headers, compress=False)

        def _api(self, method: str, path: str, query: dict):
            parts = [p for p in path.split("/") if p][1:]  # strip "api"
            key = (method, "/".join(parts))
            handler = ROUTES.get(key)
            if handler is not None:
                return handler(self, query)
            m = re.match(r"^miners/([A-Za-z0-9_-]+)/(start|stop|reboot)$", "/".join(parts))
            if method == "POST" and m:
                self._read_json()
                if m.group(2) in ("start", "reboot") and app.controller.state != "running":
                    raise HttpError(409, "Miner csak üzemelő rendszernél (járó szivattyúk mellett) indítható vagy "
                                         "indítható újra")
                ok, msg = app.miners.command_one(m.group(1), m.group(2), source="web")
                return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
            raise HttpError(404, "Ismeretlen API végpont")

        # ------------------------------------------------------------ handlers
        def h_state(self, query):
            self._json(app.live_state())

        def h_stream(self, query):
            with app.sse_lock:
                if app.sse_clients >= MAX_SSE_CLIENTS:
                    raise HttpError(503, "Túl sok egyidejű kapcsolat")
                app.sse_clients += 1
            try:
                self._stream()
            finally:
                with app.sse_lock:
                    app.sse_clients -= 1

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(20)
            version = -1
            last_sent = 0.0
            last_miners = last_events = None
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            try:
                while not app.stopping:
                    new_version = app.signal.wait(version, 1.0)
                    now = time.monotonic()
                    if new_version == version and now - last_sent < 1.0:
                        continue
                    if now - last_sent < 0.25:
                        time.sleep(0.25 - (now - last_sent))
                    version = app.signal.version
                    state = app.live_state()
                    miners_json = json.dumps(state["miners"], separators=(",", ":"), default=str)
                    events_json = json.dumps(state["events"], separators=(",", ":"), default=str)
                    if miners_json == last_miners and now - last_sent < 15:
                        del state["miners"]
                        del state["summary"]
                    if events_json == last_events:
                        del state["events"]
                    last_miners, last_events = miners_json, events_json
                    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"), default=str)
                    self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
                    self.wfile.flush()
                    last_sent = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                pass

        def h_start(self, query):
            self._read_json()
            ok, msg = app.controller.request_start("web")
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)

        def h_stop(self, query):
            self._read_json()
            ok, msg = app.controller.request_stop("web")
            self._json({"ok": ok, "message": msg})

        def h_ack(self, query):
            self._read_json()
            ok, msg = app.controller.acknowledge("web")
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)

        def h_manual(self, query):
            data = self._read_json()
            ok, msg = app.controller.manual_pump(str(data.get("pump")), str(data.get("action")), "web")
            self._json({"ok": ok, "message": msg}, 200 if ok else 409)

        def h_config_get(self, query):
            self._json({"ok": True, "config": masked_config(app.config.data), "meta": config_meta(),
                        "sim": app.simulated, "version": __version__,
                        "bus_pins": {str(k): v for k, v in bus_pins(app.config.data["hardware"]["adc"]).items()}})

        def h_config_post(self, query):
            data = self._read_json()
            patch = data.get("patch")
            if not isinstance(patch, dict):
                raise HttpError(400, "Hiányzó 'patch' objektum")
            patch = copy.deepcopy(patch)
            web = patch.get("web")
            if isinstance(web, dict):
                web.pop("password_hash", None)
                web.pop("has_password", None)
                if web.get("auth_enabled") and not app.config.data["web"]["password_hash"]:
                    raise HttpError(400, "Előbb állíts be jelszót")
            miners = patch.get("miners")
            if isinstance(miners, dict) and isinstance(miners.get("list"), list):
                current = {m["id"]: m for m in app.config.data["miners"]["list"]}
                for m in miners["list"]:
                    if not isinstance(m, dict):
                        continue
                    m.pop("has_password", None)
                    if m.get("password") is None:
                        old = current.get(m.get("id"))
                        if old is not None:
                            m["password"] = old["password"]
                        else:
                            m.pop("password", None)
            try:
                app.config.update(patch)
            except ConfigError as exc:
                return self._json({"ok": False, "error": "Hibás beállítás", "errors": exc.errors}, 400)
            app.events.add("info", "Beállítások módosítva (%s)" % ", ".join(sorted(patch.keys())),
                           source="web")
            app.signal.bump()
            self._json({"ok": True, "config": masked_config(app.config.data),
                        "bus_pins": {str(k): v for k, v in bus_pins(app.config.data["hardware"]["adc"]).items()}})

        def h_password(self, query):
            data = self._read_json()
            enabled = bool(data.get("enabled"))
            patch = {"web": {"auth_enabled": enabled}}
            if enabled:
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "")
                if not username:
                    raise HttpError(400, "Add meg a felhasználónevet")
                if password:
                    if len(password) < 4:
                        raise HttpError(400, "A jelszó legalább 4 karakter legyen")
                    patch["web"]["password_hash"] = hash_password(password)
                elif not app.config.data["web"]["password_hash"]:
                    raise HttpError(400, "Add meg a jelszót")
                patch["web"]["username"] = username
            else:
                patch["web"]["password_hash"] = ""
            try:
                app.config.update(patch)
            except ConfigError as exc:
                return self._json({"ok": False, "error": "; ".join(exc.errors)}, 400)
            app.auth_cache.clear()
            app.events.add("info", "Webes jelszavas védelem %s" % ("bekapcsolva" if enabled else "kikapcsolva"),
                           source="web")
            self._json({"ok": True})

        def h_calibrate(self, query):
            data = self._read_json()
            sid = data.get("sensor")
            if sid not in SENSOR_IDS:
                raise HttpError(400, "Ismeretlen szenzor")
            try:
                reference = float(data.get("reference"))
            except (TypeError, ValueError):
                raise HttpError(400, "Érvénytelen referencia hőmérséklet")
            reading = app.io.sensor_snapshot().get(sid) or {}
            raw = reading.get("raw_value")
            if raw is None:
                raise HttpError(409, "A szenzor jelenleg nem ad érvényes értéket – nem kalibrálható")
            gain = float(app.config.data["sensors"][sid]["gain"])
            offset = round(reference - raw * gain, 2)
            try:
                app.config.update({"sensors": {sid: {"offset": offset}}})
            except ConfigError as exc:
                return self._json({"ok": False, "error": "; ".join(exc.errors)}, 400)
            app.events.add("info", "%s kalibrálva: referencia %.2f °C, új eltolás %+.2f °C" % (
                sid.upper(), reference, offset), source="web")
            self._json({"ok": True, "offset": offset})

        def h_hw(self, query):
            self._json({"ok": True, "pins": app.io.pin_states(), "sensors": app.io.sensor_snapshot(),
                        "hardware": app.io.status(),
                        "bus_pins": {str(k): v for k, v in bus_pins(app.config.data["hardware"]["adc"]).items()}})

        def h_miner_test(self, query):
            data = self._read_json()
            cfg = dict(data.get("miner") or {})
            if not cfg.get("host"):
                raise HttpError(400, "Add meg az IP címet")
            if cfg.get("password") is None and cfg.get("id"):
                for m in app.config.data["miners"]["list"]:
                    if m["id"] == cfg["id"]:
                        cfg["password"] = m["password"]
            cfg.setdefault("id", "test")
            cfg.setdefault("name", cfg["host"])
            cfg.setdefault("type", "antminer")
            try:
                driver = create_driver(cfg, float(app.config.data["miners"]["timeout_s"]), app.sim)
                started = time.monotonic()
                md = driver.poll()
                return self._json({"ok": True, "data": md.to_dict(),
                                   "elapsed_ms": int((time.monotonic() - started) * 1000)})
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": str(exc) or exc.__class__.__name__})

        def h_scan(self, query):
            data = self._read_json()
            subnet = str(data.get("subnet") or default_subnet())
            try:
                port = int(data.get("port") or 4028)
                known = [m["host"] for m in app.config.data["miners"]["list"]]
                results = scan(subnet, port=port, known_hosts=known)
            except ValueError as exc:
                raise HttpError(400, str(exc))
            self._json({"ok": True, "subnet": subnet, "results": results})

        def h_scan_info(self, query):
            self._json({"ok": True, "subnet": default_subnet()})

        def h_history(self, query):
            now = int(time.time())
            try:
                end = int(float(query.get("end") or now))
                start = int(float(query.get("start") or (end - 3600)))
                points = int(query.get("points") or 600)
                result = app.datalog.history(start, end, points)
            except ValueError as exc:
                raise HttpError(400, str(exc))
            order = {m["id"]: i for i, m in enumerate(app.config.data["miners"]["list"])}
            names = {m["id"]: m["name"] for m in app.config.data["miners"]["list"]}
            for m in result["miners"]:
                m["name"] = names.get(m["id"], m["name"] + " (törölt)")
            result["miners"].sort(key=lambda m: (order.get(m["id"], 10 ** 6), m["name"]))
            result["ok"] = True
            self._json(result)

        def h_export(self, query):
            now = int(time.time())
            try:
                end = int(float(query.get("end") or now))
                start = int(float(query.get("start") or (end - 86400)))
            except ValueError:
                raise HttpError(400, "Érvénytelen időtartomány")
            if end - start > 366 * 86400:
                raise HttpError(400, "Legfeljebb 1 év exportálható egyszerre")
            states = {0: "leállítva", 1: "indítás", 2: "üzemel", 3: "leállítás", 4: "vészleállás"}
            lines = ["unix_ido;helyi_ido;allapot;T1_C;T2_C;T3_C;T4_C;F1;F2;P1;P2;hashrate_THs;teljesitmeny_W;"
                     "minerek_online;max_miner_C"]

            def fmt(v, nd=2):
                return "" if v is None else ("%.*f" % (nd, v)).replace(".", ",")

            for row in app.datalog.export_rows(start, end):
                ts, state, t1, t2, t3, t4, f1, f2, p1, p2, hs, pw, online, mt = row
                lines.append(";".join([
                    str(ts), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), states.get(state, ""),
                    fmt(t1), fmt(t2), fmt(t3), fmt(t4), fmt(f1, 0), fmt(f2, 0), fmt(p1, 0), fmt(p2, 0),
                    fmt(hs / 1e12 if hs is not None else None, 3), fmt(pw, 0), fmt(online, 0), fmt(mt, 1)]))
            body = ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")
            name = "rig-naplo_%s_%s.csv" % (time.strftime("%Y%m%d-%H%M", time.localtime(start)),
                                            time.strftime("%Y%m%d-%H%M", time.localtime(end)))
            self._send(200, body, "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="%s"' % name})

        def h_events(self, query):
            try:
                limit = int(query.get("limit") or 100)
                before = int(query["before"]) if query.get("before") else None
            except ValueError:
                raise HttpError(400, "Érvénytelen paraméter")
            self._json({"ok": True, "events": app.events.query(limit, before, query.get("level"))})

        def h_loginfo(self, query):
            self._json({"ok": True, "info": app.datalog.info()})

        def h_sim(self, query):
            if app.sim is None:
                raise HttpError(404, "Nem szimulációs módban fut a program")
            data = self._read_json()
            action = data.get("action")
            sim = app.sim
            if action == "fault":
                key = data.get("key")
                if key not in sim.faults:
                    raise HttpError(400, "Ismeretlen hiba típus")
                value = data.get("value")
                if key.startswith("flow"):
                    value = bool(value)
                elif value not in (None, "open", "short"):
                    raise HttpError(400, "Érvénytelen érték")
                sim.set_fault(key, value)
            elif action == "button":
                if data.get("key") not in ("btn_start", "btn_stop"):
                    raise HttpError(400, "Ismeretlen gomb")
                sim.press_button(data["key"])
            elif action == "miner":
                m = sim.miners.get(data.get("id"))
                if m is None:
                    raise HttpError(404, "Ismeretlen szimulált miner")
                field = data.get("field")
                if field == "offline":
                    m.offline = bool(data.get("value"))
                elif field == "ignore_stop":
                    m.ignore_stop = bool(data.get("value"))
                elif field == "error":
                    m.injected_error = (str(data.get("value")) or None) if data.get("value") else None
                else:
                    raise HttpError(400, "Ismeretlen mező")
            elif action == "ambient":
                try:
                    sim.t_amb = max(-30.0, min(45.0, float(data.get("value"))))
                except (TypeError, ValueError):
                    raise HttpError(400, "Érvénytelen hőmérséklet")
            elif action != "get":
                raise HttpError(400, "Ismeretlen szimulációs parancs")
            app.signal.bump()
            self._json({"ok": True, "sim": sim.state()})

    ROUTES = {
        ("GET", "state"): Handler.h_state,
        ("GET", "stream"): Handler.h_stream,
        ("POST", "control/start"): Handler.h_start,
        ("POST", "control/stop"): Handler.h_stop,
        ("POST", "control/ack"): Handler.h_ack,
        ("POST", "control/manual"): Handler.h_manual,
        ("GET", "config"): Handler.h_config_get,
        ("POST", "config"): Handler.h_config_post,
        ("POST", "password"): Handler.h_password,
        ("POST", "calibrate"): Handler.h_calibrate,
        ("GET", "hw"): Handler.h_hw,
        ("POST", "miners/test"): Handler.h_miner_test,
        ("POST", "miners/scan"): Handler.h_scan,
        ("GET", "miners/scan"): Handler.h_scan_info,
        ("GET", "history"): Handler.h_history,
        ("GET", "export.csv"): Handler.h_export,
        ("GET", "events"): Handler.h_events,
        ("GET", "loginfo"): Handler.h_loginfo,
        ("POST", "sim"): Handler.h_sim,
    }
    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class WebServer:
    def __init__(self, app, host: str, port: int):
        self.httpd = _Server((host, port), make_handler(app))
        self.port = self.httpd.server_address[1]
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
