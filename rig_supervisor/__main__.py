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
    parser.add_argument("--gpio-test", action="store_true",
                        help="a kimenetek ki-be kapcsolása és visszaolvasása, a bemenetek figyelése 10 mp-ig, majd "
                             "kilépés (a szolgáltatást előtte le kell állítani)")
    parser.add_argument("--yes", action="store_true", help="--gpio-test: ne kérdezzen rá a kimenetek kapcsolására")
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
    if args.gpio_test:
        return _gpio_test(args)

    from .app import AlreadyRunning, App

    try:
        app = App(args.data, port=args.port, simulate=args.sim)
        app.start(args.host)
    except AlreadyRunning as exc:
        logging.getLogger(__name__).error("A program NEM indult el: %s", exc)
        return 3
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
        for name in ("pump1", "pump2", "flow1", "flow2", "btn_start", "btn_stop", "killswitch"):
            io = hw.get("io", {}).get(name)
            if io:
                print("  %-10s GPIO%-2s  aktív szint: %s" % (name, io.get("gpio"), io.get("active")))
    except FileNotFoundError:
        print("Beállítások:       még nincs config.json itt: %s" % args.data)
    except (OSError, ValueError) as exc:
        print("Beállítások:       nem olvasható (%s): %s" % (cfg_path, exc))
    from .hardware import owner

    others = owner.other_instances()
    if others:
        print("Futó példányok:    %d db" % len(others))
        for o in others:
            print("   PID %-7d %s" % (o["pid"], o["cmd"]))
        if len(others) > 1:
            print("   !!! TÖBB példány fut - egyszerre csak egy vezérelheti a GPIO lábakat!")
    else:
        print("Futó példányok:    nincs (a szolgáltatás nem fut)")
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


def _load_cfg(args):
    """Configuration for the GPIO test (read-only; the service's data dir is preferred)."""
    from .config import ConfigStore

    for d in (args.data, "/var/lib/rig-supervisor"):
        path = os.path.join(d, "config.json")
        if os.path.exists(path):
            try:
                with open(path, "rb"):
                    pass
            except OSError as exc:
                print("A beállítások nem olvashatók (%s): %s – futtasd sudo-val" % (path, exc))
                continue
            print("Beállítások:       %s" % path)
            import tempfile
            import shutil
            tmp = tempfile.mkdtemp()
            shutil.copy(path, os.path.join(tmp, "config.json"))  # never written back
            return ConfigStore(os.path.join(tmp, "config.json")).data
    print("Beállítások:       nincs config.json - alapértelmezett lábkiosztás")
    import tempfile
    return ConfigStore(os.path.join(tempfile.mkdtemp(), "config.json")).data


def _gpio_test(args) -> int:
    """Switch every output on and off, read it back, then watch the inputs for 10 s."""
    import time

    from .config import IO_INPUTS, IO_LABELS, IO_OUTPUTS
    from .hardware import gpio, owner

    print("Gép:               %s" % (gpio.pi_model() or "NEM Raspberry Pi"))
    ok, other = owner.acquire()
    if not ok:
        print("\nHIBA: %s" % other)
        print("A teszt előtt állítsd le:  sudo systemctl stop rig-supervisor")
        return 2
    cfg = _load_cfg(args)
    io = cfg["hardware"]["io"]
    backend, errors = gpio.create_gpio_backend("rpi")
    if backend.name == "nincs":
        print("GPIO könyvtár:     NEM HASZNÁLHATÓ")
        for e in errors:
            print("   - %s" % e)
        return 1
    print("GPIO könyvtár:     %s" % backend.name)
    if not args.yes:
        try:
            answer = input("\nA szivattyúk SSR-jei egyenként 2-2 mp-re BEKAPCSOLNAK. Folytatod? (i/n) ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("i", "igen", "y", "yes"):
            print("Megszakítva.")
            return 1
    result = 0
    print("\nKIMENETEK")
    for name in IO_OUTPUTS:
        pin, active_high = int(io[name]["gpio"]), io[name]["active"] == "high"
        conflict = backend.conflict(pin)
        print("  %s – GPIO%d (aktív: %s)%s" % (IO_LABELS[name], pin, "magas" if active_high else "alacsony",
                                                "  !!! " + conflict if conflict else ""))
        try:
            backend.setup_output(pin, not active_high)
            for on in (True, False, True, False):
                level = on == active_high
                backend.write(pin, level)
                samples = []
                end = time.monotonic() + (2.0 if on else 1.0)
                while time.monotonic() < end:
                    samples.append(bool(backend.read(pin)))
                    time.sleep(0.05)
                bad = sum(1 for x in samples if x != level)
                state = "OK" if not bad else "HIBA: %d/%d mintában eltér (más is vezérli, rövidzár?)" % (bad, len(samples))
                if bad:
                    result = 3
                print("     %-3s -> láb %d (%s)  visszaolvasva: %s" % ("BE" if on else "KI", level,
                                                                       "3,3 V" if level else "0 V", state))
        except Exception as exc:  # noqa: BLE001
            print("     HIBA: %s" % exc)
            result = 3
    print("\nBEMENETEK (10 mp figyelés - most nyomd meg a gombokat, nyisd/zárd a kapcsolókat)")
    pins = {}
    for name in IO_INPUTS:
        pin = int(io[name]["gpio"])
        conflict = backend.conflict(pin)
        try:
            backend.setup_input(pin, io[name].get("pull", "off"))
            pins[name] = pin
        except Exception as exc:  # noqa: BLE001
            print("  %s – GPIO%d: HIBA: %s" % (IO_LABELS[name], pin, exc))
        if conflict:
            print("  %s – GPIO%d: !!! %s" % (IO_LABELS[name], pin, conflict))
    first = {n: backend.read(p) for n, p in pins.items()}
    changes = {n: 0 for n in pins}
    last = dict(first)
    end = time.monotonic() + 10.0
    while time.monotonic() < end:
        for n, p in pins.items():
            v = backend.read(p)
            if v != last[n]:
                changes[n] += 1
                last[n] = v
        time.sleep(0.005)
    for n, p in pins.items():
        note = ""
        if changes[n] > 10:
            note = "  !!! UGRÁL - lebegő bemenet / zaj / laza érintkezés"
        print("  %-30s GPIO%-2d felhúzó: %-4s  kezdő szint: %d  váltások: %d%s" % (
            IO_LABELS[n], p, io[n].get("pull", "off"), first[n], changes[n], note))
    print("\nA kimenetek inaktív (KI) állapotban maradtak. Szolgáltatás indítása: sudo systemctl start rig-supervisor")
    return result


if __name__ == "__main__":
    sys.exit(main())
