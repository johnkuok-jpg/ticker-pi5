# MIT License — Copyright (c) 2026 John Kuok
r"""World-clock mode: one big home dial + two secondary dials.

Layout on a 128x32 panel::

    +--------------------------------------------------------------+
    |     ___         SF        :        ___         ___           |
    |    /   \      9:15        :       /   \       /   \          |
    |    | + |                  :       | + |       | + |          |
    |    \___/                  :       \___/       \___/          |
    |                           :         NYC         LON          |
    +--------------------------------------------------------------+

The home city (first entry) sits in the left third as a large r=14 dial with
an amber rim and a two-colour hand set -- red for the hour, white for the
minute -- so the current hour reads at a glance while the minute stays
legible. The city name and digital time (H:MM) sit to the right of the big
dial. A dotted vertical rule at x=60 separates the home block from the two
smaller r=7 dials on the right, which render in cool grey with white hands
and a labelled name underneath.

Cities are configured via the webapp as a JSON list of ``{"label", "tz"}``
objects and persisted in ``state_dir/worldclock.json``. If the file is
missing or malformed, a sensible default (SF / NYC / LON) is used so the
mode is always renderable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from PIL import ImageDraw

from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
# Amber for the home dial rim keeps the mode consistent with the other
# accent-amber modes (stocks, market). The home hour hand switches to a vivid
# red so hour and minute never blur into one shape at a glance, and the
# minute stays plain white for maximum contrast against the amber rim.
AMBER = (255, 176, 0)
BRIGHT_AMBER = (255, 200, 40)
HOUR_RED = (255, 60, 60)
WHITE = (215, 225, 240)
SOFT_WHITE = (180, 190, 210)
RING_DIM = (72, 84, 106)
DIVIDER_DIM = (48, 58, 78)

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
# Big home dial on the left third.
HOME_CX = 15
HOME_CY = 15
HOME_R = 14
HOME_HOUR_LEN = 6   # short, so the red is unmistakable as the hour
HOME_MIN_LEN = 11   # nearly touches the rim so the minute reads first

# Digital readout to the right of the home dial.
HOME_TEXT_X = 32
HOME_LABEL_Y = 2
HOME_TIME_Y = 11

# Vertical dotted divider separates home block from secondary dials.
DIVIDER_X = 60

# Two secondary dials in the right two-thirds.
SECONDARY_CX_START = 76
SECONDARY_CX_STEP = 26
SECONDARY_CY = 12
SECONDARY_R = 7
SECONDARY_HOUR_LEN = 3
SECONDARY_MIN_LEN = 4
SECONDARY_LABEL_Y = 22

# Digital (H4) view geometry. Three equal 42-px slots, each stacking a small
# city label, a MEDIUM H:MM readout, and a small A/P suffix. Dotted vertical
# rules between slots echo the analog view's divider so both views feel like
# siblings rather than unrelated screens.
DIGITAL_SLOT_WIDTH = 42
DIGITAL_LABEL_Y = 2
DIGITAL_TIME_Y = 12
DIGITAL_SUFFIX_Y = 20
DIGITAL_DIVIDER_XS = (42, 85)

# Default city set. SF as home; NYC and LON round out US-East and Europe.
DEFAULT_CITIES: tuple[dict, ...] = (
    {"label": "SF", "tz": "America/Los_Angeles"},
    {"label": "NYC", "tz": "America/New_York"},
    {"label": "LON", "tz": "Europe/London"},
)


@dataclass(slots=True)
class ClockDial:
    """One city's clock, ready to draw."""

    label: str
    hour: int      # 0..23
    minute: int    # 0..59


