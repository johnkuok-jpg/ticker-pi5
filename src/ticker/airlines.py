# MIT License — Copyright (c) 2026 John Kuok
"""Airline brand colours and a 16x16 logo tile.

A real airline logo scaled to 16x16 is a coloured smudge: the wordmarks that
carry the recognition are three or four times that size. What survives at this
resolution is the brand colour pairing plus the two-letter IATA code, so that is
what gets drawn. Anyone who wants a true logo can drop a PNG into
``web/static/logos/airlines/<IATA>.png`` and it is used instead.

Colours are (background, foreground) taken from each carrier's livery, adjusted
where a literal brand colour would be unreadable on an LED panel - a mid navy at
16 pixels reads as black, so those are lifted.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeAlias

Color: TypeAlias = tuple[int, int, int]

LOGO_SIZE = 16

# Fallback for any carrier not listed: a neutral slate that still reads as a
# deliberate tile rather than a rendering failure.
DEFAULT_BRAND: tuple[Color, Color] = ((52, 74, 110), (225, 235, 250))

BRANDS: dict[str, tuple[Color, Color]] = {
    # North America
    "UA": ((26, 78, 150), (255, 255, 255)),
    "AA": ((30, 44, 62), (225, 235, 248)),
    "DL": ((190, 30, 45), (255, 255, 255)),
    "WN": ((48, 66, 118), (255, 190, 40)),
    "AS": ((20, 52, 92), (255, 255, 255)),
    "B6": ((28, 62, 140), (255, 255, 255)),
    "NK": ((240, 210, 30), (30, 30, 30)),
    "F9": ((20, 100, 60), (255, 255, 255)),
    "HA": ((100, 40, 130), (255, 210, 60)),
    "AC": ((200, 30, 40), (255, 255, 255)),
    "WS": ((20, 130, 200), (255, 255, 255)),
    "AM": ((20, 60, 120), (255, 200, 120)),
    "G4": ((20, 80, 60), (250, 200, 60)),
    "SY": ((30, 60, 120), (250, 250, 250)),
    # US regionals, which fly most of the short domestic legs and would otherwise
    # all land on the same anonymous slate tile.
    "OO": ((40, 80, 140), (250, 250, 250)),
    "YX": ((30, 70, 130), (200, 220, 245)),
    "MQ": ((40, 55, 75), (225, 235, 248)),
    "9E": ((150, 35, 50), (250, 250, 250)),
    "ZW": ((60, 100, 160), (250, 250, 250)),
    # Cargo. Overnight freight is much of what is airborne at night.
    "FX": ((70, 40, 130), (250, 130, 30)),
    "5X": ((90, 60, 30), (250, 200, 60)),
    "PO": ((30, 70, 130), (250, 200, 60)),
    "5Y": ((40, 60, 100), (250, 250, 250)),
    # Europe
    "BA": ((30, 50, 110), (255, 255, 255)),
    "LH": ((240, 190, 40), (25, 40, 80)),
    "AF": ((30, 55, 130), (255, 255, 255)),
    "KL": ((60, 145, 210), (255, 255, 255)),
    "IB": ((215, 40, 60), (250, 200, 40)),
    "AZ": ((30, 90, 60), (240, 240, 240)),
    "SK": ((40, 70, 130), (240, 200, 60)),
    "LX": ((210, 40, 50), (255, 255, 255)),
    "OS": ((215, 40, 55), (255, 255, 255)),
    "TP": ((20, 120, 90), (255, 255, 255)),
    "EI": ((20, 130, 90), (255, 255, 255)),
    "FR": ((25, 55, 140), (250, 205, 30)),
    "U2": ((250, 120, 20), (255, 255, 255)),
    "TK": ((200, 30, 45), (255, 255, 255)),
    "SU": ((225, 40, 55), (255, 255, 255)),
    "LO": ((30, 70, 120), (255, 255, 255)),
    "VS": ((215, 30, 60), (255, 255, 255)),
    "BT": ((160, 200, 40), (30, 45, 70)),
    # Middle East and Africa
    "EK": ((200, 30, 45), (240, 210, 60)),
    "QR": ((130, 30, 60), (240, 240, 240)),
    "EY": ((190, 155, 90), (60, 40, 60)),
    "SV": ((20, 110, 80), (240, 210, 90)),
    "ET": ((30, 110, 70), (240, 200, 50)),
    "MS": ((30, 70, 140), (240, 200, 60)),
    # Asia and Pacific
    "SQ": ((30, 60, 110), (240, 200, 60)),
    "CX": ((20, 110, 100), (240, 240, 240)),
    "JL": ((210, 35, 50), (255, 255, 255)),
    "NH": ((40, 90, 170), (255, 255, 255)),
    "KE": ((60, 110, 180), (240, 240, 240)),
    "OZ": ((215, 50, 60), (255, 255, 255)),
    "CI": ((30, 90, 70), (240, 210, 70)),
    "BR": ((60, 110, 170), (250, 240, 220)),
    "TG": ((110, 50, 140), (250, 200, 60)),
    "MH": ((30, 90, 120), (240, 210, 70)),
    "GA": ((30, 100, 150), (250, 240, 230)),
    "VN": ((30, 110, 140), (240, 200, 60)),
    "PR": ((30, 60, 130), (255, 255, 255)),
    "CA": ((200, 35, 45), (240, 210, 60)),
    "CZ": ((30, 80, 150), (255, 255, 255)),
    "MU": ((30, 70, 140), (255, 255, 255)),
    "AI": ((200, 40, 50), (250, 210, 60)),
    "6E": ((30, 60, 130), (250, 250, 250)),
    "QF": ((200, 30, 45), (255, 255, 255)),
    "NZ": ((30, 40, 55), (240, 240, 240)),
    "VA": ((210, 40, 55), (255, 255, 255)),
    "FJ": ((30, 110, 160), (250, 250, 250)),
    # Latin America
    "LA": ((30, 55, 120), (255, 255, 255)),
    "AV": ((215, 40, 55), (255, 255, 255)),
    "AR": ((80, 170, 220), (255, 255, 255)),
    "CM": ((30, 60, 130), (230, 180, 40)),
    "AD": ((215, 60, 40), (255, 255, 255)),
    "G3": ((230, 120, 40), (255, 255, 255)),
}


def brand_for(iata: str | None) -> tuple[Color, Color]:
    """Background and foreground for a two-letter IATA airline code."""
    return BRANDS.get((iata or "").strip().upper(), DEFAULT_BRAND)


def tile_code(iata: str | None, icao: str | None = None) -> str:
    """Pick the text for the tile: IATA if there is one, else the ICAO code.

    Cargo and charter operators often have no IATA code at all, and drawing an
    empty coloured square for them looks like a bug. Three ICAO characters still
    fit across 16 pixels in the 5-wide font, just.
    """
    code = (iata or "").strip().upper()
    if code:
        return code[:2]
    return (icao or "").strip().upper()[:3]


def logo_path(static_root: Path, iata: str | None) -> Path | None:
    """Return a user-supplied logo PNG for this airline, if one exists."""
    code = (iata or "").strip().upper()
    if not code:
        return None
    candidate = static_root / "logos" / "airlines" / f"{code}.png"
    return candidate if candidate.is_file() else None


def draw_logo(  # noqa: ANN001
    canvas,
    x: int,
    y: int,
    iata: str | None,
    static_root: Path | None = None,
    icao: str | None = None,
) -> None:
    """Draw the airline tile at (x, y), preferring a user PNG over the code tile.

    A missing or unreadable PNG falls back to the generated tile rather than
    leaving a hole, because a logo file is cosmetic and must not be able to take
    the flight display down.
    """
    from ticker.canvas import SMALL  # imported here to avoid a module cycle

    code = tile_code(iata, icao)
    background, foreground = brand_for((iata or "").strip().upper())

    if static_root is not None:
        path = logo_path(static_root, (iata or "").strip().upper() or code)
        if path is not None:
            try:
                from PIL import Image

                with Image.open(path) as source:
                    tile = source.convert("RGB").resize((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
                canvas.image(x, y, tile)
                return
            except Exception:
                pass

    canvas.fill_rect(x, y, LOGO_SIZE, LOGO_SIZE, background)
    if not code:
        return
    # Centre the glyphs by measured width; the 5x8 cell advance is not the same
    # as the inked width, so hard-coding an offset drifts by a pixel.
    width = canvas.text_width(code, SMALL)
    canvas.text(x + max(0, (LOGO_SIZE - width) // 2), y + 4, code, foreground, SMALL)
