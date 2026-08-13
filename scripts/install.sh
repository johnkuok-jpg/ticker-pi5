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
sudo systemctl daemon-reload
sudo systemctl enable --now ticker ticker-web

echo
echo "Installed ticker-pi5. Open http://ticker.local:8080 on your phone"
echo "If mDNS is unavailable, use: http://$(hostname -I | awk '{print $1}'):8080"
