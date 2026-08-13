#!/usr/bin/env bash
# MIT License — Copyright (c) 2026 John Kuok
# Disable ticker-pi5 services while deliberately leaving the repository and configuration intact.
set -euo pipefail

sudo systemctl disable --now ticker.service ticker-web.service || true
sudo rm -f /etc/systemd/system/ticker.service /etc/systemd/system/ticker-web.service
sudo systemctl daemon-reload
echo "Services removed. /home/pi/ticker-pi5 and /var/lib/ticker were left in place."
