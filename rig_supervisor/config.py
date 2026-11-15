"""Configuration: defaults, loading, validation and persistence.

The configuration lives in ``<data_dir>/config.json``.  Every change made on the
web interface is validated, written atomically (the previous version is kept as
``config.json.bak``) and then applied live (GPIO pins, ADC, miners...).

The published ``ConfigStore.data`` dict is never mutated in place - an update
builds a new dict and swaps the reference, so readers may use it without locks
(they must treat it as read-only).
"""

from __future__ import annotations

import copy
import logging
import os
import re
import threading
import time
import uuid

from .util import atomic_write_json, deep_merge, read_json

log = logging.getLogger(__name__)

SENSOR_IDS = ("t1", "t2", "t3", "t4")
IO_OUTPUTS = ("pump1", "pump2")
IO_INPUTS = ("flow1", "flow2", "btn_start", "btn_stop", "killswitch")
IO_ALL = IO_OUTPUTS + IO_INPUTS

IO_LABELS = {
    "pump1": "P1 szivattyú SSR (belső kör)",
    "pump2": "P2 szivattyú SSR (külső kör)",
    "flow1": "F1 áramláskapcsoló (belső kör)",
    "flow2": "F2 áramláskapcsoló (külső kör)",
    "btn_start": "Indító nyomógomb",
    "btn_stop": "Leállító nyomógomb",
    "killswitch": "KILLSWITCH (abszolút vészleállás)",
}

# BCM GPIO number -> physical pin on the 40 pin header (Raspberry Pi 2 and newer)
GPIO_TO_PIN = {
    2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 10: 19, 9: 21, 11: 23, 5: 29,
    6: 31, 13: 33, 19: 35, 26: 37, 14: 8, 15: 10, 18: 12, 23: 16, 24: 18,
    25: 22, 8: 24, 7: 26, 12: 32, 16: 36, 20: 38, 21: 40,
}
VALID_GPIOS = sorted(GPIO_TO_PIN)

ADC_TYPES = {
    "ads1220": {"label": "ADS1220 (SPI, 24 bit, 4 csatorna)", "channels": 4},
    "ads1115": {"label": "ADS1115 (I²C, 16 bit, 4 csatorna)", "channels": 4},
    "mcp3008": {"label": "MCP3008 (SPI, 10 bit, 8 csatorna)", "channels": 8},
    "mcp3208": {"label": "MCP3208 (SPI, 12 bit, 8 csatorna)", "channels": 8},
    "none": {"label": "Nincs ADC (hőmérséklet mérés kikapcsolva)", "channels": 8},
}

MINER_TYPES = ("antminer", "braiins", "vnish", "luxos", "cgminer", "simulated")

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-\.]{0,251}[A-Za-z0-9])?$")

