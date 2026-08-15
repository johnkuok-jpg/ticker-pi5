#!/usr/bin/env bash
# MIT License — Copyright (c) 2026 John Kuok
# Disable ticker-pi5 services while deliberately leaving the repository and configuration intact.
set -euo pipefail

sudo systemctl disable --now ticker.service ticker-web.service || true
# The updater timer may or may not have been opted in; disable both units
# either way so we do not leave a poller running against a repo the
# operator just told us to remove.
sudo systemctl disable --now ticker-updater.timer ticker-updater.service || true
# The Wi-Fi fallback daemon and its privileged sudoers drop-in are only
# present on NetworkManager-based images. Disable and remove them either way
# -- "|| true" swallows the case where nothing is installed to remove.
sudo systemctl disable --now ticker-wifi.service || true
sudo rm -f /etc/systemd/system/ticker.service /etc/systemd/system/ticker-web.service \
           /etc/systemd/system/ticker-updater.service /etc/systemd/system/ticker-updater.timer \
           /etc/systemd/system/ticker-wifi.service
# Removing the sudoers drop-in matters more than the unit files: leaving a
# rule for a binary the operator no longer expects to be privileged is a
# real security regression, not just clutter.
sudo rm -f /etc/sudoers.d/ticker-nmcli
sudo systemctl daemon-reload
echo "Services removed. /home/pi/ticker-pi5 and /var/lib/ticker were left in place."
