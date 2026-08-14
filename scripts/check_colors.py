#!/usr/bin/env python3
# MIT License — Copyright (c) 2026 John Kuok
"""Show named solid colors so a channel swap can be identified by eye.

Wrong colors on a HUB75 panel are almost always a channel permutation, and
guessing which one wastes hardware time. Each step here fills the panel with a
color whose NAME is printed in the terminal, so reporting what the panel
actually shows pins the permutation exactly: red displaying as blue means R and
B are exchanged, red as green means R and G are, and so on.

The last two steps are diagnostics of a different kind. Full white at low
brightness reveals a weak supply or a dim channel, and the split frame proves
which physical panel is first in the chain.

Fills are drawn at SOLID_LEVEL rather than 255. Identifying a hue needs no more
than that, and it matters if the Pi shares one supply with the panels: a full
white panel is by far the heaviest frame this project can draw, so at 255 the
diagnostic itself could brown out the Pi it is meant to diagnose. Pass --full to
get true 255 when the panels have their own supply.

Run with the renderer stopped, since only one process can drive the PIO:

    sudo systemctl stop ticker
    sudo venv/bin/python scripts/check_colors.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ticker.config import load_config  # noqa: E402
from ticker.renderer import _open_matrix  # noqa: E402

HOLD_SECONDS = 3.0

# 160 of 255 keeps every hue unmistakable while cutting the worst-case panel
# current by about a third, which is the difference between safe and marginal on
# a shared 5V supply.
SOLID_LEVEL = 160

# Deliberately saturated single channels: a permutation is unmistakable, whereas
# a blended color like orange could be misread as any of several swaps. Values
# are written as full-scale flags and scaled by SOLID_LEVEL below.
STEPS: list[tuple[str, tuple[int, int, int]]] = [
    ("RED", (1, 0, 0)),
    ("GREEN", (0, 1, 0)),
    ("BLUE", (0, 0, 1)),
    ("YELLOW (red + green)", (1, 1, 0)),
    ("CYAN (green + blue)", (0, 1, 1)),
    ("MAGENTA (red + blue)", (1, 0, 1)),
    ("WHITE", (1, 1, 1)),
]


def main() -> int:
    level = 255 if "--full" in sys.argv else SOLID_LEVEL
    config = load_config()
    matrix, framebuffer = _open_matrix(config)
    height, width = framebuffer.shape[0], framebuffer.shape[1]

    print(f"Panel geometry: {width}x{height}. Each color holds {HOLD_SECONDS:.0f}s.")
    print(f"Fill level {level}/255." + ("" if level == 255 else " Pass --full for 255."))
    if level == 255:
        print("WARNING: full white draws the most current this project can. Only safe")
        print("         when the panels have a supply of their own.")
    print("Write down what the panel ACTUALLY shows for each name.\n")
    # This script writes the framebuffer directly and deliberately ignores
    # TICKER_CHANNEL_ORDER. Applying the correction here would hide the very
    # swap the script exists to identify, so colors stay wrong until the
    # renderer runs with the setting in place.
    if config.channel_order != "rgb":
        print(f"NOTE: TICKER_CHANNEL_ORDER={config.channel_order} is set but NOT applied")
        print("      here, so colors below are raw. The renderer applies it.\n")

    for name, rgb in STEPS:
        print(f"  showing {name:<22} -> panel should be {name.split(' ')[0]}")
        framebuffer[:] = (np.array(rgb, dtype=np.uint16) * level).astype(np.uint8)
        matrix.show()
        time.sleep(HOLD_SECONDS)

    # Chain order: left half red, right half blue. If the colors appear on the
    # wrong sides, the ribbon order or rotation is reversed rather than the
    # channels being swapped.
    print("\n  showing LEFT HALF RED, RIGHT HALF BLUE -> checks chain order")
    framebuffer[:] = 0
    framebuffer[:, : width // 2] = np.array((level, 0, 0), dtype=np.uint8)
    framebuffer[:, width // 2 :] = np.array((0, 0, level), dtype=np.uint8)
    matrix.show()
    time.sleep(HOLD_SECONDS * 2)

    # A one-pixel border finds a geometry mistake: a clean rectangle means the
    # addressing is right, while doubled or missing edges mean it is not.
    print("  showing WHITE 1px BORDER -> checks addressing and edges")
    framebuffer[:] = 0
    framebuffer[0, :] = level
    framebuffer[height - 1, :] = level
    framebuffer[:, 0] = level
    framebuffer[:, width - 1] = level
    matrix.show()
    time.sleep(HOLD_SECONDS * 2)

    framebuffer[:] = 0
    matrix.show()
    print("\nDone. Panel cleared. Restart the renderer with:")
    print("  sudo systemctl start ticker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
