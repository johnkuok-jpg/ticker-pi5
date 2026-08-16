# MIT License — Copyright (c) 2026 John Kuok
"""Live SF Muni arrival board for one stop.

Three upcoming buses, soonest first, in the route's own colour. The stop is
chosen from the web app by typing the 5-digit code printed on every Muni
shelter sign, or by searching the stop directory the picker builds on
demand.

Layout matches the BART mode's grammar so a rider glances at either panel
the same way: red Muni worm-style wordmark and stop label across the top,
then three rows of ``ROUTE  DESTINATION           NNM``. Route colours come
from Muni's own hex palette, brightened along their own hue to clear the
LED panel's legibility floor — a channel-uniform scale preserves each
route's identity while making sure the T's dark teal doesn't disappear at
20% night brightness.
"""

from __future__ import annotations

import time

from ticker import muni
from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode

# Muni's signage red on paper is close to Pantone 173 — a saturated scarlet
# with a warm shift. #E4002B at panel brightness reads slightly pink at
# scale so the values below are pulled marginally toward orange, matching
# how the wordmark looks on a real shelter under daylight.
MUNI_RED = (230, 50, 40)
MUNI_RED_DIM = (150, 34, 26)

WHITE = (235, 240, 250)
DIM = (108, 122, 148)
AMBER = (255, 190, 60)
ERROR = (255, 150, 60)

HEADER_Y = 0
# Row baselines match BART's board so a rider glances between the two
# panels the same way. 8-pixel rows starting at y=8 leave the header on
# top and the last row bottom-aligned at y=31.
ROW_Y = (8, 16, 24)
LABEL_X = 0
GAP = 3

# ---------------------------------------------------------------------------
# Muni "worm" wordmark, hand-drawn as a bitmap
# ---------------------------------------------------------------------------
#
# The real Muni worm is one continuous cursive stroke; at eight pixels tall
# a faithful trace would blob into a red smear. The renderer's version
# instead spells "muni" in a bold pixel font whose 'm' and 'n' curl back
# into their stems, echoing the worm's ligatures. All four glyphs are 7
# pixels wide and 8 tall, with a 1-pixel kern, so the mark occupies a
# 31-pixel-wide band that leaves the top row's right side free for the
# stop label and clock.
_WORM_GLYPHS: dict[str, tuple[str, ...]] = {
    # Bold, slightly rounded 'm' with the shoulder curve preserved.
    "m": (
        ".......",
        ".##.##.",
        "##.#.##",
        "##.#.##",
        "##.#.##",
        "##.#.##",
        "##.#.##",
        ".......",
    ),
    # 'u' curls back at its stems in the worm's own manner.
    "u": (
        ".......",
        "##...##",
        "##...##",
        "##...##",
        "##...##",
        "##...##",
        ".#####.",
        ".......",
    ),
    # 'n' mirrors the 'm' half.
    "n": (
        ".......",
        ".####..",
        "##..##.",
        "##..##.",
        "##..##.",
        "##..##.",
        "##..##.",
        ".......",
    ),
    # 'i' with a proper worm-red dot floating a pixel above its stem, so
    # the dot doesn't collide with the top of the row and still reads.
    "i": (
        "##.....",
        ".......",
        "##.....",
        "##.....",
        "##.....",
        "##.....",
        "##.....",
        ".......",
    ),
}

_WORM_TEXT = "muni"
_WORM_GLYPH_W = 7
_WORM_KERN = 1
WORM_WIDTH = len(_WORM_TEXT) * (_WORM_GLYPH_W + _WORM_KERN) - _WORM_KERN  # 31
WORM_HEIGHT = 8


def _worm_palette(dim: bool) -> dict[str, tuple[int, int, int]]:
    red = MUNI_RED_DIM if dim else MUNI_RED
    return {"#": red}


def draw_worm(canvas: Canvas, x: int, y: int, dim: bool = False) -> None:
    """Paint the 'muni' worm-style wordmark at ``(x, y)`` in Muni red.

    A helper rather than a private method so the tests and the preview
    script can exercise the mark in isolation from the mode's fetch loop.
    """
    palette = _worm_palette(dim)
    cursor = x
    for character in _WORM_TEXT:
        glyph = _WORM_GLYPHS[character]
        canvas.sprite(cursor, y, list(glyph), palette)
        cursor += _WORM_GLYPH_W + _WORM_KERN


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


