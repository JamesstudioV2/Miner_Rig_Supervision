"""Automatic notifications: email (SMTP) and SMS (GSM modem / Twilio / HTTP gateway).

Every notification is derived from an event of the event log, so what is sent is
always consistent with what the event log shows:

  * the event code decides the category (KILLSWITCH, emergency stop, manual
    start/stop, miner stopped producing, other errors, warnings, restart),
  * the rules matrix (category x channel) decides where it goes,
  * events arriving close to each other are merged into one message
    (``batch_s``), KILLSWITCH / emergency stop messages go out immediately,
  * identical messages are not repeated within 10 minutes, non-critical
    messages are limited per hour (SMS costs money),
  * failed sends are retried with growing delays (up to ~2 hours); the queue
    is stored on disk, so a power loss does not lose a pending notification.

Sending runs on its own threads - the safety controller is never blocked.
"""

from __future__ import annotations

import base64
import collections
import email.utils
import json
import logging
import os
import re
import select
import smtplib
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage

from . import errors as E
from .config import http_needs_recipient, split_emails, split_phones
from .util import atomic_write_json, read_json

log = logging.getLogger(__name__)

SOURCE = "értesítő"
CHANNELS = ("email", "sms")
CRITICAL = ("killswitch", "emergency")
# identical messages of these categories are not repeated within DEDUP_S (manual actions always are)
DEDUP_CATEGORIES = ("errors", "warnings", "miner_production")
SEVERITY = ("killswitch", "emergency", "miner_production", "errors", "manual", "restart", "warnings")
CATEGORY_LABELS = {
    "killswitch": "KILLSWITCH",
    "emergency": "VÉSZLEÁLLÁS",
    "miner_production": "MINER TERMELÉS",
    "errors": "HIBA",
    "manual": "Kézi művelet",
    "restart": "Újraindulás",
    "warnings": "Figyelmeztetés",
}
RETRY_DELAYS = (30, 60, 120, 300, 600, 1800, 3600)
DEDUP_S = 600
MAX_OUTBOX = 200
GSM_MAX_LEN = 160
SMS_MAX_LEN = 300
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


class NotifyError(Exception):
    pass


# ---------------------------------------------------------------- classification
def classify(event: dict, notify_recovery: bool = True):
    """Category of an event log entry, or None when it is never notified."""
    code = event.get("code") or ""
    level = event.get("level")
    if code in ("E001", "E002"):
        return "killswitch"
    if code[:2] in ("E1", "E2"):
        return "emergency"
    if code in ("I601", "I602", "I603"):
        return "manual"
    if code in ("W406", "W407"):
        return "miner_production"
    if code == "I606":
        return "miner_production" if notify_recovery else None
    if code in ("I604", "I605"):
        return "restart"
    if level == "error":
        return "errors"
    if level == "warning":
        return "warnings"
    return None


# ---------------------------------------------------------------- text helpers
_ASCII_REPL = {
    "–": "-", "—": "-", "°": "", "…": "...", "„": '"', "”": '"', "“": '"', "’": "'", "‘": "'",
    "→": "->", "≥": ">=", "≤": "<=", "×": "x", "_": "-", "@": "(at)", "[": "(", "]": ")",
    "{": "(", "}": ")", "·": "-", "Δ": "d", "€": "EUR", "‼": "!", "⚠": "!",
}
# characters that are identical in ASCII and in the GSM 03.38 default alphabet
_GSM_SAFE = re.compile(r"[^A-Za-z0-9 .,:;!?+\-()/'\"%=#*&<>]")


