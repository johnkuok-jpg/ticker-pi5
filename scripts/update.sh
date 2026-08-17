#!/usr/bin/env bash
# MIT License — Copyright (c) 2026 John Kuok
# Update an existing ticker-pi5 installation without changing its .env.
set -euo pipefail

cd /home/pi/ticker-pi5

# Run git and pip as `pi`, not as root.
#
# This script runs under sudo (and as root from ticker-updater.service), but
# the checkout and the venv are owned by pi. Earlier versions ran `git pull`
# as root with a `safe.directory` whitelist, which silenced git's ownership
# *warning* without changing who owns the objects it writes. The result: root
# wrote root-owned files into .git/objects and into venv/lib/.../site-packages
# on every single update, and the next plain `git pull` as pi died with
#   error: insufficient permission for adding an object to repository database
# Dropping to pi for these two steps means nothing root-owned is created in
# the first place, and safe.directory is no longer needed because pi owns the
# repo it is being asked to touch.
if [[ "$(id -un)" == "pi" ]]; then
  as_pi=()
else
  as_pi=(sudo -u pi)
fi

# Self-heal boxes already poisoned by the old root-owned-writes behaviour.
# Guarded by a cheap probe so the common case does not pay for a recursive
# chown across the whole venv on every update tick.
if find /home/pi/ticker-pi5 -uid 0 -print -quit | grep -q .; then
  echo "Repairing root-owned files left by a previous update..."
  chown -R pi:pi /home/pi/ticker-pi5
fi

"${as_pi[@]}" git pull --ff-only
# --no-cache-dir keeps pip from stockpiling every wheel it ever downloads
# in ~/.cache/pip. yt-dlp releases weekly, so on an SD card that cache
# would grow indefinitely; we never reinstall offline, so caching buys
# nothing here.
"${as_pi[@]}" venv/bin/python -m pip install --no-cache-dir --upgrade -r requirements.txt
"${as_pi[@]}" venv/bin/python -m pip install --no-cache-dir --editable .
# fonts-noto-cjk was added later than the first Pi installs, so an update
# on an older box needs to backfill it. `apt install -y` is a no-op when
# it's already present, so this is cheap.
sudo apt install -y fonts-noto-cjk
sudo cp systemd/ticker.service systemd/ticker-web.service /etc/systemd/system/
# Keep the fleet auto-update units in sync too, so a rollout that tweaks the
# poll interval or the updater script itself takes effect on the next tick.
# The timer is only reloaded, not restarted: an in-flight update on this Pi
# should finish cleanly rather than getting killed and half-applied.
sudo cp systemd/ticker-updater.service systemd/ticker-updater.timer /etc/systemd/system/

# SD-card hardening: backfill the journal cap and apt-clean drop-in on
# older Pis that were installed before these existed. Idempotent — writing
# the same file on every tick is fine, and journald is only restarted when
# the file actually changed.
sudo install -d -m 0755 /etc/systemd/journald.conf.d
if ! sudo cmp -s systemd/journald-ticker.conf /etc/systemd/journald.conf.d/ticker.conf 2>/dev/null; then
  sudo install -m 0644 systemd/journald-ticker.conf /etc/systemd/journald.conf.d/ticker.conf
  sudo systemctl restart systemd-journald
fi
sudo install -m 0644 systemd/99-ticker-apt-clean.conf /etc/apt/apt.conf.d/99-ticker-apt-clean
sudo apt-get clean

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
