# MIT License — Copyright (c) 2026 John Kuok
"""World-clock mode: three analog clocks with city labels underneath.

Layout on a 128x32 panel:

    +--------------------------------------------------------------+
    |       (o)                (o)                (o)              |   rows 1..19
    |      SF                 NYC                LON                |   row 23
    +--------------------------------------------------------------+

The panel is divided into three equal 42-pixel slots. Each slot centres a
19-pixel-wide analog clock face and a SMALL label beneath. The user's "home"
city (first in the list) renders in amber; the other two render in a cool
grey so the eye latches on to home first, then reads across.

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

from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode

# Palette. Amber matches the existing "labels and non-semantic accents"
# convention (stocks/market modes both use 255,176,0). Steel greys are picked
# to sit visibly against black without competing with amber for attention.
AMBER = (255, 176, 0)
WHITE = (215, 225, 240)
RING_DIM = (48, 58, 78)
LABEL_DIM = (140, 150, 170)

# Geometry -- centralised so a future 64x32 or 128x64 panel could re-derive
# without hunting through the drawing code.
CLOCK_RADIUS = 9      # gives a 19-px face (radius + centre + radius)
CLOCK_CY = 10         # centre y; face spans y=1..19
LABEL_Y = 23          # top pixel of the SMALL label row (SMALL is 8 tall)

# Default city set. Chosen for the three markets the user actually cares
# about (SF as home, NYC for US markets, LON for European tape). Kept
# module-level so the webapp can echo it back as a default.
DEFAULT_CITIES: tuple[dict, ...] = (
    {"label": "SF", "tz": "America/Los_Angeles"},
    {"label": "NYC", "tz": "America/New_York"},
    {"label": "LON", "tz": "Europe/London"},
)


@dataclass(slots=True)
class ClockDial:
    """One city's clock, ready to draw.

    Split out so the render loop reads as "for each dial: draw face,
    draw hands, draw label" without recomputing angles inline.
    """

    label: str
    hour: int      # 0..23
    minute: int    # 0..59
    is_home: bool  # first entry gets the accent treatment


def _city_now(tz_name: str, fallback: datetime) -> datetime:
    """Return the current time in *tz_name*, or the caller's fallback on error.

    A bogus IANA name (typo, missing tzdata) must not blank out the whole
    panel, so we degrade to the fallback -- which is the system clock in the
    user's own zone -- and let the label still render. That's less confusing
    than a blank slot.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return fallback


def _draw_circle(canvas: Canvas, cx: int, cy: int, radius: int, color) -> None:
    """Draw a hollow one-pixel-thick circle onto the canvas buffer.

    Uses PIL's ``ellipse`` outline for a rounder rim than a hand-rolled
    Bresenham at this small radius. ``fontmode='1'`` on the canvas ensures no
    sub-pixel anti-alias bleeds into neighbouring cells, which would show up
    on the LED panel as ghost pixels.
    """
    draw = ImageDraw.Draw(canvas.image_buffer)
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=color,
    )


def _draw_line(canvas: Canvas, x0: int, y0: int, x1: int, y1: int, color) -> None:
    """Bresenham line -- integer only, so the hand endpoints never smear.

    Used for both hour and minute hands. A thicker hand would look better in
    the abstract but at r=9 there is no room for a second pixel of width
    without eating the ticks.
    """
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


def _draw_dial(canvas: Canvas, cx: int, cy: int, dial: ClockDial) -> None:
    """Draw one analog dial: rim, hands, centre dot, and its label below.

    The home dial gets amber for the rim, both hands, and the centre dot so
    the accent is unmistakable at a glance. Non-home dials are a cool grey
    against a slightly cooler-still rim, so they read as clearly "secondary"
    without disappearing.
    """
    ring_color = AMBER if dial.is_home else RING_DIM
    hand_color = AMBER if dial.is_home else WHITE
    label_color = AMBER if dial.is_home else LABEL_DIM

    _draw_circle(canvas, cx, cy, CLOCK_RADIUS, ring_color)

    # 12 = 0deg (up), sweep clockwise. Subtract 90 to align "0" with the top.
    #
    # Hour hand advances 30deg/hour + 0.5deg/minute so a clock at 4:30 shows
    # the hand halfway between 4 and 5, not stuck at exactly 4.
    hour_angle = math.radians((dial.hour % 12) * 30 + dial.minute * 0.5 - 90)
    minute_angle = math.radians(dial.minute * 6 - 90)

    # Hand lengths tuned so both hands are visually distinct at r=9 but
    # neither touches the rim (the rim would visually absorb the tip).
    hour_len = CLOCK_RADIUS - 5    # 4px hour hand
    minute_len = CLOCK_RADIUS - 3  # 6px minute hand

    hx = cx + int(round(hour_len * math.cos(hour_angle)))
    hy = cy + int(round(hour_len * math.sin(hour_angle)))
    mx = cx + int(round(minute_len * math.cos(minute_angle)))
    my = cy + int(round(minute_len * math.sin(minute_angle)))

    _draw_line(canvas, cx, cy, hx, hy, hand_color)
    _draw_line(canvas, cx, cy, mx, my, hand_color)

    # Centre dot brighter than the rim so the pivot point reads as "solid"
    # rather than as a hole in the middle of the face.
    canvas.pixel(cx, cy, hand_color)

    # Label centred under the dial, clamped to the panel edges so a 4-char
    # city name near the right edge does not spill off pixel 127.
    label_w = canvas.text_width(dial.label, SMALL)
    label_x = max(0, min(canvas.width - label_w, cx - label_w // 2))
    canvas.text(label_x, LABEL_Y, dial.label, label_color, SMALL)


class WorldClockMode(Mode):
    """Three analog clocks side by side with city labels.

    The mode is deliberately network-free -- it reads system time and does
    the timezone math with :mod:`zoneinfo`, so it keeps working when the WiFi
    is down. That's the same trust model as the market-session clock.
    """

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        cities = self.config.current_worldclock_cities()

        # Cap at 3 (layout is 3-slot). A 4-clock layout would need r=7 or
        # smaller which loses too much detail at 32px tall.
        cities = list(cities)[:3]
        if not cities:
            cities = list(DEFAULT_CITIES)

        fallback = self.config.now()
        slot_w = canvas.width // 3

        for i, city in enumerate(cities):
            label = str(city.get("label", "")).strip() or "?"
            tz = str(city.get("tz", "")).strip()
            local = _city_now(tz, fallback) if tz else fallback
            cx = slot_w * i + slot_w // 2
            dial = ClockDial(
                label=label,
                hour=local.hour,
                minute=local.minute,
                is_home=(i == 0),
            )
            _draw_dial(canvas, cx, CLOCK_CY, dial)
