# MIT License — Copyright (c) 2026 John Kuok
"""Desk name tag — a single name in bold, in a color of the wearer's choice.

Meant to be printed to a coworker's ticker so the panel becomes a permanent
desk plate rather than a live data feed. The name and its color both live on
the config, and both are settable from the web control panel.

The font tier is picked automatically per name length so the wearer never has
to think about it:

    up to 14 chars     -> LARGE bold  (spleen 8x16, weight 1)
    15 chars           -> LARGE plain
    16-20 chars        -> MEDIUM bold (spleen 6x12, weight 1)
    21 chars           -> MEDIUM plain
    22+ chars          -> truncated to 21 in MEDIUM plain

Anything above 14 chars is a corner case in a normal office; the ladder is
here so a longer legal name still looks intentional rather than clipped.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ..canvas import Canvas, LARGE, MEDIUM
from ..config import Config

# Perplexity mark, pre-rasterized to an 18x24 1-bit bitmap. Shipped as a
# static asset so the Pi does not need cairosvg at runtime; the bitmap is
# recolored per-render to match the wearer's chosen text color.
_MARK_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "logos" / "perplexity_24.png"

# The mark sits in a fixed left zone; the name centers in whatever's left.
# 18 (mark width) + 1 (left margin) + 3 (gap) = 22 px of left zone.
MARK_LEFT_MARGIN = 1
MARK_TEXT_GAP = 3

# 128-pixel panel, minus one pixel of edge margin on the right, minus the
# left zone reserved for the mark.
MAX_TEXT_WIDTH = 128 - 1 - 18 - MARK_LEFT_MARGIN - MARK_TEXT_GAP

# Fallback name shown before the wearer sets their own, so a brand-new panel
# still looks like a name tag instead of a mystery blank.
DEFAULT_NAME = "HELLO"

# Perplexity brand teal. The mark always ships in brand color regardless of
# what the wearer picks for the name, so the plate still reads as Perplexity
# even when the name is amber or pink.
MARK_COLOR = (32, 128, 141)  # #20808D


def _load_mark_bitmap() -> Image.Image | None:
    """Load the mark bitmap once and cache; returns None if the asset is missing."""
    if not _MARK_PATH.exists():
        return None
    return Image.open(_MARK_PATH).convert("1")


class NametagMode:
    """A static name-tag renderer that redraws only when the config changes."""

    def __init__(self, config: Config) -> None:
        self._config = config
        # Cache the mark bitmap on init; missing asset falls back to text-only.
        self._mark = _load_mark_bitmap()

    def render(self, canvas: Canvas, tick: int) -> None:  # noqa: ARG002 - static screen
        name = self._config.current_nametag_name() or DEFAULT_NAME
        color = self._config.current_nametag_color()

        # Paint the mark in Perplexity teal (fixed), then center the name in
        # the wearer's chosen color in the remaining right zone.
        mark_width = self._paint_mark(canvas, MARK_COLOR)
        text_zone_left = MARK_LEFT_MARGIN + mark_width + MARK_TEXT_GAP
        text_zone_width = canvas.width - text_zone_left - 1

        size, use_bold, width = _fit(canvas, name, text_zone_width)

        # Vertical centering per font tier. LARGE is 16 tall on a 32-tall
        # panel, MEDIUM is 12 tall; both split the empty space evenly.
        line_height = 16 if size == LARGE else 12
        y = (canvas.height - line_height) // 2
        x = text_zone_left + (text_zone_width - width) // 2

        if use_bold:
            canvas.text_bold(x, y, name, color, size, weight=1)
        else:
            canvas.text(x, y, name, color, size)

    def _paint_mark(self, canvas: Canvas, color: tuple[int, int, int]) -> int:
        """Draw the recolored mark into the canvas; return its width (0 if missing)."""
        if self._mark is None:
            return 0
        mw, mh = self._mark.size
        # Vertically center the mark on the 32-tall panel.
        top = (canvas.height - mh) // 2
        left = MARK_LEFT_MARGIN
        # 1-bit bitmap: walk every lit pixel and set it to the chosen color.
        # The bitmap is small (18x24), so this is well under a millisecond.
        pixels = self._mark.load()
        for y in range(mh):
            for x in range(mw):
                if pixels[x, y]:
                    canvas.pixel(left + x, top + y, color)
        return mw


def _fit(canvas: Canvas, name: str, max_width: int = MAX_TEXT_WIDTH) -> tuple[int, bool, int]:
    """Pick the largest font tier the name fits into, and return (size, bold, width).

    ``max_width`` shrinks when the logo takes up part of the panel, so short
    names still land at LARGE bold and longer names step down to MEDIUM.
    """
    for size, use_bold in ((LARGE, True), (LARGE, False), (MEDIUM, True), (MEDIUM, False)):
        width = (
            canvas.text_bold_width(name, size, weight=1)
            if use_bold
            else canvas.text_width(name, size)
        )
        if width <= max_width:
            return size, use_bold, width

    # Nothing fits: truncate to whatever MEDIUM plain can hold in the given
    # zone rather than let the name run off the right edge.
    max_chars = max(1, max_width // 6)  # MEDIUM is 6 px per char
    clipped = name[:max_chars]
    width = canvas.text_width(clipped, MEDIUM)
    return MEDIUM, False, width
