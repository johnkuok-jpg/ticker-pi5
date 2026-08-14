#!/usr/bin/env bash
# MIT License — Copyright (c) 2026 John Kuok
#
# Poll GitHub for new commits on the tracked branch (default: main) and, if
# any land, run scripts/update.sh to bring this ticker onto the new code.
#
# Runs as the ticker-updater.service unit driven by ticker-updater.timer.
# The whole point is that a fleet of tickers can share one GitHub repo:
# every Pi self-heals to whatever is on `main`, without per-device push
# infrastructure, port forwarding, VPNs, or webhooks. Push to main -> every
# online Pi picks it up within the timer interval (default 5 min); an
# offline Pi picks it up when it comes back.
#
# Design notes:
#   * We compare the local HEAD to the remote tracking branch via a bare
#     `git fetch` + `git rev-parse`, so a no-op poll costs one small HTTPS
#     round trip and no disk writes. This is what we run 288 times per day.
#   * We only invoke `update.sh` (pip install + systemd restart) when the
#     SHA actually moved. Restarts flash the LED panel, so we do not want
#     them on every timer tick.
#   * We use a flock so overlapping timer runs (slow network, upstream
#     hiccup) do not stomp on each other mid-pip-install.
#   * All output goes to the journal via systemd; run
#     `journalctl -u ticker-updater.service -f` to watch a rollout live.

set -euo pipefail

REPO_DIR="${TICKER_REPO_DIR:-/home/pi/ticker-pi5}"
BRANCH="${TICKER_UPDATE_BRANCH:-main}"
LOCK_FILE="/run/ticker-updater.lock"

# Serialize timer ticks. `-n` = exit immediately if the previous poll is
# still running (probably a slow pip install), rather than piling up.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "auto_update: previous run still in progress, skipping."
    exit 0
fi

cd "${REPO_DIR}"

# `--quiet` keeps the happy-path journal clean; failures still print to
# stderr and become a systemd unit failure the operator can see.
if ! git fetch --quiet origin "${BRANCH}"; then
    echo "auto_update: git fetch failed (network?); will retry next tick."
    exit 0
fi

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/${BRANCH}")"

if [ "${LOCAL}" = "${REMOTE}" ]; then
    # Up to date. Exit 0 so systemd shows the timer as healthy.
    exit 0
fi

echo "auto_update: new commit on ${BRANCH} (${LOCAL:0:7} -> ${REMOTE:0:7}), updating."

# Delegate the actual install + restart to the existing script so there
# is one code path for both manual `sudo scripts/update.sh` runs and the
# automatic poller. Anything the auto-update needs (pip, systemd files,
# service restarts) belongs in update.sh, not here.
if scripts/update.sh; then
    echo "auto_update: updated to ${REMOTE:0:7}."
else
    # update.sh already logged the specific failure; surface a top-line
    # summary so `systemctl status ticker-updater` is useful at a glance.
    echo "auto_update: update.sh failed on ${REMOTE:0:7}; leaving Pi on ${LOCAL:0:7}."
    exit 1
fi
