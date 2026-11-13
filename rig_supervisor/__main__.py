"""Command line entry point:  python3 -m rig_supervisor [--data DIR] [--port N] [--sim]"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from . import APP_NAME, __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rig_supervisor", description="%s v%s" % (APP_NAME, __version__))
    parser.add_argument("--data", default=os.environ.get("RIG_DATA_DIR", os.path.join(os.getcwd(), "data")),
                        help="adatkönyvtár (konfiguráció, naplók) – alapértelmezett: ./data")
    parser.add_argument("--port", type=int, default=None, help="webes port (felülírja a beállítást)")
    parser.add_argument("--host", default="0.0.0.0", help="figyelt cím (alapértelmezett: minden interfész)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sim", dest="sim", action="store_true", default=None,
                      help="szimuláció, ha a webes felületen a hardver mód 'Automatikus' (a felületen "
                           "kiválasztott mód mindig elsőbbséget élvez)")
    mode.add_argument("--hardware", dest="sim", action="store_false",
                      help="valódi hardver, ha a webes felületen a hardver mód 'Automatikus'")
    parser.add_argument("--diagnose", action="store_true",
                        help="hardver diagnosztika kiírása (GPIO könyvtárak, jogosultságok), majd kilépés")
    parser.add_argument("--reset-password", action="store_true",
                        help="a webes jelszavas védelem kikapcsolása (elfelejtett jelszó esetén), majd kilépés")
    parser.add_argument("--log-level", default=os.environ.get("RIG_LOG_LEVEL", "INFO"),
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if args.reset_password:
        from .config import ConfigStore

        store = ConfigStore(os.path.join(args.data, "config.json"))
        store.update({"web": {"auth_enabled": False, "password_hash": ""}})
        print("A webes jelszavas védelem kikapcsolva.")
        return 0

    if args.diagnose:
        return _diagnose(args)

    from .app import App

    try:
        app = App(args.data, port=args.port, simulate=args.sim)
        app.start(args.host)
    except OSError as exc:
        logging.getLogger(__name__).error("Indítási hiba: %s", exc)
        return 1

    done = threading.Event()

    def _shutdown(signum, _frame):
        logging.getLogger(__name__).info("Leállítási jel (%s)", signum)
        done.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    print("%s v%s fut – webes felület: http://<raspberry-ip>:%d/  [hardver mód: %s – %s]" % (
        APP_NAME, __version__, app.port,
        "SZIMULÁCIÓ" if app.simulated else "valódi hardver (%s)" % app.io.status()["gpio_backend"],
        app.mode_reason), flush=True)
    while not done.wait(1.0):
        pass
    app.stop()
    return 0


def _diagnose(args) -> int:
    """Print everything that matters for GPIO access - without touching any pin."""
    import json

    from .hardware import gpio

    d = gpio.diagnostics()
    print("Python:            %s" % d["python"])
    print("Gép:               %s" % (d["model"] or "NEM Raspberry Pi (vagy nem felismerhető)"))
    print("Felhasználó:       %s  csoportok: %s" % (d["user"], ", ".join(d["groups"]) or "-"))
    for dev, state in d["devices"].items():
        print("  %-16s %s" % (dev, state))
    cfg_path = os.path.join(args.data, "config.json")
    try:  # read-only: the diagnosis must never modify anything
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        hw = cfg.get("hardware", {})
        setting = hw.get("backend", "auto")
        print("Beállított mód:    %s   (%s)" % (setting, cfg_path))
        if setting == "sim":
            print("   !!! SZIMULÁCIÓ van beállítva - a program NEM vezérli a GPIO lábakat.")
            print("       Váltás: webes felület > Beállítások > Kalibráció & GPIO > Hardver mód")
        for name in ("pump1", "pump2", "flow1", "flow2", "btn_start", "btn_stop"):
            io = hw.get("io", {}).get(name)
            if io:
                print("  %-10s GPIO%-2s  aktív szint: %s" % (name, io.get("gpio"), io.get("active")))
    except FileNotFoundError:
        print("Beállítások:       még nincs config.json itt: %s" % args.data)
    except (OSError, ValueError) as exc:
        print("Beállítások:       nem olvasható (%s): %s" % (cfg_path, exc))
    backend, errors = gpio.create_gpio_backend("rpi")
    if backend.name == "nincs":
        print("GPIO könyvtár:     NEM HASZNÁLHATÓ")
        for e in errors:
            print("   - %s" % e)
        return 1
    print("GPIO könyvtár:     %s (verzió: %s) - működik" % (backend.name, getattr(backend, "version", "?")))
    for e in errors:
        print("   (kihagyva: %s)" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
