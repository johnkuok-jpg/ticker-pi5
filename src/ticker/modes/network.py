# MIT License — Copyright (c) 2026 John Kuok
"""Wi-Fi status, and the setup instructions when there is no network.

This mode exists because the ticker is headless in the strict sense: the only
way to control it is a web app on a network, so the moment it is not on a network
there is no channel left to tell the user anything -- except the panel itself.
So this screen carries the two facts that break that deadlock:

* when connected, the IP address, because ``ticker.local`` resolves through mDNS
  and mDNS is exactly what a guest network with client isolation blocks;
* when not connected, the name and password of the setup hotspot the ticker is
  broadcasting, and the address to open once joined.

The renderer forces this screen on by itself while the setup hotspot is up. That
is the one exception to "the panel only shows what the web app selected", and it
has to be: the web app is unreachable at that moment, so a screen the user cannot
select is the only way they will ever learn the hotspot's name.

Nothing here calls nmcli on the render thread. A status sweep runs three short
subprocesses and, on a radio with nothing to connect to, can take seconds, which
at 30fps would be a visible freeze. The sweep is done on a daemon thread and the
result swapped in when it lands.
"""

from __future__ import annotations

import threading
import time

from ticker import icons, net
from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

WHITE = (235, 240, 250)
DIM = (108, 122, 148)
AMBER = (255, 190, 60)
GREEN = (80, 220, 130)
ERROR = (255, 150, 60)
# Unlit signal bars: present enough to show how many are missing, dim enough not
# to be mistaken for lit ones at the 20% night brightness step.
BAR_OFF = (46, 54, 70)

HEADER_Y = 0
ROW_Y = (8, 16, 24)
GAP = 3
ICON_X = 0
ICON_Y = 1
ICON_WIDTH = len(icons.WIFI[0])
TITLE_X = ICON_WIDTH + GAP

# Three-character labels, so the value column starts at 24px and a full
# "192.168.100.100:8080" still fits in the remaining 104. Four-character labels
# looked better and overflowed by four pixels.
LABEL_WIDTH = 3
VALUE_X = (LABEL_WIDTH + 1) * 6

BARS = 4
BAR_WIDTH = 2
BAR_GAP = 1
BAR_BASE_Y = 31  # bottom edge, exclusive, matching fill_rect

# NetworkManager's "shared" IPv4 mode hands out 10.42.0.0/24 and takes .1 for
# itself, so this is the address of the ticker on its own hotspot. Only a
# fallback: the daemon writes the observed address into the notice.
HOTSPOT_URL = "10.42.0.1:8080"
WEB_PORT = 8080


def _tint(palette: dict[str, tuple[int, int, int]], color: tuple[int, int, int]) -> dict[str, tuple[int, int, int]]:
    """Recolour a single-colour sprite, keeping its shape."""
    return dict.fromkeys(palette, color)


