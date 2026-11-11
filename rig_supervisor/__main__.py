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
                      help="szimulációs mód (valódi hardver nélkül)")
    mode.add_argument("--hardware", dest="sim", action="store_false", help="valódi hardver kényszerítése")
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
    print("%s v%s fut – webes felület: http://<raspberry-ip>:%d/%s" % (
        APP_NAME, __version__, app.port, "  [SZIMULÁCIÓS MÓD]" if app.simulated else ""), flush=True)
    while not done.wait(1.0):
        pass
    app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
