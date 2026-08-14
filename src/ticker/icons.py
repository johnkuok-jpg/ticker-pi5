# MIT License — Copyright (c) 2026 John Kuok
"""Hand-plotted 12x12 weather glyphs.

Emoji fonts are colour bitmap fonts that PIL cannot composite through the plain
bitmap-font path, and at 12 pixels an emoji would be an unreadable smudge
anyway. These are drawn pixel by pixel instead, so every lit LED is deliberate.

Each glyph is 12 rows of 12 characters. A dot leaves the LED dark; any other
character indexes PALETTE.
"""

from __future__ import annotations

from typing import TypeAlias

Color: TypeAlias = tuple[int, int, int]

PALETTE: dict[str, Color] = {
    "Y": (255, 200, 40),  # sun body
    "O": (255, 140, 25),  # sun rays
    "M": (235, 240, 205),  # moon
    "W": (225, 232, 245),  # cloud, lit top
    "G": (120, 138, 168),  # cloud, shaded underside
    "B": (80, 165, 255),  # rain
    "C": (195, 235, 255),  # snow
    "L": (255, 235, 90),  # lightning
}

SUN = [
    ".....YY.....",
    "..O..YY..O..",
    "...O....O...",
    "....YYYY....",
    "...YYYYYY...",
    ".O.YYYYYY.O.",
    ".O.YYYYYY.O.",
    "...YYYYYY...",
    "....YYYY....",
    "...O....O...",
    "..O..YY..O..",
    ".....YY.....",
]

# A thick crescent: a full disc with a second disc subtracted five pixels to the
# right. A thin outline read as a bracket rather than a moon.
MOON = [
    "....MMMM....",
    "..MMMMMM..M.",
    ".MMMMMM.....",
    ".MMMMM......",
    "MMMMMM......",
    "MMMMM.......",
    "MMMMM.......",
    "MMMMMM...M..",
    ".MMMMM......",
    ".MMMMMM.....",
    "..MMMMMM....",
    "....MMMM....",
]

CLOUD = [
    "............",
    "............",
    "............",
    "....WWWW....",
    "..WWWWWWWW..",
    ".WWWWWWWWWW.",
    "WWWWWWWWWWWW",
    "WWWWWWWWWWWW",
    ".GGGGGGGGGG.",
    "............",
    "............",
    "............",
]

# The sun disc here is deliberately as large as the standalone SUN's, because a
# 3x3 sun peeking out from behind the cloud vanished at panel scale.
PARTLY_CLOUDY = [
    "....O.......",
    "...YYY......",
    "..YYYYY.....",
    "O.YYYYY.....",
    "..YYYYY.....",
    "...YYY.WWW..",
    ".O...WWWWWWW",
    "....WWWWWWWW",
    "...WWWWWWWWW",
    "...GGGGGGGGG",
    "............",
    "............",
]

RAIN = [
    "............",
    "....WWWW....",
    "..WWWWWWWW..",
    ".WWWWWWWWWW.",
    "WWWWWWWWWWWW",
    ".GGGGGGGGGG.",
    "............",
    "..B..B..B...",
    "..B..B..B...",
    ".B..B..B....",
    ".B..B..B....",
    "............",
]

SNOW = [
    "............",
    "....WWWW....",
    "..WWWWWWWW..",
    ".WWWWWWWWWW.",
    "WWWWWWWWWWWW",
    ".GGGGGGGGGG.",
    "............",
    ".C..C..C....",
    "............",
    "...C..C..C..",
    "............",
    ".C..C..C....",
]

THUNDER = [
    "............",
    "....WWWW....",
    "..WWWWWWWW..",
    ".WWWWWWWWWW.",
    "WWWWWWWWWWWW",
    ".GGGGGGGGGG.",
    "......LL....",
    ".....LL.....",
    "...LLLLL....",
    ".....LL.....",
    "....LL......",
    "...LL.......",
]

FOG = [
    "............",
    "............",
    "..GGGGGGGG..",
    "............",
    ".GGGGGGGGGG.",
    "............",
    "..GGGGGGGG..",
    "............",
    ".GGGGGGGGGG.",
    "............",
    "............",
    "............",
]

