#!/usr/bin/env bash
# Rig Supervisor telepítő Raspberry Pi OS / Raspbian rendszerre.
#   sudo ./install.sh              (alapértelmezett webes port: 8080)
#   sudo PORT=80 ./install.sh      (más port)
# Újratelepítés / frissítés: ugyanez a parancs (a beállítások és naplók megmaradnak).
set -euo pipefail

PORT="${PORT:-8080}"
APP_DIR=/opt/rig-supervisor
DATA_DIR=/var/lib/rig-supervisor
SERVICE=rig-supervisor
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "A telepítőt rendszergazdaként kell futtatni: sudo ./install.sh" >&2
  exit 1
fi
case "$PORT" in ''|*[!0-9]*) echo "Érvénytelen port: $PORT" >&2; exit 1 ;; esac

echo "==> Csomagok telepítése (python3, GPIO, I2C, SPI)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || echo "    (figyelmeztetés: apt-get update sikertelen – nincs internet?)"
if ! command -v python3 >/dev/null 2>&1; then
  apt-get install -y -qq python3 >/dev/null
fi
PYV="$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')"
if [ "$PYV" -lt 307 ]; then
  echo "Python 3.7 vagy újabb szükséges (talált: $(python3 --version))" >&2
  exit 1
fi
# csak azt telepítjük, ami hiányzik (a python3-rpi-lgpio és a python3-rpi.gpio ugyanazt a modult adja)
need_pkg() {  # need_pkg <python modul> <csomag>...
  local mod="$1"; shift
  if python3 -c "import $mod" >/dev/null 2>&1; then
    echo "    = $mod már telepítve"
    return 0
  fi
  for pkg in "$@"; do
    if apt-get install -y -qq "$pkg" >/dev/null 2>&1 && python3 -c "import $mod" >/dev/null 2>&1; then
      echo "    + $pkg"
      return 0
    fi
  done
  echo "    - $mod nem telepíthető ($*)"
  return 1
}
apt-get install -y -qq i2c-tools >/dev/null 2>&1 || true
GPIO_LIBS=0
need_pkg RPi.GPIO python3-rpi.gpio python3-rpi-lgpio && GPIO_LIBS=1
need_pkg lgpio python3-lgpio && GPIO_LIBS=1
need_pkg spidev python3-spidev || true
need_pkg smbus python3-smbus || need_pkg smbus2 python3-smbus2 || true
if [ "$GPIO_LIBS" -eq 0 ]; then
  echo "FIGYELEM: egyik GPIO könyvtár (RPi.GPIO / lgpio) sem telepíthető - a szivattyúk nem lesznek vezérelhetők!" >&2
fi

if command -v raspi-config >/dev/null 2>&1; then
  echo "==> I²C és SPI engedélyezése…"
  raspi-config nonint do_i2c 0 || true
  raspi-config nonint do_spi 0 || true
fi

echo "==> Szolgáltatás felhasználó (rigsup)…"
if ! id rigsup >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin rigsup
fi
for grp in gpio i2c spi dialout; do
  if getent group "$grp" >/dev/null; then usermod -aG "$grp" rigsup; fi
done

echo "==> Program másolása: $APP_DIR"
if systemctl is-active --quiet "$SERVICE"; then
  echo "    (a futó szolgáltatás újraindul – a szivattyúk állapota közben nem változik)"
fi
mkdir -p "$APP_DIR" "$DATA_DIR"
rm -rf "$APP_DIR/rig_supervisor.new"
cp -r "$SRC_DIR/rig_supervisor" "$APP_DIR/rig_supervisor.new"
find "$APP_DIR/rig_supervisor.new" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$APP_DIR/rig_supervisor.old"
if [ -d "$APP_DIR/rig_supervisor" ]; then mv "$APP_DIR/rig_supervisor" "$APP_DIR/rig_supervisor.old"; fi
mv "$APP_DIR/rig_supervisor.new" "$APP_DIR/rig_supervisor"
rm -rf "$APP_DIR/rig_supervisor.old"
chown -R root:root "$APP_DIR"
chown -R rigsup:rigsup "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "==> Segédparancs: rig-supervisor-reset-password"
cat > /usr/local/bin/rig-supervisor-reset-password <<EOF
#!/bin/sh
# A webes jelszavas védelem kikapcsolása (elfelejtett jelszó esetén)
cd $APP_DIR && sudo -u rigsup /usr/bin/python3 -m rig_supervisor --data $DATA_DIR --reset-password
EOF
chmod 755 /usr/local/bin/rig-supervisor-reset-password

echo "==> systemd szolgáltatás…"
sed "s/@PORT@/$PORT/" "$SRC_DIR/deploy/rig-supervisor.service" > /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"
sleep 2

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
if systemctl is-active --quiet "$SERVICE"; then
  echo "Kész! A webes felület: http://${IP:-<raspberry-ip>}:$PORT/"
else
  echo "A szolgáltatás nem indult el. Napló: journalctl -u $SERVICE -n 50" >&2
  exit 1
fi
echo
echo "==> Hardver ellenőrzés a szolgáltatás felhasználójával (rigsup):"
(cd "$APP_DIR" && runuser -u rigsup -- env LG_WD="$DATA_DIR" /usr/bin/python3 -m rig_supervisor --diagnose --data "$DATA_DIR") \
  || echo "FIGYELEM: a GPIO nem használható - lásd a fenti hibákat és a README 'Hibaelhárítás' részét." >&2
echo
echo "Napló megtekintése:      journalctl -u $SERVICE -f"
echo "Leállítás / indítás:     sudo systemctl stop|start $SERVICE"
if [ -f /var/run/reboot-required ] || ! ls /dev/i2c-1 >/dev/null 2>&1; then
  echo "Megjegyzés: az I²C/SPI engedélyezése után egy újraindítás szükséges lehet (sudo reboot)."
fi