class MuniMode(Mode):
    """Poll umoiq's Muni predictions and render the next few arrivals."""

    # Predictions are refreshed by the upstream feed roughly once a minute,
    # so 30 seconds is aggressive enough to catch a "5M" ticking to "4M"
    # without hammering a shared public endpoint.
    CACHE_SECONDS = 30
    ERROR_BACKOFF_SECONDS = 60

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.predictions: muni.Predictions | None = None
        self._stop_code = ""
        # Far enough in the past that the first render always fetches.
        self._last_refresh = -1e9
        self._failed = False

    def _refresh(self, stop_code: str) -> None:
        result = muni.lookup(stop_code)
        if result is None:
            self._failed = True
        else:
            self.predictions = result
            self._stop_code = stop_code
            self._failed = False
        self._last_refresh = time.monotonic()

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        stop_code = self.config.current_muni_stop()

        if not muni.is_stop_code(stop_code):
            canvas.text_centered(6, "MUNI", ERROR, SMALL)
            canvas.text_centered(18, "PICK A STOP", ERROR, SMALL)
            return

        # Switching stops must not wait out the cache, otherwise the board
        # would sit on the previous stop's arrivals for up to 30 seconds.
        due = self.CACHE_SECONDS if not self._failed else self.ERROR_BACKOFF_SECONDS
        if stop_code != self._stop_code or time.monotonic() - self._last_refresh >= due:
            self._refresh(stop_code)

        self._draw_header(canvas, stop_code, tick)

        if self.predictions is None:
            canvas.text_centered(18, "LOADING MUNI", (255, 200, 200), SMALL)
            return

        arrivals = self.predictions.arrivals[:3]
        if not arrivals:
            # Off-hours (owl service ends around 5am) or an unserved stop
            # code; both surface the same "no arrivals" state.
            canvas.text_centered(14, "NO ARRIVALS", AMBER, SMALL)
            canvas.text_centered(24, f"STOP {stop_code}", DIM, SMALL)
            return

        for row_y, arrival in zip(ROW_Y, arrivals):
            self._draw_arrival(canvas, row_y, arrival)

    def _draw_header(self, canvas: Canvas, stop_code: str, tick: int) -> None:
        """Muni worm on the left, stop label + clock filling the rest of the row.

        The stop name goes right after the worm; if there isn't room, the
        stop code takes its place because the number is what the rider
        actually needs to confirm they're at the right shelter.
        """
        draw_worm(canvas, 0, HEADER_Y, dim=False)

        # 12-hour clock trimmed to HH:MM: the AM/PM suffix eats seven pixels
        # that the stop label needs more, and the rider already knows what
        # part of day it is when they walk up to a bus stop.
        clock = _trim_clock(self.clock_text(tick))
        clock_width = canvas.text_width(clock, SMALL)
        clock_x = canvas.width - clock_width

        label_x = WORM_WIDTH + GAP
        label_width = clock_x - GAP - label_x
        stop = self.predictions.stop_name if self.predictions else ""
        if not stop:
            stop = f"STOP {stop_code}"
        else:
            stop = _panel_stop_name(stop)
        title = canvas.fit(stop, label_width, SMALL)
        canvas.text(label_x, HEADER_Y, title, DIM, SMALL)
        canvas.text(clock_x, HEADER_Y, clock, WHITE, SMALL)

    def _draw_arrival(self, canvas: Canvas, y: int, arrival: muni.Arrival) -> None:
        countdown = arrival.countdown()
        countdown_width = canvas.text_width(countdown, SMALL)
        # One-pixel inset from the bezel, same as the BART board.
        countdown_x = canvas.width - 1 - countdown_width

        route = arrival.route
        route_width = canvas.text_width(route, SMALL)
        canvas.text(LABEL_X, y, route, arrival.color, SMALL)

        dest_x = LABEL_X + route_width + GAP
        dest_width = countdown_x - GAP - dest_x
        destination = canvas.fit(arrival.destination.upper(), dest_width, SMALL)
        canvas.text(dest_x, y, destination, WHITE, SMALL)

        # NOW arrivals go amber like BART's "leaving" trains, so a rider
        # glancing between the two panels reads urgency the same way.
        color = AMBER if arrival.is_leaving else WHITE
        canvas.text(countdown_x, y, countdown, color, SMALL)


def _panel_stop_name(name: str) -> str:
    """Uppercased, cross-street shortened stop name for the panel header.

    NextBus stop names include a ``&`` between cross-streets; we keep the
    format but uppercase the whole thing so it matches the panel's other
    all-caps labels. Long names are handled by the canvas ``fit`` call at
    the render site — this is just the presentation transform.
    """
    return " ".join(name.split()).upper().replace(" & ", " & ")


def _trim_clock(text: str) -> str:
    """Drop a trailing 'AM'/'PM' suffix from the 12-hour clock text.

    The base ``clock_text`` returns strings like ``'1:07 PM'`` for the
    12-hour user preference; on a 128-pixel row the AM/PM suffix competes
    with the stop label for space. A 24-hour clock is already short and
    passes through unchanged.
    """
    upper = text.upper()
    if upper.endswith(" AM") or upper.endswith(" PM"):
        return text[:-3]
    return text


__all__ = [
    "MUNI_RED",
    "MuniMode",
    "WORM_HEIGHT",
    "WORM_WIDTH",
    "draw_worm",
]