def to_ascii(text: str) -> str:
    """Strip accents and every character an SMS / GSM modem could garble."""
    for a, b in _ASCII_REPL.items():
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _GSM_SAFE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clip(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[:max_len - 3].rstrip() + "..."


def local_time(ts: float) -> str:
    return time.strftime("%Y.%m.%d. %H:%M:%S", time.localtime(ts))


def _sort_key(item: dict):
    return (SEVERITY.index(item["category"]), item["ts"])


# for these categories the catalogue text is the best subject; for the others (miner and
# hardware problems) the event message says more, e.g. which miner stopped producing
_CATALOG_SUBJECT = ("killswitch", "emergency", "manual", "restart")


def headline(item: dict) -> str:
    code, msg = item["code"], item["message"]
    if item["category"] in _CATALOG_SUBJECT and code in E.CATALOG:
        return "%s %s" % (code, E.describe(code))
    return msg if not code or code in msg else "%s %s" % (code, msg)


def build_email(site: str, items: list, status: list, url: str):
    """``(subject, body)`` of one email that reports every event of a batch."""
    main = sorted(items, key=_sort_key)[0]
    subject = "[%s] %s: %s" % (site, CATEGORY_LABELS[main["category"]], _clip(headline(main), 110))
    if len(items) > 1:
        subject += " (+%d további)" % (len(items) - 1)
    lines = ["Rig Supervisor értesítés – %s" % site, ""]
    lines.append("Esemény%s:" % ("ek" if len(items) > 1 else ""))
    for it in sorted(items, key=lambda x: x["ts"]):
        lines.append("  %s  %-5s %s" % (local_time(it["ts"]), it["code"] or "-", it["message"]))
        if it.get("source"):
            lines.append("  %s  %-5s (forrás: %s)" % (" " * 21, "", it["source"]))
    if status:
        lines += ["", "Állapot az üzenet készítésekor (%s):" % local_time(time.time())]
        lines += ["  " + s for s in status]
    if url:
        lines += ["", "Webes felület: %s" % url]
    lines += ["", "-- ", "Automatikus üzenet a Rig Supervisor programtól. Az értesítések a Beállítások › Értesítések "
                     "oldalon állíthatók."]
    return subject, "\n".join(lines) + "\n"


def photo_text(series_list) -> str:
    """Lines added to the KILLSWITCH email about the attached webcam pictures."""
    lines = ["", "Webkamera képek (csatolva):"]
    for s in series_list:
        for shot in s.result():
            if shot["file"]:
                lines.append("  +%d mp  %s  (%s)" % (shot["offset"], shot["file"], local_time(s.t0_wall + shot["actual"])))
        if s.pending():
            lines.append("  %d kép nem készült el: %s" % (len(s.pending()), s.error or "időtúllépés"))
    return "\n".join(lines) + "\n"


def build_sms(site: str, items: list, ascii_only: bool, max_len: int) -> str:
    main = sorted(items, key=_sort_key)[0]
    msg = main["message"]
    if main["code"] and main["code"] not in msg:
        msg = "%s %s" % (main["code"], msg)
    text = "[%s] %s (%s)" % (site, msg, time.strftime("%H:%M", time.localtime(main["ts"])))
    if len(items) > 1:
        text += " +%d tovabbi" % (len(items) - 1) if ascii_only else " +%d további" % (len(items) - 1)
    if ascii_only:
        text = to_ascii(text)
    return _clip(text, max_len)


# --------------------------------------------------------------------- email
def send_email(cfg: dict, subject: str, body: str, timeout: float = 25.0, attachments=None) -> str:
    recipients = split_emails(cfg.get("recipients"))
    if not recipients:
        raise NotifyError("nincs email címzett megadva")
    host, port, security = cfg.get("smtp_host"), int(cfg.get("smtp_port") or 587), cfg.get("security", "starttls")
    if not host:
        raise NotifyError("nincs SMTP szerver megadva")
    sender = cfg.get("sender") or cfg.get("username")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="rig-supervisor.local")
    msg.set_content(body)
    attached = 0
    for path in attachments or []:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:  # deleted meanwhile - the email still goes out
            log.warning("Csatolmány nem olvasható (%s): %s", path, exc)
            continue
        msg.add_attachment(data, maintype="image", subtype="jpeg", filename=os.path.basename(path))
        attached += 1
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
        try:
            if cfg.get("username"):
                smtp.login(cfg["username"], cfg.get("password") or "")
            refused = smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
    except smtplib.SMTPAuthenticationError as exc:
        raise NotifyError("SMTP bejelentkezés sikertelen (felhasználónév / jelszó). Gmail esetén alkalmazásjelszó "
                          "kell. Szerver válasza: %s" % _smtp_text(exc))
    except smtplib.SMTPNotSupportedError:
        raise NotifyError("a szerver nem támogatja a választott titkosítást – próbáld SSL-lel (465) vagy STARTTLS-sel "
                          "(587)")
    except smtplib.SMTPRecipientsRefused as exc:
        raise NotifyError("a szerver minden címzettet visszautasított: %s" % ", ".join(exc.recipients))
    except smtplib.SMTPResponseException as exc:
        raise NotifyError("SMTP hiba (%s:%d): %s" % (host, port, _smtp_text(exc)))
    except ssl.SSLError as exc:
        raise NotifyError("TLS hiba (%s:%d): %s – ellenőrizd a portot és a titkosítás típusát" % (host, port, exc))
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError("az SMTP szerver nem érhető el (%s:%d): %s" % (host, port, exc or exc.__class__.__name__))
    if refused:
        raise NotifyError("a szerver visszautasította: %s" % ", ".join(refused))
    return "email elküldve: %s%s" % (", ".join(recipients), " (%d kép csatolva)" % attached if attached else "")


