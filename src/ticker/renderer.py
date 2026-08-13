# MIT License — Copyright (c) 2026 John Kuok
"""PioMatter display loop and file-polled mode switching."""

from __future__ import annotations

import os
import time
from typing import Any

import numpy as np

from ticker.canvas import Canvas
from ticker.config import Config, load_config
from ticker.modes import build_mode


def _open_matrix(config: Config) -> tuple[Any, np.ndarray]:
    """Create the Pi 5 PIO display object using the official PioMatter pattern."""
    import piomatter

    geometry = piomatter.Geometry(
        width=config.width,
        height=config.height,
        n_addr_lines=config.addr_lines,
        rotation=piomatter.Orientation.Normal,
    )
    framebuffer = np.zeros((config.height, config.width, 3), dtype=np.uint8)
    matrix = piomatter.PioMatter(
        colorspace=piomatter.Colorspace.RGB888Packed,
        pinout=piomatter.Pinout.AdafruitMatrixBonnet,
        framebuffer=framebuffer,
        geometry=geometry,
    )
    return matrix, framebuffer


def _write_pid(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")


def run() -> None:
    """Run at the configured frame rate until systemd stops the process."""
    config = load_config()
    matrix, framebuffer = _open_matrix(config)
    canvas = Canvas(config.width, config.height)
    current_name = config.current_mode()
    current_mode = build_mode(current_name, config)
    _write_pid(config)
    tick = 0
    check_interval = max(1, config.fps)

    # Brightness is resolved once a second, not every frame: it reads state files
    # and walks the schedule, and no schedule step or slider drag needs 30Hz.
    target_brightness = config.current_brightness()
    brightness = target_brightness
    # Ramp the full 0-1 range over about a second and a half. A scheduled drop
    # from 75% to off is a startling flash to black when applied in one frame.
    ramp = 1.0 / (1.5 * config.fps)

    try:
        while True:
            started = time.monotonic()
            if tick % check_interval == 0:
                requested_name = config.current_mode()
                if requested_name != current_name:
                    current_name = requested_name
                    current_mode = build_mode(current_name, config)
                target_brightness = config.current_brightness()
            canvas.clear()
            try:
                current_mode.render(canvas, tick)
            except Exception as error:
                canvas.clear()
                canvas.text(1, 10, f"{current_name} error", (255, 60, 60), 8)
                canvas.text(1, 21, str(error)[:24], (140, 140, 140), 7)
            # Scale pixels here rather than relying on an undocumented driver
            # brightness property; this also makes the web slider hardware-neutral.
            brightness += max(-ramp, min(ramp, target_brightness - brightness))
            pixels = np.asarray(canvas.image_buffer, dtype=np.float32)
            framebuffer[:] = (pixels * brightness).astype(np.uint8)
            matrix.show()
            tick += 1
            time.sleep(max(0.0, (1 / config.fps) - (time.monotonic() - started)))
    finally:
        try:
            config.pid_file.unlink(missing_ok=True)
        finally:
            if hasattr(matrix, "deinit"):
                matrix.deinit()


if __name__ == "__main__":
    run()
