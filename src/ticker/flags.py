# MIT License — Copyright (c) 2026 John Kuok
"""Pixel-art country/region flags for the currency mode.

Each flag is a 12x8 sprite designed to sit next to a MEDIUM-font row: 12
pixels of width give a proportional 3:2 aspect and 8 pixels of height fits
the row's cap-to-baseline cleanly. Flags with intricate central emblems
(the CNY sun-stars, the JPY disc, the CHF cross, the CAD leaf) are drawn as
schematic marks whose colour and layout survive the panel's resolution;
literal reproduction at 12x8 would smear.

Every flag is a ``rows`` list of 12-character strings and a ``palette`` dict
mapping single-character keys to RGB triples. Both are consumed by
``Canvas.sprite`` unchanged.

Currency-to-flag mapping keys on the ISO 4217 currency code so the caller
does not have to know which country prints it. The euro maps to a stylised
EU roundel; there is no single country flag for it.
"""

from __future__ import annotations

from typing import TypeAlias

Color: TypeAlias = tuple[int, int, int]

FLAG_WIDTH = 12
FLAG_HEIGHT = 8

# --- Shared colours ---------------------------------------------------------
# Flag colours are tuned toward LED-panel brightness: each hex value is
# pulled a shade lighter than the print/web reference so a dark navy or a
# deep red does not vanish at the 20% night brightness step.

_WHITE = (240, 240, 240)
_BLACK = (18, 18, 18)
_RED = (235, 50, 50)
_DEEP_RED = (200, 30, 40)
_BLUE = (30, 90, 220)
_DEEP_BLUE = (0, 40, 160)
_YELLOW = (255, 210, 40)
_GREEN = (40, 200, 90)
_ORANGE = (255, 140, 40)
_MAROON = (170, 30, 40)

# --- Flag definitions -------------------------------------------------------
#
# Every entry is a (rows, palette) tuple; ``rows`` is 8 strings of exactly
# 12 characters. Single-character keys pick colours out of ``palette``; the
# dot character always leaves that pixel dark (unset on the LED panel).

_US = (
    [
        "RRRRRRRBBBBB",
        "WWWWWWWBBBBB",
        "RRRRRRRWWWWW",
        "WWWWWWWWWWWW",
        "RRRRRRRRRRRR",
        "WWWWWWWWWWWW",
        "RRRRRRRRRRRR",
        "WWWWWWWWWWWW",
    ],
    {"R": _RED, "W": _WHITE, "B": _DEEP_BLUE},
)

_JP = (
    [
        "WWWWWWWWWWWW",
        "WWWWWWWWWWWW",
        "WWWWRRRRWWWW",
        "WWWRRRRRRWWW",
        "WWWRRRRRRWWW",
        "WWWWRRRRWWWW",
        "WWWWWWWWWWWW",
        "WWWWWWWWWWWW",
    ],
    {"W": _WHITE, "R": _DEEP_RED},
)

# Circle of 12 gold stars on blue -- at 12x8 the stars can't be individual,
# so a schematic ring of dots is drawn instead.
_EU = (
    [
        "BBBBBBBBBBBB",
        "BBBBYYYYBBBB",
        "BBYYBBBBYYBB",
        "BBYBBBBBBYBB",
        "BBYBBBBBBYBB",
        "BBYYBBBBYYBB",
        "BBBBYYYYBBBB",
        "BBBBBBBBBBBB",
    ],
    {"B": _DEEP_BLUE, "Y": _YELLOW},
)

# China: red field with the large gold star and four smaller stars in the
# upper-left. At 12x8 the small stars collapse to a single row of dots.
_CN = (
    [
        "RRRRRRRRRRRR",
        "RRYYRRRRRRRR",
        "RYYYYRYRRRRR",
        "RYYYYRRYRRRR",
        "RRYYRRRYRRRR",
        "RRRRRRYRRRRR",
        "RRRRRRRRRRRR",
        "RRRRRRRRRRRR",
    ],
    {"R": _DEEP_RED, "Y": _YELLOW},
)

# UK: navy blue field with white saltire and red cross of St George on top.
_GB = (
    [
        "BWBBBBBBBBWB",
        "BBWBBBWBBWBB",
        "BBBWWWWWWBBB",
        "WWWWWRWWWWWW",
        "BBBWWWWWWBBB",
        "BBWBBBWBBWBB",
        "BWBBBBBBBBWB",
        "BBBBBBBBBBBB",
    ],
    {"B": _DEEP_BLUE, "W": _WHITE, "R": _RED},
)

# South Korea: white field with red-blue taegeuk and four trigrams.
_KR = (
    [
        "WWWKKWWWWWKW",
        "WWWWWWWWKWKW",
        "WWWRRBWWWWKW",
        "WWWRBBWWWWWW",
        "WWWWWWWWKKKW",
        "WWWKWKWWWWWW",
        "WKKWKKKWWWKW",
        "WWWWWWWWWWWW",
    ],
    {"W": _WHITE, "R": _RED, "B": _BLUE, "K": _BLACK},
)

# India: saffron/white/green tricolour with a schematic chakra dot.
_IN = (
    [
        "OOOOOOOOOOOO",
        "OOOOOOOOOOOO",
        "OOOOOOOOOOOO",
        "WWWWWBWWWWWW",
        "WWWWWBWWWWWW",
        "GGGGGGGGGGGG",
        "GGGGGGGGGGGG",
        "GGGGGGGGGGGG",
    ],
    {"O": _ORANGE, "W": _WHITE, "G": _GREEN, "B": _DEEP_BLUE},
)