class NetworkMode(Mode):
    """Show the Wi-Fi state, or how to get the ticker onto a network."""

    # Slower than the data modes on purpose: this is a state that changes when a
    # human does something, and each sweep costs several subprocesses.
    CACHE_SECONDS = 10

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.status = net.Status()
        self._last_refresh = -1e9
        self._refreshing = False

    def _refresh(self) -> None:
        """Replace the cached status. Safe to call from the worker thread."""
        try:
            self.status = net.status()
        finally:
            self._last_refresh = time.monotonic()
            self._refreshing = False

    def _refresh_soon(self) -> None:
        """Start a status sweep if one is due and none is already running.

        A frozen ``Status`` is swapped in as a whole, so a render that lands
        mid-sweep draws the previous state rather than a half-updated one.
        """
        if self._refreshing or time.monotonic() - self._last_refresh < self.CACHE_SECONDS:
            return
        self._refreshing = True
        threading.Thread(target=self._refresh, name="ticker-net", daemon=True).start()

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        notice = self.config.network_notice()

        # The notice is written by the fallback daemon, which already knows the
        # hotspot's name, password and address. Trusting it here means the setup
        # screen needs no nmcli call at all -- which matters, because that screen
        # is shown exactly when the radio is busy being an access point.
        if notice.get("state") == "hotspot":
            self._draw_setup(canvas, tick, notice)
            return

        self._refresh_soon()
        status = self.status
        if status.state == "connected":
            self._draw_connected(canvas, tick, status)
        else:
            self._draw_waiting(canvas, tick, status)

    def _header(self, canvas: Canvas, tick: int, title: str, color: tuple[int, int, int]) -> None:
        clock = self.clock_text(tick)
        clock_x = canvas.width - canvas.text_width(clock, SMALL)
        canvas.sprite(ICON_X, ICON_Y, icons.WIFI, _tint(icons.WIFI_PALETTE, color))
        canvas.text(TITLE_X, HEADER_Y, canvas.fit(title, clock_x - GAP - TITLE_X, SMALL), DIM, SMALL)
        canvas.text(clock_x, HEADER_Y, clock, WHITE, SMALL)

    def _row(self, canvas: Canvas, y: int, label: str, value: str, color: tuple[int, int, int]) -> None:
        canvas.text(0, y, label[:LABEL_WIDTH], DIM, SMALL)
        canvas.text(VALUE_X, y, canvas.fit(value, canvas.width - VALUE_X, SMALL), color, SMALL)

    def _draw_connected(self, canvas: Canvas, tick: int, status: net.Status) -> None:
        """The address, large, because that is the thing being looked up."""
        self._header(canvas, tick, status.ssid or "WI-FI", GREEN)
        # MEDIUM, not SMALL: an IP address is read off the panel digit by digit
        # while typing it into a phone, and every dotted quad fits at 6px a glyph.
        canvas.text(0, 10, canvas.fit(status.ip or "NO ADDRESS", canvas.width, MEDIUM), WHITE, MEDIUM)
        # TICKER_UNIT_NAME wins the bottom row when set -- with several
        # identical gift units on identical code, this is the one line on the
        # whole panel that says which physical board you're looking at.
        # Falls back to the old ticker.local:PORT hint so single-unit setups
        # (nothing set) see exactly what they always have.
        bottom = self.config.unit_name or f"ticker.local:{WEB_PORT}"
        canvas.text(0, ROW_Y[2], canvas.fit(bottom, canvas.width, SMALL), DIM, SMALL)
        self._draw_bars(canvas, status.signal)

    def _draw_setup(self, canvas: Canvas, tick: int, notice: dict[str, str]) -> None:
        """The one screen that has to work with no network at all."""
        self._header(canvas, tick, "WI-FI SETUP", AMBER)
        self._row(canvas, ROW_Y[0], "NET", notice.get("ssid") or net.HOTSPOT_SSID, WHITE)
        self._row(canvas, ROW_Y[1], "KEY", notice.get("password") or "-", AMBER)
        self._row(canvas, ROW_Y[2], "URL", notice.get("url") or HOTSPOT_URL, WHITE)

    def _draw_waiting(self, canvas: Canvas, tick: int, status: net.Status) -> None:
        headline, detail, color = {
            "connecting": ("CONNECTING", status.ssid or "", AMBER),
            "offline": ("NO WI-FI", "LOOKING FOR A NETWORK", AMBER),
            "unavailable": ("WI-FI IS OFF", "RADIO DISABLED", ERROR),
            "hotspot": ("SETUP MODE", "STARTING HOTSPOT", AMBER),
        }.get(status.state, ("WI-FI UNKNOWN", "CANNOT REACH NMCLI", ERROR))
        self._header(canvas, tick, "WI-FI", color)
        canvas.text_centered(12, headline, color, SMALL)
        if detail:
            canvas.text_centered(22, canvas.fit(detail, canvas.width, SMALL), DIM, SMALL)

    def _draw_bars(self, canvas: Canvas, signal: int) -> None:
        """Signal as a four-bar staircase in the bottom-right corner.

        Bars rather than a percentage: the number is not actionable, and the
        corner it sits in is the only space the address row leaves.
        """
        lit = net.Network(ssid="", signal=signal).bars
        width = BARS * BAR_WIDTH + (BARS - 1) * BAR_GAP
        left = canvas.width - 1 - width
        for index in range(BARS):
            height = index + 3
            x = left + index * (BAR_WIDTH + BAR_GAP)
            color = GREEN if index < lit else BAR_OFF
            canvas.fill_rect(x, BAR_BASE_Y - height, BAR_WIDTH, height, color)
