# MIT License — Copyright (c) 2026 John Kuok
"""Pixel test — full-screen solid-color cycle for spotting dead/stuck LEDs.

Behavior in one sentence: the panel fills edge-to-edge with one flat color
at a time, cycling through a fixed sequence, so a dead pixel (never lights)
or a stuck pixel (wrong color, or won't go black) stands out as the one dot
that doesn't match its neighbors.

Why a fixed sequence instead of something fancier
---------------------------------------------------
Diagnostic tooling should be boring on purpose. A gradient or animation
makes it hard to tell "is that dim corner a dead pixel or just the pattern
doing that"; a flat field removes all ambiguity — every pixel on screen
should be bit-identical, so anything that isn't jumps out immediately.

The sequence is red, green, blue, white, black. RGB isolates each channel
per sub-pixel (a stuck-red LED only shows up as a wrong dot during the
non-red frames), white confirms all three channels drive together at full
brightness, and black confirms nothing is stuck "on" (a pixel that stays
lit during the black frame is stuck, not dead).

Timing is derived from wall-clock time, not a frame counter, so — like
focus mode — a renderer restart mid-cycle resumes at the right color
instead of snapping back to red. There is no persisted state at all;
the whole mode is a pure function of ``time.time()``.
"""

from __future__ import annotations

import time

from ticker.canvas import Canvas, SMALL
from ticker.modes.base import Mode

# ---------------------------------------------------------------------------
# Sequence + timing
# ---------------------------------------------------------------------------

# Order matters: pure red/green/blue first so a stuck sub-pixel of any one
# channel is visible against an otherwise-uniform field, then white (all
# channels together) and black (nothing should be lit) as the two checks
# that catch what the per-channel frames can't.
_SEQUENCE: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("RED", (255, 0, 0)),
    ("GREEN", (0, 255, 0)),
    ("BLUE", (0, 0, 255)),
    ("WHITE", (255, 255, 255)),
    ("BLACK", (0, 0, 0)),
)

# Long enough to walk right up to the panel and scan it edge to edge before
# it moves on; short enough that leaving it running still cycles a few times
# a minute if you step back and watch from across the room.
STEP_SECONDS = 6.0

# The label has to be readable against every field in the sequence,
# including pure red/green/blue where light gray would wash out. Black text
# with a 1px offset shadow-free outline is overkill for this; instead each
# step picks a label color explicitly (see _label_color) rather than using
# one fixed color that only works on some backgrounds.


def _label_color(fill: tuple[int, int, int]) -> tuple[int, int, int]:
    """Pick a label color that stays legible against ``fill``.

    Black fill needs a light label; every other fill in the sequence is
    already at full saturation on one or more channels, so black reads
    cleanly on all of them (dark enough to contrast with white/red/green/
    blue without disappearing into anti-aliasing at the glyph edges).
    """
    if fill == (0, 0, 0):
        return (140, 140, 140)
    return (0, 0, 0)


class PixelTestMode(Mode):
    """Cycles full-screen solid colors so dead/stuck LEDs are easy to spot.

    Purely a function of wall-clock time — no state file, nothing to reset.
    Meant to be selected manually from the mode grid while physically
    inspecting the panel, the same way focus/nametag are manual utility
    modes rather than part of any automatic sequence (this renderer has no
    automatic mode rotation at all; the panel always shows whatever mode is
    selected on the settings page).
    """

    def render(self, canvas: Canvas, tick: int) -> None:
        step_count = len(_SEQUENCE)
        elapsed = time.time() % (STEP_SECONDS * step_count)
        index = int(elapsed // STEP_SECONDS) % step_count
        name, fill = _SEQUENCE[index]

        canvas.clear(fill)

        # Small corner label: which color is up, and where in the cycle we
        # are (e.g. "RED 1/5"). Doesn't defeat the point of a flat field —
        # it's a handful of pixels in one corner, not a graphic — but it
        # means you always know whether the panel has frozen or is just
        # sitting on a long step, and it survives being glanced at from an
        # angle where color alone is ambiguous.
        label = f"{name} {index + 1}/{step_count}"
        canvas.text(1, 1, label, _label_color(fill), SMALL)
