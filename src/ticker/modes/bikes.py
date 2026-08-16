# MIT License — Copyright (c) 2026 John Kuok
"""Bay Wheels station availability — Lyft mark + mini bike, name, and counts.

Layout at 128x32 (a locked design; see docs/bikes-layout.md if it ever needs
revisiting):

    +--------- 26 -------+ +---------------- 102 -------------------+
    |   LYFT WORDMARK    | | station name row (y=0)                  |
    |   (pink, 23x16)    | | ⚡3   8    ▢12    (values row, y=12)   |
    |   pink bike icon   | | EBIKE BIKE DOCK   (labels row, y=22)    |
    +--------------------+ +----------------------------------------+

The three data columns are colour coded: BLUE for ebikes (the fast ones the
day trader will actually want), GREEN for classic bikes, DIM_GRAY for empty
docks (a return-trip cue, not a look-here cue).

The renderer never blocks: fetches happen on their own throttled clock and
the last known snapshot stays on screen while a refresh is in flight, so a
transient GBFS hiccup does not blank the panel.
"""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

from ticker import baywheels
from ticker.canvas import Canvas
from ticker.modes.base import Mode

# Colours are chosen for a 128x32 RGB LED panel: fully saturated primaries read
# best from across a room, and DIM_GRAY reads as "muted" without vanishing.
LYFT_PINK = (255, 0, 191)
WHITE = (255, 255, 255)
GREEN = (52, 199, 89)
BLUE = (10, 132, 255)
DIM_GRAY = (180, 180, 180)
RED = (255, 60, 60)

# Layout constants. Extracted rather than inlined so tests can import them.
LOGO_ZONE_WIDTH = 26
LOGO_HEIGHT = 16
LOGO_Y = 0            # Lyft wordmark hugs the top edge of the panel.
BIKE_Y = 20           # Mini bike sits below the wordmark with a 4-row gap.
RIGHT_ZONE_X = LOGO_ZONE_WIDTH + 3  # 29
COL2_X = RIGHT_ZONE_X + 30          # 59
COL3_X = RIGHT_ZONE_X + 62          # 91
NAME_ROW_Y = 0
VALUES_ROW_Y = 12
LABELS_ROW_Y = 22

# Bolt sprite. Hand-drawn to sit inside a 5-wide advance so the digit that
# follows lands one character-column to the right, matching the 5x8 spleen
# grid. Any wider and it would bump the digit off the column.
_BOLT_ROWS = [
    "...##",
    "..##.",
    ".##..",
    "#####",
    "..###",
    ".##..",
    "##...",
    "#....",
]

# Mini bike silhouette below the wordmark. This is a 14x10 nearest-neighbour
# downsample of lyft.png -- same shape, roughly half the pixel area, so it
# reads as "the same bike, smaller" rather than a different mark. Facing
# RIGHT (seat/rider on the left, handlebars up on the right, front wheel on
# the right, rear wheel on the left), matching the parent logo.
_BIKE_ROWS = [
    "##.......###..",
    "##......#####.",
    "##########.###",
    "##########.###",
    "#############.",
    "#############.",
    "##########..##",
    ".#.######...##",
    "...####.......",
    "...###........",
]

# Logo path is package-relative so the module works whether Python imports
# it from the checkout, a wheel, or the systemd service.
_LOGO_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "logos" / "lyft.png"