_DEFAULTS = {
    "version": 1,
    "site_name": "Antminer bányatelep",
    "web": {
        "port": 8080,
        "auth_enabled": False,
        "username": "admin",
        "password_hash": "",
    },
    "hardware": {
        # auto: Raspberry Pi-n valódi hardver, máshol szimuláció | rpi | sim
        "backend": "auto",
        "adc": {
            "type": "ads1220",
            "i2c_bus": 1,
            "i2c_address": 0x48,
            "spi_bus": 0,
            "spi_device": 0,
            "vref": 3.3,       # MCP3x08 / ADS1220 külső referencia feszültség (V)
            # ADS1220 referencia: avdd = arányos mérés az AVDD táphoz (az osztó is erről megy) |
            # internal = belső 2,048 V | external = REFP0/REFN0 lábak (feszültség: vref)
            "ref": "avdd",
            "samples": 5,      # mérésenkénti minták száma (medián szűrés)
        },
        "divider": {
            "supply_v": 5.0,       # a feszültségosztó tápja (V) - javasolt: 3.3 V
            "series_ohm": 10000.0  # fix ellenállás (Ω) a táp és a mérőpont között
        },
        "io": {
            "pump1": {"gpio": 17, "active": "high"},
            "pump2": {"gpio": 27, "active": "high"},
            "flow1": {"gpio": 22, "active": "low", "pull": "up", "enabled": True},
            "flow2": {"gpio": 23, "active": "low", "pull": "up", "enabled": True},
            "btn_start": {"gpio": 24, "active": "low", "pull": "up", "enabled": True},
            "btn_stop": {"gpio": 25, "active": "low", "pull": "up", "enabled": True},
            # magas szint = vészleállás; belső lehúzó, így bekötetlen lábon nem old ki
            "killswitch": {"gpio": 5, "active": "high", "pull": "down", "enabled": True},
        },
    },
    "sensors": {
        "t1": {"name": "Belső kör – minerek előtt", "short": "Miner be", "channel": 0,
               "min": 0.0, "max": 50.0},
        "t2": {"name": "Belső kör – minerek után", "short": "Miner ki", "channel": 1,
               "min": 0.0, "max": 60.0},
        "t3": {"name": "Külső kör – radiátor előtt", "short": "Radiátor be", "channel": 2,
               "min": -10.0, "max": 60.0},
        "t4": {"name": "Külső kör – radiátor után", "short": "Radiátor ki", "channel": 3,
               "min": -10.0, "max": 50.0},
    },
    "safety": {
        "miner_start_delay_s": 2.0,      # szivattyúk indítása után ennyivel indulnak a minerek
        "flow_start_timeout_s": 15.0,    # ennyi idő alatt meg kell indulnia az áramlásnak
        "flow_loss_delay_s": 3.0,        # áramlás kimaradás tűrése üzem közben
        "post_stop_pump_s": 30.0,        # leállítás után ennyi ideig keringetnek még a szivattyúk
        "limit_trip_delay_s": 5.0,       # hőfok határérték túllépés tűrése
        "sensor_fault_delay_s": 5.0,     # szenzorhiba tűrése
        "overheat_enabled": True,        # leállítás utáni túlmelegedés elleni védelem
        "overheat_temp_c": 60.0,
        "overheat_hysteresis_c": 5.0,
        "overheat_min_run_s": 60.0,
        "overheat_use_water_temp": True,  # a T2 (minerek utáni) vízhőfokot is figyelje
        "overheat_after_emergency": True,
        "auto_resume": True,             # áramszünet / újraindulás után az előző állapot visszaállítása
        "manual_hold_timeout_s": 1.5,    # kézi (tartásos) szivattyú gomb "élet-jel" időtúllépés
    },
    "miners": {
        "poll_interval_s": 10.0,
        "timeout_s": 4.0,
        # termelés figyelése (W406 / W407): csak akkor, ha a minernek dolgoznia kellene
        "production_loss_delay_s": 180.0,     # ennyi ideig tartó kiesés után "abbahagyta a termelést"
        "production_start_timeout_s": 900.0,  # indítás után ennyi időn belül el kell kezdenie termelni
        "production_min_pct": 10.0,           # a névleges hashrate ennyi %-a alatt nem számít termelésnek
        "list": [],
    },
    "logging": {
        "interval_s": 10.0,
        "max_size_gb": 10.0,
        "min_free_mb": 500.0,
    },
    "camera": {
        "enabled": True,          # KILLSWITCH esetén képek 0, 1, 3, 5 mp-nél (ha van kamera)
        "device": "auto",         # auto | /dev/videoN | picam
        "resolution": "1280x720",
        "standby": True,          # a kamera folyamatosan fut (alacsony képkocka-sebességgel) -> pontos 0 mp-es kép
    },
    "notifications": {
        "enabled": True,
        "site_url": "",          # a levelekben szereplő link (üresen: automatikus)
        "batch_s": 10.0,         # ennyi ideig gyűjti az eseményeket egy üzenetbe (kritikusak azonnal)
        "notify_recovery": True,  # "miner ismét termel" értesítés
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "security": "starttls",   # starttls | ssl | none
            "username": "",
            "password": "",
            "sender": "",
            "recipients": "",
            "max_per_hour": 60,
        },
        "sms": {
            "enabled": False,
            "provider": "gsm",        # gsm (soros GSM modem, AT parancsok) | twilio | http
            "recipients": "",
            "ascii": True,            # ékezetek nélkül (olcsóbb, minden telefon jól mutatja)
            "max_per_hour": 20,
            "gsm_port": "/dev/ttyUSB0",
            "gsm_baud": 115200,
            "gsm_pin": "",
            "twilio_sid": "",
            "twilio_token": "",
            "twilio_from": "",
            "http_method": "GET",
            "http_url": "",
            "http_body": "",
            "http_content_type": "application/json",
            "http_headers": "",
        },
        "rules": {
            "killswitch": {"email": True, "sms": True},
            "emergency": {"email": True, "sms": True},
            "manual": {"email": True, "sms": True},
            "miner_production": {"email": True, "sms": True},
            "errors": {"email": True, "sms": True},
            "warnings": {"email": True, "sms": False},
            "restart": {"email": True, "sms": False},
        },
    },
}

