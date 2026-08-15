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

# Wi-Fi fallback lifecycle: keep update in step with install.sh. Only touch
# these files where NetworkManager is actually in charge; otherwise a
# dhcpcd-based image would end up with a sudoers rule for a binary it does
# not have and a service polling nothing.
WIFI_UNIT=""
if systemctl is-enabled --quiet NetworkManager 2>/dev/null && command -v nmcli >/dev/null; then
  sudo cp systemd/ticker-wifi.service /etc/systemd/system/
  # Validated before installation: a malformed drop-in in /etc/sudoers.d can
  # break sudo for every user on the box, so refuse rather than risk it.
  if sudo visudo -c -q -f systemd/ticker-nmcli.sudoers; then
    sudo install -m 0440 -o root -g root systemd/ticker-nmcli.sudoers /etc/sudoers.d/ticker-nmcli
  else
    echo "WARNING: systemd/ticker-nmcli.sudoers failed validation and was not updated."
    echo "         Existing /etc/sudoers.d/ticker-nmcli (if any) was left in place."
  fi
  WIFI_UNIT="ticker-wifi"
fi

sudo systemctl daemon-reload
# shellcheck disable=SC2086 - WIFI_UNIT is deliberately word-split or empty
sudo systemctl restart ticker ticker-web $WIFI_UNIT
echo "ticker-pi5 updated and restarted."
