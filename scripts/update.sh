#!/usr/bin/env bash
# MIT License — Copyright (c) 2026 John Kuok
# Update an existing ticker-pi5 installation without changing its .env.
set -euo pipefail

cd /home/pi/ticker-pi5
git pull --ff-only
venv/bin/python -m pip install --upgrade -r requirements.txt
venv/bin/python -m pip install --editable .
sudo cp systemd/ticker.service systemd/ticker-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ticker ticker-web
echo "ticker-pi5 updated and restarted."