NOTIFY_CATEGORIES = ("killswitch", "emergency", "manual", "miner_production", "errors", "warnings", "restart")
# secret fields: never sent to the browser, an empty (null) value on save keeps the stored one
NOTIFY_SECRETS = (("email", "password"), ("sms", "twilio_token"), ("sms", "http_headers"), ("sms", "gsm_pin"))
UART_DEVICES = ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0")
GSM_BAUDS = (9600, 19200, 38400, 57600, 115200)
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_PHONE_RE = re.compile(r"^\+?[0-9]{6,15}$")


def split_emails(text) -> list:
    return [x.strip() for x in re.split(r"[,;\s]+", str(text or "")) if x.strip()]


def split_phones(text) -> list:
    return [re.sub(r"[\s\-()/.]", "", x) for x in re.split(r"[,;\n]+", str(text or "")) if x.strip()]


def http_needs_recipient(sms: dict) -> bool:
    """An HTTP gateway URL / body without {to} (e.g. a Telegram bot) sends one request with no phone number."""
    return re.search(r"\{to(_plain)?\}", (sms.get("http_url") or "") + (sms.get("http_body") or "")) is not None


def uart_pins(cfg: dict) -> dict:
    """GPIO14/15 are needed when the GSM modem is wired to the Pi's own UART."""
    sms = cfg.get("notifications", {}).get("sms", {})
    if sms.get("enabled") and sms.get("provider") == "gsm" and sms.get("gsm_port") in UART_DEVICES:
        return {14: "UART TXD (GSM modem)", 15: "UART RXD (GSM modem)"}
    return {}


def reserved_pins(cfg: dict) -> dict:
    pins = dict(bus_pins(cfg["hardware"]["adc"]))
    pins.update(uart_pins(cfg))
    return pins

_SENSOR_DEFAULTS = {
    "r25": 10000.0,
    "beta": 3950.0,
    "offset": 0.0,
    "gain": 1.0,
    "limit_enabled": True,
    "stop_on_fault": False,
}
for _sid in SENSOR_IDS:
    for _k, _v in _SENSOR_DEFAULTS.items():
        _DEFAULTS["sensors"][_sid].setdefault(_k, _v)

MINER_DEFAULTS = {
    "id": "",
    "name": "",
    "host": "",
    "type": "antminer",
    "api_port": 4028,
    "web_port": 80,
    "username": "root",
    "password": "root",
    "enabled": True,
}


def default_config() -> dict:
    return copy.deepcopy(_DEFAULTS)


def new_miner_id() -> str:
    return "m" + uuid.uuid4().hex[:8]


def bus_pins(adc_cfg: dict) -> dict:
    """GPIO pins occupied by the ADC bus -> description."""
    t = adc_cfg.get("type")
    if t == "ads1115":
        if int(adc_cfg.get("i2c_bus", 1)) == 1:
            return {2: "I²C SDA (ADC)", 3: "I²C SCL (ADC)"}
        return {}
    if t in ("mcp3008", "mcp3208", "ads1220"):
        bus = int(adc_cfg.get("spi_bus", 0))
        dev = int(adc_cfg.get("spi_device", 0))
        if bus == 0:
            pins = {9: "SPI MISO (ADC)", 10: "SPI MOSI (ADC)", 11: "SPI SCLK (ADC)"}
            pins[8 if dev == 0 else 7] = "SPI CE%d (ADC)" % dev
            return pins
        if bus == 1:
            pins = {19: "SPI1 MISO (ADC)", 20: "SPI1 MOSI (ADC)", 21: "SPI1 SCLK (ADC)"}
            pins[{0: 18, 1: 17, 2: 16}.get(dev, 18)] = "SPI1 CE%d (ADC)" % dev
            return pins
    return {}


