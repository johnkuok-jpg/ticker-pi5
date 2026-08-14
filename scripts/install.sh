#!/usr/bin/env bash
# MIT License — Copyright (c) 2026 John Kuok
# Install ticker-pi5 on a fresh Raspberry Pi OS Lite (64-bit, Bookworm) system.
set -euo pipefail

REPO_URL="https://github.com/johnkuok-jpg/ticker-pi5.git"
TARGET="/home/pi/ticker-pi5"

if [[ ! -d "$TARGET/.git" ]]; then
  sudo mkdir -p /home/pi
  sudo chown pi:pi /home/pi
  sudo -u pi git clone "$REPO_URL" "$TARGET"
fi

cd "$TARGET"
sudo apt update
sudo apt install -y python3-venv python3-dev build-essential pkg-config git

if [[ ! -d venv ]]; then
  # PioMatter needs the distro-provided GPIO bindings, so keep system packages visible.
  python3 -m venv --system-site-packages venv
fi
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m pip install --editable .

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created $TARGET/.env — set WEATHER_LAT and WEATHER_LON before using weather mode."
fi

# Root renderer and pi web server deliberately share this writable, file-backed state.
sudo install -d -m 0775 -o pi -g pi /var/lib/ticker
sudo cp systemd/ticker.service systemd/ticker-web.service /etc/systemd/system/
# Fleet auto-update units: installed on every Pi so a later
# `systemctl enable --now ticker-updater.timer` needs no repo edits, but
# NOT enabled by default -- the operator opts in per-device below.
sudo cp systemd/ticker-updater.service systemd/ticker-updater.timer /etc/systemd/system/

# Wi-Fi fallback. Only installed where NetworkManager is actually in charge of
# the radio: on an older dhcpcd-based image the daemon would poll a tool that
# reports nothing, and the sudoers rule would grant access to a binary that is
# not there.
if systemctl is-enabled --quiet NetworkManager 2>/dev/null && command -v nmcli >/dev/null; then
  sudo cp systemd/ticker-wifi.service /etc/systemd/system/
  # Validated before installation: a malformed file in /etc/sudoers.d can break
  # sudo for every user on the box, so this refuses rather than risks it.
  if sudo visudo -c -q -f systemd/ticker-nmcli.sudoers; then
    sudo install -m 0440 -o root -g root systemd/ticker-nmcli.sudoers /etc/sudoers.d/ticker-nmcli
  else
    echo "WARNING: systemd/ticker-nmcli.sudoers failed validation and was not installed."
    echo "         The Wi-Fi page will be read-only until this is fixed."
  fi
  WIFI_UNIT="ticker-wifi"
else
  echo "NetworkManager not detected; skipping the Wi-Fi fallback hotspot."
  WIFI_UNIT=""
fi

sudo systemctl daemon-reload
# shellcheck disable=SC2086 - WIFI_UNIT is deliberately word-split or empty
sudo systemctl enable --now ticker ticker-web $WIFI_UNIT

# Opt into fleet auto-update by exporting TICKER_AUTO_UPDATE=1 before running
# install.sh. This is off by default so a first-time install on the
# maintainer's own bench does not silently re-pull mid-hacking. On gift Pis,
# set it to 1 so `git push` on the maintainer's machine reaches every unit.
if [[ "${TICKER_AUTO_UPDATE:-0}" == "1" ]]; then
  sudo systemctl enable --now ticker-updater.timer
  echo "Auto-update enabled: this Pi will poll GitHub every 5 minutes."
else
  echo "Auto-update available but not enabled. Turn it on with:"
  echo "  sudo systemctl enable --now ticker-updater.timer"
fi

echo
echo "Installed ticker-pi5. Open http://ticker.local:8080 on your phone"
echo "If mDNS is unavailable, use: http://$(hostname -I | awk '{print $1}'):8080"
if [[ -n "$WIFI_UNIT" ]]; then
  echo
  echo "Wi-Fi fallback is on. Away from every known network the ticker broadcasts"
  echo "${WIFI_SETUP_SSID:-TICKER-SETUP}; the panel shows the password and the address to open."
fi
