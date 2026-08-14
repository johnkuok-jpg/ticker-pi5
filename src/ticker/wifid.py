# MIT License — Copyright (c) 2026 John Kuok
"""Wi-Fi fallback daemon: raise a setup hotspot when no known network is around.

Runs as root under systemd, polls the radio, and when the ticker has been off a
network for long enough it broadcasts its own access point so a phone can reach
the web app and hand it new credentials. It also writes the notice file the panel
reads, which is what makes the hotspot discoverable at all -- an access point
nobody knows the name of is no better than no network.

Why this rather than comitup, which does the same job: comitup ships its own
captive web UI for choosing a network
(https://manpages.ubuntu.com/manpages/jammy/man8/comitup.8.html), and this
project already has a web app that owns every control. Two UIs on one device,
one of which appears only sometimes, is worse than a small daemon.

Why a daemon rather than an autoconnect priority on the hotspot profile: letting
NetworkManager pick the access point itself, with ``autoconnect-priority`` at the
documented -999 floor, is reported to work but is sensitive to ordering during
boot, and the failure mode is the ticker sitting on its own island next to a
working router. An explicit state machine with a grace period is auditable, and
the decision function is unit-testable without a radio.

The single-radio constraint drives the rest: a Pi 5 cannot reliably scan while
acting as an access point, so once the hotspot is up this drops it periodically,
looks around, and either reconnects or puts it straight back. See
:func:`ticker.net.next_action`.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from ticker import net
from ticker.config import load_config

LOGGER = logging.getLogger("ticker.wifid")

POLL_SECONDS = 20.0
# How long to give NetworkManager to find a known network after the hotspot is
# dropped: association plus DHCP on a slow router, and no longer, because the
# panel is showing nothing useful for this whole window.
RECONNECT_GRACE = 25.0

_running = True


def _stop(signum, frame) -> None:  # noqa: ANN001, ARG001 - signal handler
    global _running
    _running = False


class Daemon:
    """The poll loop. Holds only the state :func:`net.next_action` needs."""

    def __init__(self, config, poll: float = POLL_SECONDS) -> None:  # type: ignore[no-untyped-def]
        self.config = config
        self.poll = poll
        self.misses = 0
        self.hotspot_since: float | None = None

    def publish(self, status: net.Status) -> None:
        """Write the notice the panel reads, or clear it when nothing is wrong.

        The observed address is preferred over the documented 10.42.0.1 because a
        NetworkManager shared-mode subnet can be reconfigured, and a URL on the
        panel that does not work is worse than none.
        """
        if status.state != "hotspot":
            self.config.set_network_notice(None)
            return
        address = status.ip or "10.42.0.1"
        self.config.set_network_notice(
            {
                "state": "hotspot",
                "ssid": net.HOTSPOT_SSID,
                "password": net.hotspot_password(self.config.state_dir),
                "url": f"{address}:8080",
            }
        )

    def raise_hotspot(self) -> None:
        password = net.hotspot_password(self.config.state_dir)
        ok, output = net.hotspot_up(password)
        if not ok:
            LOGGER.error("could not raise the setup hotspot: %s", output)
            # Do not reset the miss counter: try again next tick rather than
            # waiting out another full grace period.
            return
        LOGGER.warning("no known network; broadcasting %s", net.HOTSPOT_SSID)
        self.hotspot_since = time.monotonic()
        self.publish(net.status())

    def clear_hotspot(self) -> None:
        net.hotspot_down()
        self.hotspot_since = None
        self.misses = 0
        self.config.set_network_notice(None)
        LOGGER.info("back on a real network; setup hotspot withdrawn")

    def retry_known(self) -> None:
        """Drop the access point briefly to see whether a known network is back.

        This is the single-radio compromise: while the hotspot is up the ticker is
        deaf, so the only way to notice that the user has come home is to stop
        broadcasting and listen. The window is short and the access point returns
        immediately if nothing is found, so a phone that was mid-setup loses the
        connection for a few seconds at worst.
        """
        LOGGER.info("checking for known networks")
        net.hotspot_down()
        self.hotspot_since = None
        if not net.known_network_in_range():
            LOGGER.info("still nothing known in range")
            self.raise_hotspot()
            return
        # A known network is visible, so NetworkManager will autoconnect on its
        # own. Waiting here rather than forcing a connection keeps profile
        # priority in NetworkManager's hands, where the user's choices live.
        deadline = time.monotonic() + RECONNECT_GRACE
        while _running and time.monotonic() < deadline:
            if net.status().state in ("connected", "connecting"):
                self.misses = 0
                self.config.set_network_notice(None)
                LOGGER.info("rejoined a known network")
                return
            time.sleep(2.0)
        LOGGER.info("known network did not come up in time; hotspot returning")
        self.raise_hotspot()

    def tick(self) -> str:
        """One poll. Returns the action taken, for logs and tests."""
        status = net.status()
        if status.state in ("connected", "connecting"):
            self.misses = 0
        elif status.state != "hotspot":
            self.misses += 1

        action = net.next_action(status.state, self.misses, self.hotspot_since, time.monotonic())
        if action == "raise_hotspot":
            self.raise_hotspot()
        elif action == "clear_hotspot":
            self.clear_hotspot()
        elif action == "retry_known":
            self.retry_known()
        elif status.state == "hotspot" and self.hotspot_since is None:
            # Adopt a hotspot somebody else raised -- most likely this daemon
            # before a restart. Without this the access point would stay up
            # forever, because the retry timer would never start.
            self.hotspot_since = time.monotonic()
            self.publish(status)
        return action

    def run(self) -> int:
        if not net.available():
            LOGGER.error("nmcli is not installed; nothing to do")
            return 1
        LOGGER.info("watching Wi-Fi every %.0fs", self.poll)
        while _running:
            try:
                self.tick()
            except Exception:  # a daemon that dies leaves no way back onto a network
                LOGGER.exception("poll failed; continuing")
            # Sleep in short slices so a stop signal is honoured promptly instead
            # of after a full poll interval.
            slept = 0.0
            while _running and slept < self.poll:
                time.sleep(min(1.0, self.poll - slept))
                slept += 1.0
        LOGGER.info("stopping")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll", type=float, default=POLL_SECONDS, help="seconds between checks")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    daemon = Daemon(load_config(), poll=args.poll)
    if args.once:
        status = net.status()
        print(f"state={status.state} ssid={status.ssid or '-'} ip={status.ip or '-'}")
        print(f"action={daemon.tick()}")
        return 0
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
