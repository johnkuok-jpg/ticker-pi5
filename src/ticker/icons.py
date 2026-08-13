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
