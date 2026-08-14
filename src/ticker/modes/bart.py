# MIT License — Copyright (c) 2026 John Kuok
"""Live BART departure board for one station.

Three trains, soonest first, in their line colours - the same information a
platform sign gives and in the same order. The station is chosen from the web
app, so the panel can follow whoever is about to walk out the door.

Destinations are drawn in the line colour rather than white with a colour chip
beside them: at five pixels wide a chip is easy to miss from across a room,
while a whole word in Yellow-line yellow is not. Every colour in
``ticker.bart.LINE_COLORS`` is tuned to clear the panel's legibility floor, so
this costs nothing in readability.
"""

from __future__ import annotations

import time

from ticker import bart, icons
from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode

WHITE = (235, 240, 250)
DIM = (108, 122, 148)
AMBER = (255, 190, 60)
# Car count is drawn in the departure's own line colour rather than a neutral
# grey, which groups it with the destination it describes. That matters because
# the platform sits immediately to its right in neutral grey: without the shared
# hue "10 CAR 1" reads as a single field, and the digit looks like part of the
# count. Colour does that separating, so no "P" prefix is needed on the platform.
#
# Scaled down because it is still the lowest-priority thing on the row and a
# saturated line colour at full strength outshouts the destination. Kept well
# above a token dimming so it does not become the first thing to vanish at the
# 20% night brightness step.
CARS_TINT = 0.55
ERROR = (255, 150, 60)

HEADER_Y = 0
ROW_Y = (8, 16, 24)
LABEL_X = 0
GAP = 3
CARS_GAP = 6

# The train sits in the header's spare pixels: no station name comes close to
# filling the row, so the mode is identifiable at a glance from across the room
# without costing a single character. Verified against every station name.
ICON_X = 0
ICON_Y = 1
ICON_WIDTH = len(icons.TRAIN[0])
TITLE_X = ICON_WIDTH + GAP


def _cars_color(line_color: tuple[int, int, int]) -> tuple[int, int, int]:
    """The car count's colour: the line's own hue, scaled back.

    Scaling every channel by the same factor keeps the hue exactly, so the count
    still reads as belonging to the destination rather than looking like a
    separate muddy colour.
    """
    return tuple(round(channel * CARS_TINT) for channel in line_color)  # type: ignore[return-value]


def _rider_message(raw: str) -> str:
    """Turn BART's advisory field into something a rider would want to read.

    The endpoint answers an empty platform with "No data matched your criteria",
    which is a developer's sentence, not a sign's. Genuine advisories are passed
    through untouched.
    """

    text = " ".join(raw.split()).upper().rstrip(".")
    if not text or text.startswith("NO DATA MATCHED"):
        return "NO TRAINS RUNNING"
    return text


def _wrap(canvas: Canvas, text: str, width: int, font: int, limit: int = 2) -> list[str]:
    """Break text on word boundaries, so an advisory never stops mid-word."""

    words = text.split()
    lines: list[str] = []
    current = ""
    for index, word in enumerate(words):
        candidate = f"{current} {word}".strip()
        if current and canvas.text_width(candidate, font) > width:
            lines.append(current)
            current = word
            if len(lines) == limit - 1:
                # Last row available: pour everything left into it and let the
                # cut fall there rather than silently dropping words.
                current = " ".join(words[index:])
                break
        else:
            current = candidate
    if current:
        lines.append(current)
    return [canvas.fit(line, width, font) for line in lines[:limit]] or [""]


class BartMode(Mode):
    """Poll BART's ETD endpoint and render the next few trains."""

    # Real-time data with a countdown in whole minutes: refreshing every 20
    # seconds keeps the top row honest without hammering a free public endpoint.
    CACHE_SECONDS = 20
    ERROR_BACKOFF_SECONDS = 60

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.board: bart.Board | None = None
        self._station = ""
        # Far enough in the past that the first render always fetches, without
        # depending on when the process happened to start.
        self._last_refresh = -1e9
        self._failed = False

    def _refresh(self, station: str) -> None:
        board = bart.lookup(station)
        if board is None:
            self._failed = True
        else:
            self.board = board
            self._station = station
            self._failed = False
        self._last_refresh = time.monotonic()

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        station = self.config.current_bart_station()

        if not bart.is_station(station):
            canvas.text_centered(6, "BART", ERROR, SMALL)
            canvas.text_centered(18, "PICK A STATION", ERROR, SMALL)
            return

        # Switching stations must not wait out the cache, otherwise the board
        # would sit on the previous station's trains for up to 20 seconds.
        due = self.CACHE_SECONDS if not self._failed else self.ERROR_BACKOFF_SECONDS
        if station != self._station or time.monotonic() - self._last_refresh >= due:
            self._refresh(station)

        clock = self.clock_text(tick)
        clock_width = canvas.text_width(clock, SMALL)
        clock_x = canvas.width - clock_width
        canvas.sprite(ICON_X, ICON_Y, icons.TRAIN, icons.TRAIN_PALETTE)
        title = canvas.fit(bart.panel_name(station), clock_x - GAP - TITLE_X, SMALL)
        canvas.text(TITLE_X, HEADER_Y, title, DIM, SMALL)
        canvas.text(clock_x, HEADER_Y, clock, WHITE, SMALL)

        if self.board is None:
            canvas.text_centered(16, "LOADING BART", (130, 180, 255), SMALL)
            return

        departures = self.board.departures
        if not departures:
            lines = _wrap(canvas, _rider_message(self.board.message), canvas.width - 2, SMALL)
            rows = (14,) if len(lines) == 1 else (11, 21)
            for row_y, line in zip(rows, lines):
                canvas.text_centered(row_y, line, AMBER, SMALL)
            return

        for row_y, departure in zip(ROW_Y, departures):
            self._draw_departure(canvas, row_y, departure)

    def _draw_departure(self, canvas: Canvas, y: int, departure: bart.Departure) -> None:
        countdown = departure.countdown()
        countdown_width = canvas.text_width(countdown, SMALL)
        # One pixel of inset: on a real panel the rightmost column sits against
        # the bezel, where a glyph edge is easy to lose.
        countdown_x = canvas.width - 1 - countdown_width

        platform = departure.platform
        platform_x = countdown_x
        if platform:
            platform_width = canvas.text_width(platform, SMALL)
            platform_x = countdown_x - GAP - platform_width
            canvas.text(platform_x, y, platform, DIM, SMALL)

        # Car count. BART runs anything from five to ten cars, and a short train
        # only covers part of the platform, so it changes where you stand. Spelled
        # out as "10 CAR" the way station signage reads, which also stops it being
        # mistaken for a second platform number. A zero means the feed omitted the
        # field, so draw nothing and give the space back to the destination.
        cars_x = platform_x
        if departure.cars > 0:
            cars = f"{departure.cars} CAR"
            # A wider gap here than elsewhere: the count and the platform are both
            # short and numeric, and three pixels apart they read as one run.
            cars_x = platform_x - CARS_GAP - canvas.text_width(cars, SMALL)
            canvas.text(cars_x, y, cars, _cars_color(departure.color), SMALL)

        label = canvas.fit(departure.label, cars_x - LABEL_X - GAP, SMALL)
        canvas.text(LABEL_X, y, label, departure.color, SMALL)
        canvas.text(countdown_x, y, countdown, AMBER if departure.is_delayed else WHITE, SMALL)