class ConfigError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class _Validator:
    def __init__(self):
        self.errors = []

    def err(self, msg):
        self.errors.append(msg)

    def number(self, d, key, lo, hi, label, integer=False):
        v = d.get(key)
        try:
            if isinstance(v, bool):
                raise ValueError
            if isinstance(v, str):
                v = v.strip().replace(",", ".")
            v = int(float(v)) if integer else float(v)
        except (TypeError, ValueError):
            self.err("%s: érvénytelen szám" % label)
            return
        if v != v or not (lo <= v <= hi):  # NaN check too
            self.err("%s: %s és %s között kell lennie" % (label, _fmt(lo), _fmt(hi)))
        d[key] = v

    def boolean(self, d, key, label):
        v = d.get(key)
        if isinstance(v, bool):
            return
        if isinstance(v, str) and v.lower() in ("true", "1", "igen", "on"):
            d[key] = True
        elif isinstance(v, str) and v.lower() in ("false", "0", "nem", "off", ""):
            d[key] = False
        elif isinstance(v, (int, float)):
            d[key] = bool(v)
        else:
            self.err("%s: igen/nem értéket vár" % label)

    def choice(self, d, key, options, label):
        v = d.get(key)
        if v not in options:
            self.err("%s: érvénytelen érték (%s)" % (label, v))

    def text(self, d, key, label, max_len=80, allow_empty=True):
        v = d.get(key)
        if v is None:
            v = ""
        if not isinstance(v, str):
            v = str(v)
        v = v.strip()
        if not allow_empty and not v:
            self.err("%s: nem lehet üres" % label)
        if len(v) > max_len:
            self.err("%s: legfeljebb %d karakter" % (label, max_len))
        d[key] = v


