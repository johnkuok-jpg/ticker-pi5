# MIT License — Copyright (c) 2026 John Kuok
"""Small, PIL-backed drawing API shared by all ticker modes.

Text uses the bundled Spleen bitmap fonts (BSD-2-Clause) rather than a scalable
outline font. On a 128x32 LED panel every pixel is either fully lit or fully
dark, so an antialiased font's grey edge pixels read as blur rather than detail.
A true bitmap font is pixel-exact at its design size and stays legible at the
small cap heights this display forces.

Available sizes, chosen so useful layouts fit in 32 rows:

    SMALL  (5x8)   25 chars per row, 3 rows tall  - body text, tickers
    MEDIUM (6x12)  21 chars per row, 2 rows tall  - emphasis
    LARGE  (8x16)  16 chars per row, 2 rows tall  - headline values
    HUGE   (12x24) 10 chars per row               - single hero value
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TypeAlias

from PIL import Image, ImageDraw, ImageFont

Color: TypeAlias = tuple[int, int, int]

FONTS_DIR = Path(__file__).resolve().parent / "fonts"

SMALL = 8
MEDIUM = 12
LARGE = 16
HUGE = 24

# Size -> (bundled font stem, advance width, cell height).
_FONTS: dict[int, tuple[str, int, int]] = {
    SMALL: ("spleen-5x8", 5, 8),
    MEDIUM: ("spleen-6x12", 6, 12),
    LARGE: ("spleen-8x16", 8, 16),
    HUGE: ("spleen-12x24", 12, 24),
}


# Bitmap fonts are Latin-1 only. Live RSS headlines routinely contain smart
# quotes, en/em dashes and ellipses, which raise UnicodeEncodeError deep inside
# PIL and would crash the render loop. Fold them to ASCII before drawing.
_TRANSLITERATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": ",",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "…": "...",
        "•": "*",
        " ": " ",
        "™": "TM",
        "€": "EUR",
        "£": "GBP",
        "¥": "JPY",
        "→": "->",
        "×": "x",
    }
)


def sanitize(text: str) -> str:
    """Make *text* safe for a Latin-1 bitmap font, losing as little as possible."""
    folded = str(text).translate(_TRANSLITERATE)
    # Anything still outside Latin-1 becomes a space rather than an exception.
    return "".join(char if ord(char) < 256 else " " for char in folded)


def char_width(font_size: int = SMALL) -> int:
    """Advance width of one character, for laying out fixed-width columns."""
    return _FONTS.get(font_size, _FONTS[SMALL])[1]


def line_height(font_size: int = SMALL) -> int:
    """Full cell height of a text row, for stacking rows without overlap."""
    return _FONTS.get(font_size, _FONTS[SMALL])[2]


def max_chars(width: int, font_size: int = SMALL) -> int:
    """How many characters fit across *width* pixels at *font_size*."""
    return max(0, width // char_width(font_size))


@lru_cache(maxsize=8)
def load_font(font_size: int = SMALL) -> ImageFont.ImageFont:
    """Return a cached bitmap font for *font_size*, nearest supported size."""
    stem = _FONTS.get(font_size, _FONTS[SMALL])[0]
    path = FONTS_DIR / f"{stem}.pil"
    if path.exists():
        return ImageFont.load(str(path))
    # Only reachable if the bundled fonts were deleted.
    for fallback in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",):
        if Path(fallback).exists():
            return ImageFont.truetype(fallback, font_size)
    return ImageFont.load_default()


class Canvas:
    """An RGB PIL image plus display-oriented drawing conveniences."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.image_buffer = Image.new("RGB", (width, height), (0, 0, 0))
        self._draw = ImageDraw.Draw(self.image_buffer)
        # Hard on/off glyph pixels; matters for the fallback outline font only.
        self._draw.fontmode = "1"

    def clear(self, color: Color = (0, 0, 0)) -> None:
        self._draw.rectangle((0, 0, self.width, self.height), fill=color)

    def text(self, x: int, y: int, text: str, color: Color, font_size: int = SMALL) -> None:
        """Draw *text* with its top-left cell corner at (x, y)."""
        self._draw.text((x, y), sanitize(text), font=load_font(font_size), fill=color)

    def text_width(self, text: str, font_size: int = SMALL) -> int:
        return int(self._draw.textlength(sanitize(text), font=load_font(font_size)))

    def text_bold(
        self,
        x: int,
        y: int,
        text: str,
        color: Color,
        font_size: int = SMALL,
        weight: int = 1,
    ) -> None:
        """Draw *text* with thickened strokes.

        Spleen ships no bold cut, so each glyph is stamped repeatedly one pixel
        to the right. On an LED panel this genuinely fattens the stroke instead
        of just darkening it, because there are no partial-intensity pixels to
        blend.

        Characters are placed one at a time with ``weight`` extra pixels of
        tracking. Smearing a whole string in place would push each glyph into
        its neighbour and close up the gaps between digits, turning "105" into
        one solid block.
        """
        cleaned = sanitize(text)
        font = load_font(font_size)
        advance = char_width(font_size) + weight
        for index, character in enumerate(cleaned):
            cx = x + index * advance
            for dx in range(weight + 1):
                self._draw.text((cx + dx, y), character, font=font, fill=color)

    def text_bold_width(self, text: str, font_size: int = SMALL, weight: int = 1) -> int:
        """Width of :meth:`text_bold`, matching its per-character advance."""
        return len(sanitize(text)) * (char_width(font_size) + weight)

    def text_centered(self, y: int, text: str, color: Color, font_size: int = SMALL) -> None:
        """Draw *text* horizontally centred on the panel."""
        x = max(0, (self.width - self.text_width(text, font_size)) // 2)
        self.text(x, y, text, color, font_size)

    def max_chars_in(self, width: int, font_size: int = SMALL) -> int:
        """How many characters fit in a *width*-pixel column."""
        return max_chars(width, font_size)

    def fit(self, text: str, width: int | None = None, font_size: int = SMALL) -> str:
        """Truncate *text* to what actually fits, so nothing is half-drawn."""
        cleaned = sanitize(text)
        limit = max_chars(self.width if width is None else width, font_size)
        return cleaned if len(cleaned) <= limit else cleaned[:limit]

    def scroll_text(
        self,
        y: int,
        text: str,
        color: Color,
        offset: int,
        font_size: int = SMALL,
        gap: int = 20,
    ) -> None:
        """Draw a seamlessly repeating, left-moving text string."""
        cleaned = sanitize(text)
        width = self.text_width(cleaned, font_size)
        if width <= 0:
            return
        period = width + gap
        start_x = -(offset % period)
        for x in range(start_x, self.width + period, period):
            self.text(x, y, cleaned, color, font_size)

    def image(self, x: int, y: int, pil_image: Image.Image) -> None:
        """Blit an image, retaining alpha when a logo supplies one."""
        image = pil_image.convert("RGBA")
        self.image_buffer.paste(image, (x, y), image)

    def sprite(self, x: int, y: int, rows: list[str], palette: dict[str, Color]) -> None:
        """Plot a pixel-art glyph; '.' cells are left dark."""
        for row_index, row in enumerate(rows):
            for col_index, key in enumerate(row):
                color = palette.get(key)
                if color is not None:
                    self.pixel(x + col_index, y + row_index, color)

    def pixel(self, x: int, y: int, color: Color) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self._draw.point((x, y), fill=color)

    def hline(self, y: int, color: Color, x0: int = 0, x1: int | None = None) -> None:
        """Thin horizontal rule, handy for separating rows."""
        self._draw.line((x0, y, (self.width if x1 is None else x1) - 1, y), fill=color)

    def degree(self, x: int, y: int, color: Color, size: int = 3) -> None:
        """Draw a degree ring by hand.

        Spleen's U+00B0 glyph is blank after BDF conversion, so relying on the
        font would silently drop the symbol. A hand-plotted ring is guaranteed.
        """
        if size <= 2:
            self.pixel(x, y, color)
            self.pixel(x + 1, y, color)
            self.pixel(x, y + 1, color)
            self.pixel(x + 1, y + 1, color)
            return
        last = size - 1
        for offset in range(size):
            self.pixel(x + offset, y, color)  # top edge
            self.pixel(x + offset, y + last, color)  # bottom edge
            self.pixel(x, y + offset, color)  # left edge
            self.pixel(x + last, y + offset, color)  # right edge