def _smtp_text(exc) -> str:
    err = getattr(exc, "smtp_error", b"")
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return ("%s %s" % (getattr(exc, "smtp_code", ""), err)).strip() or str(exc)


# --------------------------------------------------------------- HTTP helpers
def _http(req, timeout: float):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NotifyError("kapcsolódási hiba (%s): %s" % (urllib.parse.urlsplit(req.full_url).netloc, reason))


def _error_text(body: str) -> str:
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for key in ("message", "error", "detail", "description"):
                if data.get(key):
                    return str(data[key])[:200]
    except ValueError:
        pass
    return re.sub(r"\s+", " ", body)[:200]


def e164(number: str) -> str:
    """Hungarian convenience: 06301234567 -> +36301234567, 36301234567 -> +36301234567."""
    if number.startswith("+"):
        return number
    if number.startswith("06"):
        return "+36" + number[2:]
    if number.startswith("36") and len(number) == 11:
        return "+" + number
    return number


def send_twilio(cfg: dict, text: str, recipients: list, timeout: float = 20.0, base_url: str = TWILIO_URL) -> str:
    sid, token = cfg.get("twilio_sid", ""), cfg.get("twilio_token", "")
    url = base_url.format(sid=urllib.parse.quote(sid))
    auth = base64.b64encode(("%s:%s" % (sid, token)).encode("utf-8")).decode("ascii")
    for to in recipients:
        data = urllib.parse.urlencode({"To": e164(to), "From": cfg.get("twilio_from", ""), "Body": text}).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
        status, body = _http(req, timeout)
        if status not in (200, 201):
            raise NotifyError("Twilio hiba (HTTP %d, %s): %s" % (status, to, _error_text(body)))
    return "SMS elküldve (Twilio): %s" % ", ".join(recipients)


def _fill(template: str, to, text: str, site: str, escape) -> str:
    values = {"to": to or "", "to_plain": re.sub(r"\D", "", to or ""), "message": text, "site": site}
    return re.sub(r"\{(to|to_plain|message|site)\}", lambda m: escape(values[m.group(1)]), template)