def _fmt(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def validate(cfg: dict):
    """Validate and normalise a full configuration.

    Returns ``(normalised_copy, errors)``; ``errors`` is a list of human readable
    (Hungarian) messages, empty when the configuration is valid.
    """
    cfg = copy.deepcopy(cfg)
    v = _Validator()

    v.text(cfg, "site_name", "Telephely neve", max_len=60)

    web = cfg["web"]
    v.number(web, "port", 1, 65535, "Webes port", integer=True)
    v.boolean(web, "auth_enabled", "Jelszavas védelem")
    v.text(web, "username", "Felhasználónév", max_len=40)
    if not isinstance(web.get("password_hash"), str):
        web["password_hash"] = ""
    if web.get("auth_enabled") and not web.get("password_hash"):
        v.err("Jelszavas védelemhez jelszót kell megadni")

    hw = cfg["hardware"]
    v.choice(hw, "backend", ("auto", "rpi", "sim"), "Hardver mód")
    adc = hw["adc"]
    v.choice(adc, "type", tuple(ADC_TYPES), "ADC típus")
    v.number(adc, "i2c_bus", 0, 10, "I²C busz", integer=True)
    v.number(adc, "i2c_address", 0x48, 0x4B, "I²C cím", integer=True)
    v.number(adc, "spi_bus", 0, 1, "SPI busz", integer=True)
    v.number(adc, "spi_device", 0, 2, "SPI eszköz (CE)", integer=True)
    v.number(adc, "vref", 0.5, 5.5, "ADC referencia feszültség", integer=False)
    v.choice(adc, "ref", ("avdd", "internal", "external"), "ADS1220 referencia")
    v.number(adc, "samples", 1, 32, "Minták száma", integer=True)
    div = hw["divider"]
    v.number(div, "supply_v", 1.0, 5.5, "Osztó tápfeszültség")
    v.number(div, "series_ohm", 100.0, 1000000.0, "Osztó fix ellenállás")

    io = hw["io"]
    used = {}
    for name in IO_ALL:
        item = io.get(name)
        label = IO_LABELS[name]
        if not isinstance(item, dict):
            v.err("%s: hiányzó beállítás" % label)
            continue
        v.number(item, "gpio", 2, 27, label + " GPIO", integer=True)
        v.choice(item, "active", ("high", "low"), label + " aktív szint")
        if name in IO_INPUTS:
            v.choice(item, "pull", ("up", "down", "off"), label + " felhúzó ellenállás")
            v.boolean(item, "enabled", label + " engedélyezve")
        gpio = item.get("gpio")
        if isinstance(gpio, int):
            if gpio not in GPIO_TO_PIN:
                v.err("%s: a GPIO%d nem érhető el a csatlakozón" % (label, gpio))
            elif gpio in used:
                v.err("A GPIO%d kétszer van kiosztva: %s és %s" % (gpio, IO_LABELS[used[gpio]], label))
            else:
                used[gpio] = name
    for gpio, what in bus_pins(adc).items():
        if gpio in used:
            v.err("A GPIO%d az ADC buszhoz kell (%s), nem használható ehhez: %s"
                  % (gpio, what, IO_LABELS[used[gpio]]))
    for gpio, what in uart_pins(cfg).items():
        if gpio in used:
            v.err("A GPIO%d a GSM modem soros portjához kell (%s), nem használható ehhez: %s"
                  % (gpio, what, IO_LABELS[used[gpio]]))

    channels = ADC_TYPES.get(adc.get("type"), {"channels": 8})["channels"]
    used_ch = {}
    for sid in SENSOR_IDS:
        s = cfg["sensors"].get(sid)
        label = sid.upper()
        if not isinstance(s, dict):
            v.err("%s: hiányzó szenzor beállítás" % label)
            continue
        v.text(s, "name", label + " neve", max_len=60, allow_empty=False)
        v.text(s, "short", label + " rövid neve", max_len=20, allow_empty=False)
        v.number(s, "channel", 0, channels - 1, label + " ADC csatorna", integer=True)
        v.number(s, "r25", 100.0, 1000000.0, label + " névleges ellenállás (R25)")
        v.number(s, "beta", 1000.0, 10000.0, label + " béta érték")
        v.number(s, "offset", -20.0, 20.0, label + " eltolás")
        v.number(s, "gain", 0.5, 2.0, label + " szorzó")
        v.boolean(s, "limit_enabled", label + " határérték figyelés")
        v.boolean(s, "stop_on_fault", label + " leállás szenzorhibánál")
        v.number(s, "min", -50.0, 150.0, label + " minimum")
        v.number(s, "max", -50.0, 150.0, label + " maximum")
        if isinstance(s.get("min"), float) and isinstance(s.get("max"), float) and s["min"] >= s["max"]:
            v.err("%s: a minimumnak kisebbnek kell lennie a maximumnál" % label)
        ch = s.get("channel")
        if isinstance(ch, int):
            if ch in used_ch:
                v.err("A(z) %d. ADC csatornát két szenzor is használja (%s, %s)" % (ch, used_ch[ch], label))
            used_ch[ch] = label

    sf = cfg["safety"]
    v.number(sf, "miner_start_delay_s", 0, 600, "Miner indítási késleltetés")
    v.number(sf, "flow_start_timeout_s", 2, 600, "Áramlás megindulási időkorlát")
    v.number(sf, "flow_loss_delay_s", 0.5, 120, "Áramlás kimaradás tűrés")
    v.number(sf, "post_stop_pump_s", 0, 3600, "Utókeringetés leállítás után")
    v.number(sf, "limit_trip_delay_s", 0, 600, "Határérték túllépés tűrés")
    v.number(sf, "sensor_fault_delay_s", 0, 600, "Szenzorhiba tűrés")
    v.boolean(sf, "overheat_enabled", "Utóhűtés védelem")
    v.number(sf, "overheat_temp_c", 30, 120, "Utóhűtés hőmérséklet küszöb")
    v.number(sf, "overheat_hysteresis_c", 0.5, 30, "Utóhűtés hiszterézis")
    v.number(sf, "overheat_min_run_s", 5, 3600, "Utóhűtés minimális ideje")
    v.boolean(sf, "overheat_use_water_temp", "Utóhűtés vízhőfok alapján")
    v.boolean(sf, "overheat_after_emergency", "Utóhűtés vészleállás után")
    v.boolean(sf, "auto_resume", "Automatikus visszaállás újraindulás után")
    v.number(sf, "manual_hold_timeout_s", 0.5, 10, "Kézi gomb időtúllépés")

    mn = cfg["miners"]
    v.number(mn, "poll_interval_s", 2, 300, "Miner lekérdezési időköz")
    v.number(mn, "timeout_s", 1, 30, "Miner kommunikációs időkorlát")
    v.number(mn, "production_loss_delay_s", 10, 7200, "Termelés kiesés tűrése")
    v.number(mn, "production_start_timeout_s", 60, 7200, "Termelés megindulásának időkorlátja")
    v.number(mn, "production_min_pct", 0, 95, "Termelés alsó határa")
    if not isinstance(mn.get("list"), list):
        v.err("Miner lista: érvénytelen formátum")
        mn["list"] = []
    seen_ids = set()
    seen_hosts = {}
    normalised = []
    for idx, raw in enumerate(mn["list"]):
        if not isinstance(raw, dict):
            v.err("Miner #%d: érvénytelen bejegyzés" % (idx + 1))
            continue
        m = copy.deepcopy(MINER_DEFAULTS)
        m.update(raw)
        label = "Miner '%s'" % (m.get("name") or m.get("host") or idx + 1)
        if not m.get("id") or not isinstance(m["id"], str) or m["id"] in seen_ids:
            m["id"] = new_miner_id()
        seen_ids.add(m["id"])
        v.text(m, "name", label + " neve", max_len=40)
        v.text(m, "host", label + " IP címe", max_len=253, allow_empty=False)
        if m["host"] and not _HOST_RE.match(m["host"]):
            v.err("%s: érvénytelen IP cím / hosztnév: %s" % (label, m["host"]))
        if not m["name"]:
            m["name"] = m["host"]
        v.choice(m, "type", MINER_TYPES, label + " típusa")
        v.number(m, "api_port", 1, 65535, label + " API port", integer=True)
        v.number(m, "web_port", 1, 65535, label + " web port", integer=True)
        v.text(m, "username", label + " felhasználó", max_len=64)
        if not isinstance(m.get("password"), str):
            m["password"] = "" if m.get("password") is None else str(m["password"])
        v.boolean(m, "enabled", label + " engedélyezve")
        key = (m["host"].lower(), m.get("api_port"))
        if m["type"] != "simulated" and m["host"]:
            if key in seen_hosts:
                v.err("%s: ugyanez az IP cím már szerepel (%s)" % (label, seen_hosts[key]))
            seen_hosts[key] = m["name"]
        normalised.append({k: m[k] for k in MINER_DEFAULTS})
    if len(normalised) > 200:
        v.err("Legfeljebb 200 miner kezelhető")
    mn["list"] = normalised

    lg = cfg["logging"]
    v.number(lg, "interval_s", 1, 3600, "Naplózási időköz")
    v.number(lg, "max_size_gb", 0.05, 2000, "Napló maximális mérete")
    v.number(lg, "min_free_mb", 0, 100000, "Minimális szabad hely")

    _validate_notifications(cfg["notifications"], v)
    cam = cfg["camera"]
    v.boolean(cam, "enabled", "Webkamera")
    v.boolean(cam, "standby", "Webkamera készenlét")
    v.choice(cam, "resolution", ("640x480", "1280x720", "1920x1080"), "Webkamera felbontás")
    if not isinstance(cam.get("device"), str) or not re.match(r"^(auto|picam|/dev/video[0-9]{1,3})$", cam["device"]):
        v.err("Webkamera eszköz: auto, picam vagy /dev/videoN")
        cam["device"] = "auto"
    return cfg, v.errors


def _secret(d: dict, key: str) -> None:
    value = d.get(key)
    d[key] = "" if value is None else str(value)


def _validate_notifications(nt: dict, v: "_Validator") -> None:
    v.boolean(nt, "enabled", "Értesítések")
    v.text(nt, "site_url", "Felület címe az értesítésekben", max_len=200)
    if nt["site_url"] and not re.match(r"^https?://", nt["site_url"]):
        v.err("Felület címe: http:// vagy https:// kezdetű cím kell")
    v.number(nt, "batch_s", 0, 300, "Események összevonása")
    v.boolean(nt, "notify_recovery", "Értesítés helyreálláskor")

    em = nt["email"]
    v.boolean(em, "enabled", "Email értesítés")
    v.text(em, "smtp_host", "SMTP szerver", max_len=253)
    v.number(em, "smtp_port", 1, 65535, "SMTP port", integer=True)
    v.choice(em, "security", ("starttls", "ssl", "none"), "SMTP titkosítás")
    v.text(em, "username", "SMTP felhasználónév", max_len=200)
    _secret(em, "password")
    v.text(em, "sender", "Feladó címe", max_len=200)
    v.text(em, "recipients", "Email címzettek", max_len=1000)
    v.number(em, "max_per_hour", 1, 1000, "Email óránkénti korlát", integer=True)
    for addr in split_emails(em["recipients"]):
        if not _EMAIL_RE.match(addr):
            v.err("Érvénytelen email cím: %s" % addr)
    if em["sender"] and not _EMAIL_RE.match(em["sender"]):
        v.err("Érvénytelen feladó cím: %s" % em["sender"])
    if em["enabled"]:
        if not em["smtp_host"]:
            v.err("Email értesítéshez add meg az SMTP szervert")
        if not split_emails(em["recipients"]):
            v.err("Email értesítéshez adj meg legalább egy címzettet")
        if not em["sender"] and not _EMAIL_RE.match(em["username"]):
            v.err("Email értesítéshez add meg a feladó címét")

    sm = nt["sms"]
    v.boolean(sm, "enabled", "SMS értesítés")
    v.choice(sm, "provider", ("gsm", "twilio", "http"), "SMS szolgáltató")
    v.text(sm, "recipients", "SMS címzettek", max_len=1000)
    v.boolean(sm, "ascii", "SMS ékezetek nélkül")
    v.number(sm, "max_per_hour", 1, 500, "SMS óránkénti korlát", integer=True)
    v.text(sm, "gsm_port", "GSM modem soros port", max_len=100)
    v.number(sm, "gsm_baud", 1200, 921600, "GSM modem sebesség", integer=True)
    if isinstance(sm.get("gsm_baud"), int) and sm["gsm_baud"] not in GSM_BAUDS:
        v.err("GSM modem sebesség: %s" % ", ".join(str(b) for b in GSM_BAUDS))
    _secret(sm, "gsm_pin")
    if sm["gsm_pin"] and not re.match(r"^[0-9]{4,8}$", sm["gsm_pin"]):
        v.err("SIM PIN: 4–8 számjegy")
    v.text(sm, "twilio_sid", "Twilio Account SID", max_len=64)
    _secret(sm, "twilio_token")
    v.text(sm, "twilio_from", "Twilio feladó szám", max_len=32)
    v.choice(sm, "http_method", ("GET", "POST"), "HTTP metódus")
    v.text(sm, "http_url", "HTTP átjáró URL", max_len=1000)
    if not isinstance(sm.get("http_body"), str):
        sm["http_body"] = ""
    v.choice(sm, "http_content_type", ("application/json", "application/x-www-form-urlencoded", "text/plain"),
             "HTTP tartalom típus")
    _secret(sm, "http_headers")
    for line in sm["http_headers"].splitlines():
        if line.strip() and ":" not in line:
            v.err("HTTP fejléc: 'Név: érték' formátum kell (%s)" % line.strip()[:40])
    for number in split_phones(sm["recipients"]):
        if not _PHONE_RE.match(number):
            v.err("Érvénytelen telefonszám: %s (formátum: +36301234567)" % number)
    if sm["enabled"]:
        if not split_phones(sm["recipients"]) and not (sm["provider"] == "http" and not http_needs_recipient(sm)):
            v.err("SMS értesítéshez adj meg legalább egy telefonszámot")
        if sm["provider"] == "gsm" and not sm["gsm_port"].startswith("/dev/"):
            v.err("GSM modem: a soros port egy /dev/... eszköz (pl. /dev/ttyUSB0 vagy /dev/serial0)")
        if sm["provider"] == "twilio":
            if not sm["twilio_sid"].startswith("AC") or not sm["twilio_token"] or not sm["twilio_from"]:
                v.err("Twilio: add meg az Account SID-et (AC…), a tokent és a feladó számot")
        if sm["provider"] == "http":
            if not re.match(r"^https?://", sm["http_url"]):
                v.err("HTTP átjáró: az URL http:// vagy https:// kezdetű legyen")
            elif "{message}" not in sm["http_url"] and "{message}" not in sm["http_body"]:
                v.err("HTTP átjáró: az URL-ben vagy a törzsben szerepelnie kell a {message} helyettesítőnek")

    rules = nt.get("rules")
    if not isinstance(rules, dict):
        rules = nt["rules"] = {}
    for cat in NOTIFY_CATEGORIES:
        rule = rules.get(cat)
        if not isinstance(rule, dict):
            rule = rules[cat] = {"email": False, "sms": False}
        for ch in ("email", "sms"):
            rule.setdefault(ch, False)
            v.boolean(rule, ch, "Értesítési szabály (%s / %s)" % (cat, ch))
    for key in list(rules):
        if key not in NOTIFY_CATEGORIES:
            del rules[key]


def _migrate(cfg: dict, raw: dict) -> dict:
    """Upgrade a configuration written by an older version."""
    raw_io = ((raw.get("hardware") or {}).get("io") or {}) if isinstance(raw, dict) else {}
    if "killswitch" not in raw_io:
        # new input: give it a free pin so an existing assignment never collides with the default
        io = cfg["hardware"]["io"]
        taken = {io[n]["gpio"] for n in IO_ALL if n != "killswitch"} | set(bus_pins(cfg["hardware"]["adc"]))
        for gpio in (5, 6, 12, 13, 16, 19, 20, 21, 26, 4, 18, 14, 15):
            if gpio not in taken:
                io["killswitch"]["gpio"] = gpio
                break
    return cfg


class ConfigStore:
    """Thread safe holder of the current configuration."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._listeners = []
        self.load_warnings = []
        self.created = False
        self._data = self._load()

    @property
    def data(self) -> dict:
        """Current configuration. READ-ONLY - never mutate the returned dict."""
        return self._data

    def add_listener(self, callback) -> None:
        """``callback(old_cfg, new_cfg)`` is called after every successful update."""
        self._listeners.append(callback)

    def _load(self) -> dict:
        found_any = False
        for candidate in (self.path, self.path + ".bak"):
            if not os.path.exists(candidate):
                continue
            found_any = True
            try:
                raw = read_json(candidate)
                if not isinstance(raw, dict):
                    raise ValueError("a fájl nem JSON objektum")
                cfg, errors = validate(_migrate(deep_merge(default_config(), raw), raw))
                if errors:
                    raise ConfigError(errors)
                if candidate != self.path:
                    self.load_warnings.append(
                        "A konfiguráció sérült volt, a biztonsági mentésből lett visszaállítva.")
                    atomic_write_json(self.path, cfg)
                return cfg
            except Exception as exc:  # noqa: BLE001 - report every failure
                msg = "Hibás konfigurációs fájl (%s): %s" % (os.path.basename(candidate), exc)
                log.error(msg)
                self.load_warnings.append(msg)
                try:
                    os.replace(candidate, "%s.broken-%d" % (candidate, int(time.time())))
                except OSError:
                    pass
        cfg, errors = validate(default_config())
        assert not errors, errors
        self.created = not found_any
        if found_any:
            self.load_warnings.append("Az alapértelmezett beállítások lettek betöltve - ellenőrizd a GPIO kiosztást!")
        atomic_write_json(self.path, cfg)
        return cfg

    def update(self, patch: dict) -> dict:
        """Merge ``patch`` into the configuration, validate, save and notify."""
        with self._lock:
            cfg, errors = validate(deep_merge(self._data, patch))
            if errors:
                raise ConfigError(errors)
            atomic_write_json(self.path, cfg, keep_backup=True)
            old = self._data
            self._data = cfg
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(old, cfg)
            except Exception:  # noqa: BLE001
                log.exception("Konfiguráció változás alkalmazása sikertelen")
        return cfg