# Canada: red bars with a stylised maple leaf.
_CA = (
    [
        "RRRWWWWWWRRR",
        "RRRWWWRWWRRR",
        "RRRWRRRRRRRR",
        "RRRWRRRRRWRR",
        "RRRWWRRRWWRR",
        "RRRWWRRRWWRR",
        "RRRWWWRWWWRR",
        "RRRWWWWWWWRR",
    ],
    {"R": _RED, "W": _WHITE},
)

# Australia: Union Jack in the canton (schematic), stars simplified to dots.
_AU = (
    [
        "BBWBBBWBBWBW",
        "BWBWBWBWBBBB",
        "WWWWWWWWWBWB",
        "WWWRWWWWBBBB",
        "WWWWWWWWWBBB",
        "BWBWBWBBBWBB",
        "BBWBBBWBBBBB",
        "BBBBBBBBBBBB",
    ],
    {"B": _DEEP_BLUE, "W": _WHITE, "R": _RED},
)

# Switzerland: red field, white plus.
_CH = (
    [
        "RRRRRRRRRRRR",
        "RRRRRRRRRRRR",
        "RRRRRWWRRRRR",
        "RRRWWWWWWRRR",
        "RRRWWWWWWRRR",
        "RRRRRWWRRRRR",
        "RRRRRRRRRRRR",
        "RRRRRRRRRRRR",
    ],
    {"R": _RED, "W": _WHITE},
)

# Mexico: green/white/red with a schematic eagle silhouette.
_MX = (
    [
        "GGGGWWWWRRRR",
        "GGGGWWWWRRRR",
        "GGGGWBWWRRRR",
        "GGGGBBBWRRRR",
        "GGGGWBWWRRRR",
        "GGGGWWWWRRRR",
        "GGGGWWWWRRRR",
        "GGGGWWWWRRRR",
    ],
    {"G": _GREEN, "W": _WHITE, "R": _DEEP_RED, "B": _MAROON},
)

# Taiwan (TWD, Taiwan Mobile revenue-share): red field with blue canton and
# 12-pointed white sun. Canton simplified to top-left quarter.
_TW = (
    [
        "BBBBBBRRRRRR",
        "BWWWWBRRRRRR",
        "BWWWWBRRRRRR",
        "BWWWWBRRRRRR",
        "BBBBBBRRRRRR",
        "RRRRRRRRRRRR",
        "RRRRRRRRRRRR",
        "RRRRRRRRRRRR",
    ],
    {"B": _DEEP_BLUE, "W": _WHITE, "R": _DEEP_RED},
)

# Hong Kong: red field, white bauhinia flower (simplified).
_HK = (
    [
        "RRRRRRRRRRRR",
        "RRRRRWWRRRRR",
        "RRRWWWWWRRRR",
        "RRRWWWWWWRRR",
        "RRRWWWWWRRRR",
        "RRRRRWWRRRRR",
        "RRRRRRRRRRRR",
        "RRRRRRRRRRRR",
    ],
    {"R": _DEEP_RED, "W": _WHITE},
)

# Singapore: red-over-white with a crescent and stars (schematic).
_SG = (
    [
        "RRRRRRRRRRRR",
        "RRWWRWRRRRRR",
        "RRWWWWWWRWWR",
        "RRRWWWWRRRRR",
        "WWWWWWWWWWWW",
        "WWWWWWWWWWWW",
        "WWWWWWWWWWWW",
        "WWWWWWWWWWWW",
    ],
    {"R": _DEEP_RED, "W": _WHITE},
)

# Brazil: green field with yellow diamond and blue disc.
_BR = (
    [
        "GGGGGGGGGGGG",
        "GGGGYYYYGGGG",
        "GGYYYBBYYYGG",
        "GYYYBBBBYYYG",
        "GYYYBBBBYYYG",
        "GGYYYBBYYYGG",
        "GGGGYYYYGGGG",
        "GGGGGGGGGGGG",
    ],
    {"G": _GREEN, "Y": _YELLOW, "B": _DEEP_BLUE},
)

# Currency-code → flag mapping. Keeps callers ISO-4217-native.
_BY_CURRENCY: dict[str, tuple[list[str], dict[str, Color]]] = {
    "USD": _US,
    "JPY": _JP,
    "EUR": _EU,
    "CNY": _CN,
    "GBP": _GB,
    "KRW": _KR,
    "INR": _IN,
    "CAD": _CA,
    "AUD": _AU,
    "CHF": _CH,
    "MXN": _MX,
    "TWD": _TW,
    "HKD": _HK,
    "SGD": _SG,
    "BRL": _BR,
}


def flag_for(currency: str) -> tuple[list[str], dict[str, Color]] | None:
    """Return ``(rows, palette)`` for a currency, or ``None`` if unmapped.

    The caller can fall back to a text-only row when this returns ``None``
    rather than picking a wrong flag; a missing flag is better than a
    misleading one.
    """
    return _BY_CURRENCY.get(currency.upper())


def supported_currencies() -> tuple[str, ...]:
    """Sorted tuple of currency codes with a bundled flag."""
    return tuple(sorted(_BY_CURRENCY))


__all__ = [
    "FLAG_HEIGHT",
    "FLAG_WIDTH",
    "flag_for",
    "supported_currencies",
]