def _city_now(tz_name: str, fallback: datetime) -> datetime:
    """Return the current time in *tz_name*, or the caller's fallback on error.

    A bogus IANA name (typo, missing tzdata) must not blank out the whole
    panel, so we degrade to the fallback -- which is the system clock in the
    user's own zone -- and let the label still render.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return fallback


def _draw_circle(canvas: Canvas, cx: int, cy: int, radius: int, color) -> None:
    """Draw a hollow one-pixel-thick circle onto the canvas buffer.

    Uses PIL's ``ellipse`` outline for a rounder rim than a hand-rolled
    Bresenham at small radii. ``fontmode='1'`` on the canvas ensures no
    sub-pixel anti-alias bleeds into neighbouring cells.
    """
    draw = ImageDraw.Draw(canvas.image_buffer)
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=color,
    )


def _draw_line(canvas: Canvas, x0: int, y0: int, x1: int, y1: int, color) -> None:
    """Bresenham line -- integer only, so the hand endpoints never smear."""
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        canvas.pixel(x0, y0, color)
        if x0 == x1 and y0 == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _hand_endpoint(cx: int, cy: int, length: int, angle_deg: float) -> tuple[int, int]:
    """Compute (x, y) for a clock hand of *length* pixels at *angle_deg*.

    Angle 0 is straight up (12 o'clock); positive rotates clockwise, matching
    a real analog clock. Rounded to the nearest pixel for stable rendering.
    """
    a = math.radians(angle_deg - 90.0)
    x = cx + int(round(length * math.cos(a)))
    y = cy + int(round(length * math.sin(a)))
    return x, y


def _draw_cardinal_ticks(canvas: Canvas, cx: int, cy: int, radius: int, color) -> None:
    """Bright pixels at 12/3/6/9 just inside the rim -- gives the dial identity."""
    for m in (0, 3, 6, 9):
        a = math.radians(m * 30 - 90)
        tx = cx + int(round((radius - 1) * math.cos(a)))
        ty = cy + int(round((radius - 1) * math.sin(a)))
        canvas.pixel(tx, ty, color)


def _draw_home_dial(canvas: Canvas, dial: ClockDial) -> None:
    """Big amber dial + digital readout to the right.

    Hand colour convention:
      - hour: red, short (r=6). The red is the "which hour" glance.
      - minute: white, long (r=11). The white is the "where in the hour" glance.
      - centre dot: amber, so the pivot merges into the rim colour rather than
        competing with either hand.
    """
    _draw_circle(canvas, HOME_CX, HOME_CY, HOME_R, AMBER)
    _draw_cardinal_ticks(canvas, HOME_CX, HOME_CY, HOME_R, BRIGHT_AMBER)

    # Minute first, hour on top -- when they overlap, the red hour wins,
    # which is the semantically important number to keep visible.
    minute_angle = dial.minute * 6.0
    hour_angle = (dial.hour % 12) * 30.0 + dial.minute * 0.5

    mx, my = _hand_endpoint(HOME_CX, HOME_CY, HOME_MIN_LEN, minute_angle)
    hx, hy = _hand_endpoint(HOME_CX, HOME_CY, HOME_HOUR_LEN, hour_angle)

    _draw_line(canvas, HOME_CX, HOME_CY, mx, my, WHITE)
    _draw_line(canvas, HOME_CX, HOME_CY, hx, hy, HOUR_RED)

    canvas.pixel(HOME_CX, HOME_CY, AMBER)

    # Digital readout: city label on top row, H:MM below, both left-aligned
    # against the divider so they read as a paired block.
    canvas.text(HOME_TEXT_X, HOME_LABEL_Y, dial.label, AMBER, SMALL)
    display_hour = dial.hour % 12 or 12
    time_str = f"{display_hour}:{dial.minute:02d}"
    canvas.text(HOME_TEXT_X, HOME_TIME_Y, time_str, WHITE, SMALL)


def _draw_divider(canvas: Canvas) -> None:
    """Dotted vertical rule between home block and secondary dials.

    Every-other-pixel dashing keeps the divider visible without stealing
    contrast from the dials or labels beside it.
    """
    for y in range(4, 28):
        if y % 2 == 0:
            canvas.pixel(DIVIDER_X, y, DIVIDER_DIM)


def _draw_secondary_dial(canvas: Canvas, cx: int, dial: ClockDial) -> None:
    """Small r=7 dial with white hands and a label underneath."""
    _draw_circle(canvas, cx, SECONDARY_CY, SECONDARY_R, RING_DIM)

    minute_angle = dial.minute * 6.0
    hour_angle = (dial.hour % 12) * 30.0 + dial.minute * 0.5

    mx, my = _hand_endpoint(cx, SECONDARY_CY, SECONDARY_MIN_LEN, minute_angle)
    hx, hy = _hand_endpoint(cx, SECONDARY_CY, SECONDARY_HOUR_LEN, hour_angle)

    _draw_line(canvas, cx, SECONDARY_CY, mx, my, WHITE)
    _draw_line(canvas, cx, SECONDARY_CY, hx, hy, WHITE)
    canvas.pixel(cx, SECONDARY_CY, WHITE)

    label_w = canvas.text_width(dial.label, SMALL)
    label_x = max(0, min(canvas.width - label_w, cx - label_w // 2))
    canvas.text(label_x, SECONDARY_LABEL_Y, dial.label, SOFT_WHITE, SMALL)


def _draw_digital_slot(
    canvas: Canvas,
    slot_index: int,
    dial: ClockDial,
) -> None:
    """Draw one 42-px column: city label, big H:MM, tiny A/P suffix.

    Home slot (index 0) gets the amber accent for label + time so the eye
    lands there first; the other two slots stay white/soft-white. The A/P
    letter is dim in both cases -- meridian is context, not headline data.
    """
    is_home = (slot_index == 0)
    label_color = AMBER if is_home else SOFT_WHITE
    time_color = AMBER if is_home else WHITE

    slot_cx = DIGITAL_SLOT_WIDTH * slot_index + DIGITAL_SLOT_WIDTH // 2

    # City label centred in the slot at the top.
    label_w = canvas.text_width(dial.label, SMALL)
    label_x = max(0, min(canvas.width - label_w, slot_cx - label_w // 2))
    canvas.text(label_x, DIGITAL_LABEL_Y, dial.label, label_color, SMALL)

    # MEDIUM H:MM followed by a SMALL A/P suffix, together centred as one unit
    # so the group visually reads as "one number" rather than two elements.
    display_hour = dial.hour % 12 or 12
    time_str = f"{display_hour}:{dial.minute:02d}"
    suffix = "A" if dial.hour < 12 else "P"

    time_w = canvas.text_width(time_str, MEDIUM)
    suffix_w = canvas.text_width(suffix, SMALL)
    group_w = time_w + 2 + suffix_w
    x = max(0, min(canvas.width - group_w, slot_cx - group_w // 2))

    canvas.text(x, DIGITAL_TIME_Y, time_str, time_color, MEDIUM)
    canvas.text(x + time_w + 2, DIGITAL_SUFFIX_Y, suffix, RING_DIM, SMALL)


def _draw_digital_dividers(canvas: Canvas) -> None:
    """Dotted vertical rules between the three digital slots.

    Kept sparse (every 3rd pixel) and short (y=10..22) so the dividers read
    as a subtle guide rather than another element competing with the times.
    They also don't collide with the top-row city labels or the bottom-row
    A/P suffix.
    """
    for x in DIGITAL_DIVIDER_XS:
        for y in range(10, 22, 3):
            canvas.pixel(x, y, DIVIDER_DIM)


class WorldClockMode(Mode):
    """World clock with two selectable views: analog (G3) or digital (H4).

    The view is chosen at render time from :meth:`Config.current_worldclock_view`
    so a webapp toggle can flip between layouts without a service restart.

    The mode is deliberately network-free -- it reads system time and does
    the timezone math with :mod:`zoneinfo`, so it keeps working when the WiFi
    is down. That is the same trust model as the market-session clock.
    """

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        cities = self.config.current_worldclock_cities()

        # Cap at 3. Both views are three-slot layouts; a fourth city would
        # require shrinking the home dial (analog) or the MEDIUM font
        # (digital) below the point of legibility.
        cities = list(cities)[:3]
        if not cities:
            cities = list(DEFAULT_CITIES)

        fallback = self.config.now()
        # Materialise per-city local times up front so the two view branches
        # below stay layout-only.
        dials: list[ClockDial] = []
        for city in cities:
            label = str(city.get("label", "")).strip() or "?"
            tz = str(city.get("tz", "")).strip()
            local = _city_now(tz, fallback) if tz else fallback
            dials.append(ClockDial(label=label, hour=local.hour, minute=local.minute))

        view = self.config.current_worldclock_view()
        if view == "digital":
            _draw_digital_dividers(canvas)
            for i, dial in enumerate(dials):
                _draw_digital_slot(canvas, i, dial)
            return

        # Analog view (default): G3 layout.
        _draw_home_dial(canvas, dials[0])
        _draw_divider(canvas)
        for i, dial in enumerate(dials[1:]):
            cx = SECONDARY_CX_START + i * SECONDARY_CX_STEP
            _draw_secondary_dial(canvas, cx, dial)
