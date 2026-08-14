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

from ..canvas import Canvas, LARGE, MEDIUM
from ..config import Config

# 128-pixel panel, minus one pixel of edge margin on each side.
MAX_TEXT_WIDTH = 126

# Fallback name shown before the wearer sets their own, so a brand-new panel
# still looks like a name tag instead of a mystery blank.
DEFAULT_NAME = "HELLO"


class NametagMode:
    """A static name-tag renderer that redraws only when the config changes."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def render(self, canvas: Canvas, tick: int) -> None:  # noqa: ARG002 - static screen
        name = self._config.current_nametag_name() or DEFAULT_NAME
        color = self._config.current_nametag_color()

        size, use_bold, width = _fit(canvas, name)

        # Vertical centering per font tier. LARGE is 16 tall on a 32-tall
        # panel, MEDIUM is 12 tall; both split the empty space evenly.
        line_height = 16 if size == LARGE else 12
        y = (canvas.height - line_height) // 2
        x = (canvas.width - width) // 2

        if use_bold:
            canvas.text_bold(x, y, name, color, size, weight=1)
        else:
            canvas.text(x, y, name, color, size)


def _fit(canvas: Canvas, name: str) -> tuple[int, bool, int]:
    """Pick the largest font tier the name fits into, and return (size, bold, width)."""
    for size, use_bold in ((LARGE, True), (LARGE, False), (MEDIUM, True), (MEDIUM, False)):
        width = (
            canvas.text_bold_width(name, size, weight=1)
            if use_bold
            else canvas.text_width(name, size)
        )
        if width <= MAX_TEXT_WIDTH:
            return size, use_bold, width

    # Nothing fits: truncate to 21 characters (MEDIUM plain cap on a 128 panel)
    # rather than let the name run off the right edge.
    clipped = name[:21]
    width = canvas.text_width(clipped, MEDIUM)
    return MEDIUM, False, width