class BikesMode(Mode):
    """Live Bay Wheels station availability."""

    #: How often to poll GBFS. The status feed itself refreshes about once a
    #: minute; polling more often costs bandwidth without gaining detail.
    POLL_SECONDS = 45
    #: Backoff after a poll that returned nothing usable. A configured station
    #: id that GBFS does not know will never start working, so it is retried
    #: slowly rather than every :attr:`POLL_SECONDS`.
    MISS_SECONDS = 5 * 60

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._station_id = ""
        self._station: baywheels.Station | None = None
        self._checked = -1e9
        self._missing = False
        self._logo: Image.Image | None = None

    # ---------------------------------------------------------------- helpers

    def _load_logo(self) -> Image.Image | None:
        """The Lyft bike silhouette, cached after the first successful load."""
        if self._logo is not None:
            return self._logo
        try:
            self._logo = Image.open(_LOGO_PATH).convert("RGBA")
        except (OSError, FileNotFoundError):
            self._logo = None
        return self._logo

    def _refresh(self, station_id: str) -> None:
        """Fetch or refresh the station snapshot, respecting cache windows."""
        now = time.monotonic()
        interval = self.MISS_SECONDS if self._missing else self.POLL_SECONDS
        if station_id != self._station_id:
            # A change from the web app means "look now"; do not honour the
            # backoff from the previous station's failures.
            self._station_id = station_id
            self._station = None
            self._checked = -1e9
            self._missing = False
        if now - self._checked < interval:
            return
        try:
            snapshot = baywheels.fetch_station(station_id)
        except Exception:
            snapshot = None
        if snapshot is not None:
            self._station = snapshot
            self._missing = False
        elif self._station is None:
            # Only flip to "missing" when we truly have nothing to show. Losing
            # network for a few seconds should not blank a working station.
            self._missing = True
        self._checked = now

    def _draw_bolt(self, canvas: Canvas, x: int, y: int, color: tuple[int, int, int]) -> None:
        canvas.sprite(x, y, _BOLT_ROWS, {"#": color})

    def _draw_dock_icon(self, canvas: Canvas, x: int, y: int, color: tuple[int, int, int]) -> None:
        """5x5 outlined square centred in the 8-row band, matching text height."""
        top = y + 2
        bottom = y + 6
        canvas.hline(top, color, x, x + 5)
        canvas.hline(bottom, color, x, x + 5)
        canvas.vline(x, color, top, bottom + 1)
        canvas.vline(x + 4, color, top, bottom + 1)

    def _fit_name(self, name: str) -> str:
        """Shorten the station name to what fits in the right zone.

        Bay Wheels station names run long ("Powell St BART Station (Market St
        at 4th St)") and 5x8 Spleen fits 20 chars in the 102px zone. Truncating
        with an ellipsis reads better than clipping mid-word.
        """
        max_chars = 20  # 102 // 5 = 20 with room to spare
        cleaned = name.strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 1] + "…"

    # ---------------------------------------------------------------- render

    def render(self, canvas: Canvas, tick: int) -> None:
        # Prefer the web-app selection, falling back to the .env default. This
        # matches how the other modes (flights, bart) resolve their target.
        station_id = self.config.current_bike_station()
        if not station_id:
            canvas.text_centered(4, "No station picked", DIM_GRAY, 8)
            canvas.text_centered(16, "Pick one in the web app", DIM_GRAY, 8)
            return

        self._refresh(station_id)

        # Lyft wordmark at the top of the left zone.
        logo = self._load_logo()
        if logo is not None:
            lx = (LOGO_ZONE_WIDTH - logo.width) // 2
            canvas.image(lx, LOGO_Y, logo)

        # Mini bike silhouette below the wordmark, horizontally centred.
        bike_w = len(_BIKE_ROWS[0])
        bx = (LOGO_ZONE_WIDTH - bike_w) // 2
        canvas.sprite(bx, BIKE_Y, _BIKE_ROWS, {"#": LYFT_PINK})

        if self._station is None:
            # Two-line notice sitting where the counts normally go, so the
            # logo alone does not sit next to an empty pink void.
            canvas.text(RIGHT_ZONE_X, NAME_ROW_Y, self._fit_name(f"ID {station_id}"), WHITE, 8)
            if self._missing:
                canvas.text(RIGHT_ZONE_X, VALUES_ROW_Y, "Not in feed", RED, 8)
                canvas.text(RIGHT_ZONE_X, LABELS_ROW_Y, "Check ID", DIM_GRAY, 8)
            else:
                canvas.text(RIGHT_ZONE_X, VALUES_ROW_Y, "Loading...", DIM_GRAY, 8)
            return

        station = self._station

        # Station name across the top of the right zone.
        canvas.text(RIGHT_ZONE_X, NAME_ROW_Y, self._fit_name(station.name), WHITE, 8)

        # ---- values row (y=12) --------------------------------------------
        # E-bikes: bolt sprite in a 5-wide column, then the count one column
        # to the right, so the digit starts at RIGHT_ZONE_X + 7 (matches the
        # locked layout drawing exactly).
        self._draw_bolt(canvas, RIGHT_ZONE_X, VALUES_ROW_Y, BLUE)
        canvas.text(RIGHT_ZONE_X + 7, VALUES_ROW_Y, _format_count(station.ebikes), BLUE, 8)

        # Classic bikes: no icon, just the number. Green is enough of a hint.
        canvas.text(COL2_X, VALUES_ROW_Y, _format_count(station.classic_bikes), GREEN, 8)

        # Docks: 5x5 outlined-square icon, then the number one column right.
        self._draw_dock_icon(canvas, COL3_X, VALUES_ROW_Y, DIM_GRAY)
        canvas.text(COL3_X + 6, VALUES_ROW_Y, _format_count(station.docks), DIM_GRAY, 8)

        # ---- labels row (y=22) --------------------------------------------
        canvas.text(RIGHT_ZONE_X, LABELS_ROW_Y, "EBIKE", BLUE, 8)
        canvas.text(COL2_X, LABELS_ROW_Y, "BIKE", GREEN, 8)
        canvas.text(COL3_X, LABELS_ROW_Y, "DOCK", DIM_GRAY, 8)

        # If Lyft has taken the station offline (e.g. relocating), grey the
        # counts with a small "OFFLINE" strip. This is rare enough not to
        # deserve a full layout change, but frequent enough to be worth
        # showing when it happens.
        if not station.is_renting:
            canvas.fill_rect(RIGHT_ZONE_X, LABELS_ROW_Y, 102, 8, (30, 30, 30))
            canvas.text(RIGHT_ZONE_X, LABELS_ROW_Y, "STATION OFFLINE", RED, 8)


def _format_count(value: int) -> str:
    """Two-digit width max, so a column doesn't need to reflow at 100+.

    A Bay Wheels station never exceeds low-tens capacity, but GBFS technically
    lets any integer through; capping at "99" makes the column safe without
    changing the everyday case.
    """
    if value < 0:
        return "0"
    if value > 99:
        return "99"
    return str(value)