# Three gusts, each ending in an upward hook. Detached curl pixels read as
# stray noise, so every hook stays connected to its line.
WIND = [
    "............",
    "............",
    "........W...",
    ".WWWWWWWW...",
    "............",
    "..........W.",
    "WWWWWWWWWWW.",
    "............",
    ".......W....",
    "..WWWWWW....",
    "............",
    "............",
]

# Longest, most specific phrases first: "Mostly Sunny" must not match "sunny"
# before it matches the partly-cloudy case, and "Chance Rain Showers" must not
# fall through to the clear-sky default.
_RULES: list[tuple[tuple[str, ...], list[str]]] = [
    (("thunder", "tstorm"), THUNDER),
    (("snow", "sleet", "flurr", "wintry", "ice", "freezing", "blizzard"), SNOW),
    (("rain", "shower", "drizzle", "precipitation"), RAIN),
    (("fog", "haze", "mist", "smoke"), FOG),
    (("partly sunny", "mostly sunny", "partly cloudy", "scattered clouds"), PARTLY_CLOUDY),
    (("mostly cloudy", "overcast", "cloudy", "clouds"), CLOUD),
    (("windy", "breezy", "blustery"), WIND),
]


def for_condition(condition: str, is_daytime: bool = True) -> list[str]:
    """Pick a glyph for an NWS ``shortForecast`` string.

    Clear skies resolve to the sun during the day and the moon at night, so the
    panel does not show a blazing sun at midnight.
    """
    text = (condition or "").lower()
    for keywords, glyph in _RULES:
        if any(keyword in text for keyword in keywords):
            return glyph
    return SUN if is_daytime else MOON


# --- Market direction arrows -------------------------------------------------
#
# Solid triangles rather than stemmed arrows: at five pixels wide a stem steals
# rows from the head and the glyph stops reading as a direction. Three rows tall
# so an arrow sits inside an 8-pixel text row without crowding it.

ARROW_PALETTE: dict[str, Color] = {
    "U": (40, 230, 90),  # up, green
    "D": (255, 70, 70),  # down, red
    "F": (255, 176, 0),  # flat, amber
}

ARROW_UP = [
    "..U..",
    ".UUU.",
    "UUUUU",
]

ARROW_DOWN = [
    "DDDDD",
    ".DDD.",
    "..D..",
]

ARROW_FLAT = [
    ".....",
    "FFFFF",
    ".....",
]


# Plan view, nose to the right, so the sprite reads as travelling along a
# left-to-right progress bar. Nine columns is the narrowest that still fits a
# swept wing and a separate tailplane: at seven the wing and tail merge and the
# whole thing reads as a plus sign rather than an aircraft.
PLANE_PALETTE: dict[str, Color] = {"P": (240, 245, 255), "p": (150, 165, 195)}

PLANE_RIGHT = [
    "...P.....",
    "...P.....",
    "..PPP....",
    "PPPPPPPPP",
    "..PPPPP..",
    "...P.....",
    "..PPP....",
]


def arrow_for(change: float) -> list[str]:
    """Pick the arrow glyph for a signed change."""
    if change > 0:
        return ARROW_UP
    if change < 0:
        return ARROW_DOWN
    return ARROW_FLAT


# A ten-pixel train for the BART header. Not the BART logo: that is a
# trademarked wordmark, and at seven pixels tall it would be an illegible smudge.
# Width is what makes this read as rail rather than road -- earlier seven-pixel
# attempts all looked like a bus, because a body that short is square. The window
# strip and the rail line underneath do the rest of the work.
TRAIN_PALETTE: dict[str, Color] = {
    "B": (150, 190, 255),  # car body
    "W": (235, 240, 250),  # windows, the brightest element
    "D": (40, 60, 100),    # trucks under the body
    "R": (108, 122, 148),  # rail, matched to the header text so it recedes
}

TRAIN = [
    "..........",
    ".BBBBBBBB.",
    "BBBBBBBBBB",
    "BWWBWWBWWB",
    "BBBBBBBBBB",
    ".D.D..D.D.",
    "RRRRRRRRRR",
]
