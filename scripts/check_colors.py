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

# Deliberately saturated single channels: a permutation is unmistakable, whereas
# a blended color like orange could be misread as any of several swaps.
STEPS: list[tuple[str, tuple[int, int, int]]] = [
    ("RED", (255, 0, 0)),
    ("GREEN", (0, 255, 0)),
    ("BLUE", (0, 0, 255)),
    ("YELLOW (red + green)", (255, 255, 0)),
    ("CYAN (green + blue)", (0, 255, 255)),
    ("MAGENTA (red + blue)", (255, 0, 255)),
    ("WHITE", (255, 255, 255)),
]


def main() -> int:
    config = load_config()
    matrix, framebuffer = _open_matrix(config)
    height, width = framebuffer.shape[0], framebuffer.shape[1]

    print(f"Panel geometry: {width}x{height}. Each color holds {HOLD_SECONDS:.0f}s.")
    print("Write down what the panel ACTUALLY shows for each name.\n")

    for name, rgb in STEPS:
        print(f"  showing {name:<22} -> panel should be {name.split(' ')[0]}")
        framebuffer[:] = np.array(rgb, dtype=np.uint8)
        matrix.show()
        time.sleep(HOLD_SECONDS)

    # Chain order: left half red, right half blue. If the colors appear on the
    # wrong sides, the ribbon order or rotation is reversed rather than the
    # channels being swapped.
    print("\n  showing LEFT HALF RED, RIGHT HALF BLUE -> checks chain order")
    framebuffer[:] = 0
    framebuffer[:, : width // 2] = np.array((255, 0, 0), dtype=np.uint8)
    framebuffer[:, width // 2 :] = np.array((0, 0, 255), dtype=np.uint8)
    matrix.show()
    time.sleep(HOLD_SECONDS * 2)

    # A one-pixel border finds a geometry mistake: a clean rectangle means the
    # addressing is right, while doubled or missing edges mean it is not.
    print("  showing WHITE 1px BORDER -> checks addressing and edges")
    framebuffer[:] = 0
    framebuffer[0, :] = 255
    framebuffer[height - 1, :] = 255
    framebuffer[:, 0] = 255
    framebuffer[:, width - 1] = 255
    matrix.show()
    time.sleep(HOLD_SECONDS * 2)

    framebuffer[:] = 0
    matrix.show()
    print("\nDone. Panel cleared. Restart the renderer with:")
    print("  sudo systemctl start ticker")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
