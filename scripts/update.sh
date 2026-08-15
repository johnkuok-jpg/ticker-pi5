#!/usr/bin/env bash
# MIT License — Copyright (c) 2026 John Kuok
# Update an existing ticker-pi5 installation without changing its .env.
set -euo pipefail

cd /home/pi/ticker-pi5
# The repo is owned by pi, but this script runs as root. Modern git refuses
# to touch a repo owned by a different user ("detected dubious ownership")
# unless it is whitelisted. Whitelist inline so we do not have to mutate
# root's global git config on every Pi in the fleet.
git -c safe.directory=/home/pi/ticker-pi5 pull --ff-only
venv/bin/python -m pip install --upgrade -r requirements.txt
venv/bin/python -m pip install --editable .
sudo cp systemd/ticker.service systemd/ticker-web.service /etc/systemd/system/
# Keep the fleet auto-update units in sync too, so a rollout that tweaks the
# poll interval or the updater script itself takes effect on the next tick.
# The timer is only reloaded, not restarted: an in-flight update on this Pi
# should finish cleanly rather than getting killed and half-applied.
sudo cp systemd/ticker-updater.service systemd/ticker-updater.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ticker ticker-web
echo "ticker-pi5 updated and restarted."