def send_http(cfg: dict, text: str, recipients: list, site: str = "", timeout: float = 20.0) -> str:
    url_t, body_t = cfg.get("http_url", ""), cfg.get("http_body", "") or ""
    method = cfg.get("http_method", "GET")
    ctype = cfg.get("http_content_type", "application/json")
    per_recipient = http_needs_recipient(cfg)
    headers = {}
    for line in (cfg.get("http_headers") or "").splitlines():
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip()] = value.strip()
    if ctype == "application/json":
        body_escape = lambda s: json.dumps(s, ensure_ascii=False)[1:-1]  # noqa: E731
    elif ctype == "application/x-www-form-urlencoded":
        body_escape = urllib.parse.quote_plus
    else:
        body_escape = lambda s: s  # noqa: E731
    for to in (recipients if per_recipient else [None]):
        url = _fill(url_t, to, text, site, lambda s: urllib.parse.quote(s, safe=""))
        data = None
        if method == "POST":
            data = _fill(body_t, to, text, site, body_escape).encode("utf-8")
            headers.setdefault("Content-Type", ctype + "; charset=utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        status, body = _http(req, timeout)
        if not 200 <= status < 300:
            raise NotifyError("SMS átjáró hiba (HTTP %d%s): %s" % (status, ", " + to if to else "", _error_text(body)))
    return "SMS elküldve (HTTP átjáró)%s" % (": " + ", ".join(recipients) if per_recipient else "")


# ------------------------------------------------------------------ GSM modem
class SerialPort:
    """Minimal raw serial port on top of termios (no pyserial needed)."""

    def __init__(self, path: str, baud: int):
        import termios

        self._termios = termios
        speed = getattr(termios, "B%d" % baud, None)
        if speed is None:
            raise NotifyError("nem támogatott sebesség: %d" % baud)
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = 0                                                   # iflag
            attrs[1] = 0                                                   # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL       # cflag: 8N1, no modem control
            attrs[3] = 0                                                   # lflag: raw
            attrs[4] = attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except Exception:
            os.close(self.fd)
            raise

    def drain_input(self) -> None:
        while True:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                return
            try:
                if not os.read(self.fd, 4096):
                    return
            except (BlockingIOError, OSError):
                return

    def write(self, data: bytes, timeout: float = 5.0) -> None:
        view = memoryview(data)
        end = time.monotonic() + timeout
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > end:
                    raise NotifyError("a soros port írása nem sikerült (időtúllépés)")
                select.select([], [self.fd], [], 0.5)

    def read_until(self, patterns, timeout: float) -> str:
        buf = b""
        end = time.monotonic() + timeout
        while True:
            text = buf.decode("ascii", "replace")
            if any(p in text for p in patterns):
                return text
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise NotifyError("a GSM modem nem válaszolt időben (utolsó válasz: %r)" % _modem_text(text)[-80:])
            r, _, _ = select.select([self.fd], [], [], remaining)
            if r:
                try:
                    buf += os.read(self.fd, 1024)
                except BlockingIOError:
                    pass
                except OSError as exc:
                    raise NotifyError("soros port olvasási hiba: %s" % exc)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def _modem_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def send_gsm(cfg: dict, text: str, recipients: list, send_timeout: float = 60.0) -> str:
    path, baud = cfg.get("gsm_port", ""), int(cfg.get("gsm_baud") or 115200)
    try:
        port = SerialPort(path, baud)
    except FileNotFoundError:
        raise NotifyError("a GSM modem soros portja nem található: %s" % path)
    except PermissionError:
        raise NotifyError("nincs jogosultság a soros porthoz (%s) – a szolgáltatás felhasználójának a 'dialout' "
                          "csoportban kell lennie" % path)
    except OSError as exc:
        raise NotifyError("a soros port nem nyitható meg (%s): %s" % (path, exc))
    payload = to_ascii(text)[:GSM_MAX_LEN].encode("ascii")

    def cmd(command: str, expect=("OK",), timeout: float = 5.0) -> str:
        port.drain_input()
        port.write((command + "\r").encode("ascii"))
        resp = port.read_until(tuple(expect) + ("ERROR",), timeout)
        if "ERROR" in resp and not any(e in resp for e in expect):
            raise NotifyError("a GSM modem hibát jelzett (%s): %s" % (command.split("=")[0], _modem_text(resp)))
        return resp

    try:
        for attempt in range(3):
            try:
                cmd("AT", timeout=2.0)
                break
            except NotifyError:
                if attempt == 2:
                    raise NotifyError("a GSM modem nem válaszol (%s, %d baud) – ellenőrizd a bekötést, a tápot és a "
                                      "sebességet" % (path, baud))
                time.sleep(0.5)
        cmd("ATE0")
        pin_state = cmd("AT+CPIN?", timeout=5.0)
        if "SIM PIN" in pin_state:
            if not cfg.get("gsm_pin"):
                raise NotifyError("a SIM kártya PIN kódot kér – add meg a beállításokban, vagy kapcsold ki a PIN kérést")
            cmd('AT+CPIN="%s"' % cfg["gsm_pin"], timeout=10.0)
            time.sleep(3.0)
        elif "READY" not in pin_state:
            raise NotifyError("SIM kártya hiba: %s" % _modem_text(pin_state))
        cmd("AT+CMGF=1")
        try:
            cmd('AT+CSCS="GSM"')
        except NotifyError:
            pass
        for to in recipients:
            port.drain_input()
            port.write(('AT+CMGS="%s"\r' % to).encode("ascii"))
            prompt = port.read_until((">", "ERROR"), 10.0)
            if ">" not in prompt:
                raise NotifyError("a modem nem fogadta az SMS-t (%s): %s" % (to, _modem_text(prompt)))
            port.write(payload + b"\x1a")
            resp = port.read_until(("OK", "ERROR"), send_timeout)
            if "+CMGS" not in resp:
                raise NotifyError("SMS küldése sikertelen (%s): %s" % (to, _modem_text(resp)))
    finally:
        port.close()
    return "SMS elküldve (GSM modem): %s" % ", ".join(recipients)


def send_sms(cfg: dict, text: str, site: str = "") -> str:
    recipients = split_phones(cfg.get("recipients"))
    provider = cfg.get("provider")
    if not recipients and not (provider == "http" and not http_needs_recipient(cfg)):
        raise NotifyError("nincs telefonszám megadva")
    if provider == "gsm":
        return send_gsm(cfg, text, recipients)
    if provider == "twilio":
        return send_twilio(cfg, text, recipients)
    if provider == "http":
        return send_http(cfg, text, recipients, site)
    raise NotifyError("ismeretlen SMS szolgáltató: %s" % provider)


# ------------------------------------------------------------------- notifier
class Notifier:
    def __init__(self, config_store, events, data_dir: str, status_fn=None, url_fn=None, site_fn=None, camera=None):
        self.cfg_store = config_store
        self.events = events
        self.status_fn = status_fn or (lambda: [])
        self.url_fn = url_fn or (lambda: "")
        self.site_fn = site_fn or (lambda: self.cfg_store.data["site_name"])
        self.path = os.path.join(data_dir, "notify_outbox.json")
        self._lock = threading.RLock()
        self._pending = []
        self._pending_since = None
        self._urgent = False
        self._outbox = {ch: [] for ch in CHANNELS}
        self._sent = {ch: collections.deque() for ch in CHANNELS}
        self._dedup = {}
        self._limit_noted = {ch: None for ch in CHANNELS}
        self._send_locks = {ch: threading.Lock() for ch in CHANNELS}
        self._wake = threading.Event()
        self._sender_wake = {ch: threading.Event() for ch in CHANNELS}
        self._stop = threading.Event()
        self._threads = []
        self.history = collections.deque(maxlen=60)
        self.camera = camera
        self._photo_series = {}
        self._load_outbox()
        events.add_listener(self.on_event)

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._threads = [threading.Thread(target=self._dispatch_loop, name="notify", daemon=True)]
        self._threads += [threading.Thread(target=self._sender_loop, args=(ch,), name="notify-" + ch, daemon=True)
                          for ch in CHANNELS]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for ev in self._sender_wake.values():
            ev.set()
        # a sender may be in the middle of a slow send - wait a little, never block the shutdown
        for t in self._threads:
            t.join(1.0)
        self._flush_pending()
        self._persist()

    # ------------------------------------------------------------- intake
    def on_event(self, event: dict) -> None:
        """Event log listener - must stay fast, it may run inside the controller lock."""
        if event.get("source") == SOURCE:
            return
        series = None
        if self.camera is not None and event.get("code") in ("E001", "E002"):
            # the pictures are taken from the moment of the activation, whatever the notification settings
            try:
                series = self.camera.capture_series(event["code"], callback=self._photos_ready)
            except Exception:  # noqa: BLE001 - a camera problem must never block the emergency path
                log.exception("Webkamera hiba")
            if series is not None:
                with self._lock:
                    self._photo_series[series.id] = series
                    if len(self._photo_series) > 20:
                        for sid in [k for k, v in self._photo_series.items() if v.done.is_set()][:10]:
                            del self._photo_series[sid]
        cfg = self.cfg_store.data["notifications"]
        if not cfg["enabled"]:
            return
        category = classify(event, cfg["notify_recovery"])
        if category is None:
            return
        channels = [ch for ch in CHANNELS if cfg[ch]["enabled"] and cfg["rules"][category][ch]]
        if not channels:
            return
        mono = time.monotonic()
        with self._lock:
            if category in DEDUP_CATEGORIES:
                key = (event.get("code"), event.get("message"))
                last = self._dedup.get(key)
                if last is not None and mono - last < DEDUP_S:
                    return
                self._dedup[key] = mono
                if len(self._dedup) > 500:
                    self._dedup = {k: t for k, t in self._dedup.items() if mono - t < DEDUP_S}
            self._pending.append({"ts": event.get("ts") or time.time(), "code": event.get("code") or "",
                                  "level": event.get("level"), "message": event.get("message", ""),
                                  "source": event.get("source") or "", "category": category, "channels": channels,
                                  "photos": series.id if series is not None else None})
            if self._pending_since is None:
                self._pending_since = mono
            if category in CRITICAL:
                self._urgent = True
        self._wake.set()

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._dispatch_wait())
            self._wake.clear()
            try:
                self._flush_pending()
            except Exception:  # noqa: BLE001
                log.exception("Értesítés összeállítási hiba")

    def _dispatch_wait(self) -> float:
        """Sleep exactly until the collected batch is due (new events wake the loop anyway)."""
        with self._lock:
            if not self._pending:
                return 5.0
            if self._urgent:
                return 0.0
            batch_s = float(self.cfg_store.data["notifications"]["batch_s"])
            return max(0.0, min(5.0, batch_s - (time.monotonic() - self._pending_since)))

    def _flush_pending(self, force: bool = False) -> None:
        batch_s = float(self.cfg_store.data["notifications"]["batch_s"])
        with self._lock:
            if not self._pending:
                return
            due = force or self._urgent or self._stop.is_set() or \
                time.monotonic() - self._pending_since >= batch_s
            if not due:
                return
            batch, self._pending, self._pending_since, self._urgent = self._pending, [], None, False
        self._enqueue(batch)

    def _enqueue(self, batch: list) -> None:
        cfg = self.cfg_store.data["notifications"]
        site = self.site_fn()
        try:
            status = self.status_fn()
        except Exception:  # noqa: BLE001
            log.exception("Állapot összefoglaló hiba")
            status = []
        url = cfg["site_url"] or self.url_fn()
        for ch in CHANNELS:
            items = [it for it in batch if ch in it["channels"]]
            if not items:
                continue
            msg = {"id": uuid.uuid4().hex[:12], "channel": ch, "created": time.time(), "attempts": 0,
                   "next_try": 0.0, "critical": any(it["category"] in CRITICAL for it in items),
                   "codes": [it["code"] for it in items if it["code"]]}
            if ch == "email":
                msg["subject"], msg["body"] = build_email(site, items, status, url)
                msg["summary"] = msg["subject"]
                ids = [it["photos"] for it in items if it.get("photos")]
                if ids:
                    # the KILLSWITCH email waits for the webcam pictures (at most a few seconds)
                    msg["photo_series"] = ids
                    with self._lock:
                        deadlines = [self._photo_series[i].deadline for i in ids if i in self._photo_series]
                    msg["next_try"] = (max(deadlines) + 2.0) if deadlines else 0.0
            else:
                sms = cfg["sms"]
                msg["text"] = build_sms(site, items, sms["ascii"],
                                        GSM_MAX_LEN if sms["provider"] == "gsm" else SMS_MAX_LEN)
                msg["summary"] = msg["text"]
            with self._lock:
                if msg.get("photo_series"):
                    self._attach_if_ready(msg)
                box = self._outbox[ch]
                box.append(msg)
                while len(box) > MAX_OUTBOX:
                    drop = next((m for m in box if not m["critical"]), box[0])
                    box.remove(drop)
            self._sender_wake[ch].set()
        self._persist()

    # ------------------------------------------------------------- photos
    def _attach_if_ready(self, msg: dict) -> bool:
        """Lock held. Attach the pictures once every series of the message is finished."""
        series = [self._photo_series.get(i) for i in msg["photo_series"]]
        if any(s is not None and not s.done.is_set() for s in series):
            return False
        series = [s for s in series if s is not None]
        msg["attachments"] = [x["path"] for s in series for x in s.result() if x["path"]]
        if series:
            text = photo_text(series)
            body = msg["body"]
            msg["body"] = body.replace("\n-- \n", text + "\n-- \n", 1) if "\n-- \n" in body else body + text
        del msg["photo_series"]
        msg["next_try"] = 0.0
        return True

    def _photos_ready(self, series) -> None:
        changed = False
        with self._lock:
            for m in self._outbox["email"]:
                if series.id in (m.get("photo_series") or ()) and self._attach_if_ready(m):
                    changed = True
        if changed:
            self._persist()
            self._sender_wake["email"].set()

    # ------------------------------------------------------------ sending
    def _sender_loop(self, ch: str) -> None:
        wake = self._sender_wake[ch]
        while not self._stop.is_set():
            wake.wait(self._sender_wait(ch))
            wake.clear()
            while not self._stop.is_set():
                msg = self._next_due(ch)
                if msg is None:
                    break
                try:
                    self._deliver(ch, msg)
                except Exception:  # noqa: BLE001
                    log.exception("Értesítés küldési hiba")

    def _sender_wait(self, ch: str) -> float:
        """Sleep until the earliest retry is due (a new message wakes the sender anyway)."""
        with self._lock:
            if not self._outbox[ch]:
                return 30.0
            first = min(m["next_try"] for m in self._outbox[ch])
        return max(0.0, min(30.0, first - time.monotonic()))

    def _next_due(self, ch: str):
        now = time.monotonic()
        with self._lock:
            due = [m for m in self._outbox[ch] if m["next_try"] <= now]
            if not due:
                return None
            due.sort(key=lambda m: (not m["critical"], m["created"]))
            return due[0]

    def _over_limit(self, ch: str, limit: int) -> bool:
        now = time.monotonic()
        sent = self._sent[ch]
        while sent and now - sent[0] > 3600:
            sent.popleft()
        return len(sent) >= limit

    def _remove(self, ch: str, msg: dict) -> None:
        with self._lock:
            if msg in self._outbox[ch]:
                self._outbox[ch].remove(msg)

    def _deliver(self, ch: str, msg: dict) -> None:
        cfg = self.cfg_store.data["notifications"]
        if not cfg["enabled"] or not cfg[ch]["enabled"]:
            self._remove(ch, msg)
            self._record(ch, msg, False, "kihagyva – a csatorna közben ki lett kapcsolva", final=True)
            self._persist()
            return
        if not msg["critical"] and self._over_limit(ch, int(cfg[ch]["max_per_hour"])):
            self._remove(ch, msg)
            self._record(ch, msg, False, "eldobva – elérte az óránkénti korlátot (%d)" % cfg[ch]["max_per_hour"],
                         final=True)
            noted = self._limit_noted[ch]
            if noted is None or time.monotonic() - noted > 3600:
                self._limit_noted[ch] = time.monotonic()
                self.events.add("warning", "%s értesítések óránkénti korlátja (%d) elérve – a további nem kritikus "
                                           "üzenetek egy óráig elmaradnak" % (ch.upper(), cfg[ch]["max_per_hour"]),
                                code="W602", source=SOURCE)
            self._persist()
            return
        try:
            result = self._send(ch, cfg, msg)
        except Exception as exc:  # noqa: BLE001 - any failure is retried
            text = str(exc) or exc.__class__.__name__
            msg["attempts"] += 1
            final = msg["attempts"] > len(RETRY_DELAYS)
            delay = 0 if final else RETRY_DELAYS[msg["attempts"] - 1]
            if final:
                self._remove(ch, msg)
            else:
                msg["next_try"] = time.monotonic() + delay
            self._record(ch, msg, False, text, final=final)
            if msg["attempts"] == 1 or final:
                self.events.add("warning", "%s értesítés küldése sikertelen%s: %s" % (
                    "Email" if ch == "email" else "SMS",
                    " – végleg feladva" if final else " – újrapróbálás %s mp múlva" % delay, text),
                    code="W601", source=SOURCE)
        else:
            self._remove(ch, msg)
            self._sent[ch].append(time.monotonic())
            self._record(ch, msg, True, result)
        self._persist()

    def _send(self, ch: str, cfg: dict, msg: dict) -> str:
        with self._send_locks[ch]:
            if ch == "email":
                return send_email(cfg["email"], msg["subject"], msg["body"], attachments=msg.get("attachments"))
            return send_sms(cfg["sms"], msg["text"], self.site_fn())

    def _record(self, ch: str, msg: dict, ok: bool, detail: str, final: bool = True, test: bool = False) -> None:
        self.history.appendleft({"ts": time.time(), "channel": ch, "ok": ok, "summary": msg.get("summary", ""),
                                 "detail": detail, "attempt": msg.get("attempts", 0), "final": final, "test": test,
                                 "codes": msg.get("codes", [])})

    # ---------------------------------------------------------------- test
    def send_test(self, channel: str, notif_cfg: dict) -> str:
        site = self.site_fn()
        url = notif_cfg.get("site_url") or self.url_fn()
        item = {"ts": time.time(), "code": "", "level": "info", "category": "manual", "source": "teszt",
                "message": "Teszt értesítés – ha ezt megkaptad, az %s értesítés működik." % (
                    "email" if channel == "email" else "SMS")}
        msg = {"summary": "", "codes": []}
        try:
            if channel == "email":
                subject, body = build_email(site, [item], self.status_fn(), url)
                subject = "[%s] Teszt értesítés" % site
                msg["summary"] = subject
                with self._send_locks["email"]:
                    result = send_email(notif_cfg["email"], subject, body)
            else:
                sms = notif_cfg["sms"]
                text = build_sms(site, [item], sms["ascii"], GSM_MAX_LEN if sms["provider"] == "gsm" else SMS_MAX_LEN)
                msg["summary"] = text
                with self._send_locks["sms"]:
                    result = send_sms(sms, text, site)
        except NotifyError as exc:
            self._record(channel, msg, False, str(exc), test=True)
            raise
        self._record(channel, msg, True, result, test=True)
        return result

    # --------------------------------------------------------- persistence
    def _persist(self) -> None:
        with self._lock:
            data = {ch: [dict(m) for m in self._outbox[ch]] for ch in CHANNELS}
        try:
            if any(data.values()) or os.path.exists(self.path):
                atomic_write_json(self.path, data)
        except OSError as exc:
            log.error("Az értesítési várólista nem menthető: %s", exc)

    def _load_outbox(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            data = read_json(self.path)
            for ch in CHANNELS:
                for m in (data.get(ch) or [])[:MAX_OUTBOX]:
                    if isinstance(m, dict) and m.get("channel") == ch and ("body" in m or "text" in m):
                        m["next_try"] = 0.0
                        m.pop("photo_series", None)  # the camera series did not survive the restart
                        self._outbox[ch].append(m)
        except Exception as exc:  # noqa: BLE001 - a broken queue file must not stop the program
            log.error("Az értesítési várólista nem olvasható: %s", exc)

    def status(self) -> dict:
        mono = time.monotonic()
        with self._lock:
            queue = [{"channel": ch, "summary": m.get("summary", ""), "attempts": m["attempts"],
                      "critical": m["critical"], "created": m["created"],
                      "next_in": max(0, int(round(m["next_try"] - mono)))}
                     for ch in CHANNELS for m in self._outbox[ch]]
            return {"history": list(self.history),
                    "outbox": {ch: len(self._outbox[ch]) for ch in CHANNELS},
                    "queue": queue,
                    "pending": len(self._pending)}
