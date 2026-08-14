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
# left-to-right progress bar.
#
# Eleven wide, because the two things that make the shape legible at this size
# both need horizontal room: wings swept back far enough to state a direction on
# their own, and a tailplane separated from the wing by bare fuselage. The
# earlier nine-wide sprite had neither and read as a cross with a dash through
# it. Height is the opposite trade -- a nine-row wing reads spidery, so the
# outermost row is shaved off each wing, leaving seven.
PLANE_PALETTE: dict[str, Color] = {"P": (240, 245, 255)}

PLANE_RIGHT = [
    "...PP......",
    "....PP.....",
    "P....PP....",
    "PPPPPPPPPPP",
    "P....PP....",
    "....PP.....",
    "...PP......",
]


def arrow_for(change: float) -> list[str]:
    """Pick the arrow glyph for a signed change."""
    if change > 0:
        return ARROW_UP
    if change < 0:
        return ARROW_DOWN
    return ARROW_FLAT


# A train for the BART header, seen head on. Not the BART logo: that is a
# trademarked wordmark, and at seven pixels tall it would be an illegible smudge.
#
# An earlier version of this was a side view, which is the easier drawing but
# reads as a bus. Head on changes the proportions -- a real BART car is about
# 10ft wide and 12ft tall, so the sprite has to be roughly square rather than the
# letterbox a side view wants, and eight columns is as wide as it can go before
# it looks squat.
#
# The two headlights carry the whole read. Nothing else on this panel is a pair
# of small amber dots low in a lit rectangle, so they are what the eye resolves
# first, and they are inset a pixel rather than pushed to the body edge so they
# stay legible against the dark background instead of bleeding off it. Earlier
# attempts spent rows on a dark roof edge and a dark skirt, which just shaved a
# 7-row sprite down to a 5-row blob; the background is the shadow instead.
#
# The body is silver, not white. White made the icon the brightest thing in the
# header, which put a mode badge ahead of the station name and the clock; silver
# sits between the two.
TRAIN_PALETTE: dict[str, Color] = {
    "W": (176, 188, 208),  # car body, BART silver
    "B": (52, 104, 190),   # windshield, dark enough to read as a hole
    "Y": (255, 214, 120),  # headlights
}

TRAIN = [
    ".WWWWWW.",
    "WWWWWWWW",
    "WBBBBBBW",
    "WBBBBBBW",
    "WWWWWWWW",
    "WYWWWWYW",
    "WWWWWWWW",
]


# Wi-Fi, 8x7 -- deliberately the same box as TRAIN so the network header and the
# BART header use identical geometry and the title column does not shift between
# modes.
#
# Three expanding chevrons over a detached dot. The chevrons are drawn as a
# crown and its legs on separate rows rather than as true curves: at this size an
# antialiasing-free arc is two pixels of guesswork, and the stepped version reads
# as concentric because the widths are what carry the meaning, not the curvature.
# The dot is separated by a blank row, which is what stops the innermost chevron
# from merging with it into a single blob.
WIFI_PALETTE: dict[str, Color] = {"W": (235, 240, 250)}

WIFI = [
    ".WWWWWW.",
    "W......W",
    "..WWWW..",
    ".W....W.",
    "...WW...",
    "........",
    "...WW...",
]
