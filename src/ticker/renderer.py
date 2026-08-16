# MIT License — Copyright (c) 2026 John Kuok
"""PioMatter display loop and file-polled mode switching."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import numpy as np

from ticker.canvas import Canvas
from ticker.config import Config, load_config
from ticker.modes import build_mode
from ticker.quake_alert import QuakeAlertWatcher

LOGGER = logging.getLogger(__name__)

# One log line per mode per this many seconds, at most. Mode exceptions in the
# field are usually a persistent "the API changed" or "the API key is empty"
# situation, so a per-frame log would flood journalctl without adding signal.
_MODE_ERROR_THROTTLE_SEC = 60.0
_last_mode_error_at: dict[str, float] = {}

# Minimum brightness while the Wi-Fi setup screen is forced on. Chosen to match
# the lowest scheduled daytime step rather than something brighter: it has to be
# readable across a room, not attention-grabbing, and the screen appears
# unbidden.
SETUP_BRIGHTNESS_FLOOR = 0.45


def _open_matrix(config: Config) -> tuple[Any, np.ndarray]:
    """Create the Pi 5 PIO display object using the official PioMatter pattern.

    The distribution is ``Adafruit-Blinka-Raspberry-Pi5-Piomatter``, and the
    importable module is NOT the bare ``piomatter`` the examples alias it to:

    * 1.0.0 (stable)  -> ``adafruit_blinka_raspberry_pi5_piomatter``
    * 1.0.0a3 (alpha) -> ``adafruit_raspberry_pi5_piomatter``

    Only the stable module is usable here. Inspecting the published cp313
    aarch64 wheels shows the alpha exports just ``Geometry``, ``Orientation``,
    and ``PioMatter`` -- it has no ``Colorspace`` or ``Pinout``, so the call
    pattern in Adafruit's current examples cannot work against it. The alpha is
    therefore detected only to raise an actionable error instead of dying later
    on a confusing missing attribute. See
    https://github.com/adafruit/Adafruit_Blinka_Raspberry_Pi5_Piomatter
    examples/simpletest.py for the documented call pattern.
    """
    try:
        import adafruit_blinka_raspberry_pi5_piomatter as piomatter
    except ModuleNotFoundError as exc:
        import importlib.util

        if importlib.util.find_spec("adafruit_raspberry_pi5_piomatter") is not None:
            raise RuntimeError(
                "Found the 1.0.0a3 alpha of Adafruit-Blinka-Raspberry-Pi5-Piomatter, "
                "which predates the Colorspace and Pinout API this renderer needs. "
                "Upgrade with: venv/bin/python -m pip install -r requirements.txt"
            ) from exc
        raise

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

    # Long-lived watcher: polls USGS on its own cadence inside .tick(). The
    # renderer just asks it, once a second, to consider polling. We expose
    # .current_alert as a bound method so the quakes mode can read whatever
    # state the watcher publishes without owning any polling logic itself.
    quake_watcher = QuakeAlertWatcher(config)

    current_name = config.current_mode()
    last_user_mode = current_name
    current_mode = build_mode(current_name, config, alert_source=quake_watcher.current_alert)
    _write_pid(config)
    tick = 0
    check_interval = max(1, config.fps)

    # Brightness is resolved once a second, not every frame: it reads state files
    # and walks the schedule, and no schedule step or slider drag needs 30Hz.
    target_brightness = config.current_brightness()
    if config.network_notice():
        target_brightness = max(target_brightness, SETUP_BRIGHTNESS_FLOOR)
    brightness = target_brightness
    # Ramp the full 0-1 range over about a second and a half. A scheduled drop
    # from 75% to off is a startling flash to black when applied in one frame.
    ramp = 1.0 / (1.5 * config.fps)

    # Resolve the channel permutation once. Panels that wire green and blue the
    # other way round render every color wrong but raise nothing, so this is a
    # display setting rather than an error condition. "rgb" costs nothing since
    # the reorder is skipped entirely.
    channel_index = tuple("rgb".index(channel) for channel in config.channel_order)
    reorder_channels = channel_index != (0, 1, 2)

    try:
        while True:
            started = time.monotonic()
            if tick % check_interval == 0:
                # The Wi-Fi notice outranks the selected mode. This is the only
                # place anything overrides the web app's choice, and it has to:
                # the notice is written when the ticker is off the network, which
                # is exactly when the web app cannot be reached to select the
                # screen that explains how to fix it. The selection itself is
                # left untouched on disk, so the panel returns to whatever was
                # chosen the moment a network comes back.
                notice = config.network_notice()

                # Poll the quake watcher on the same cadence. It only actually
                # hits USGS every ~60s regardless of how often we call it, so
                # this is cheap. Skipped while a Wi-Fi notice is up because
                # there's no route to USGS anyway.
                if not notice:
                    quake_watcher.tick()

                # Manual switch cancels an active alert. "Manual" is detected
                # by observing that config.current_mode() -- the file the web
                # app writes -- has changed since the previous check. Without
                # this the alert would ignore the user tapping a different
                # mode button while the dwell window is still active.
                user_mode = config.current_mode()
                if user_mode != last_user_mode:
                    quake_watcher.clear()
                    last_user_mode = user_mode
                alert = None if notice else quake_watcher.current_alert()

                if notice:
                    requested_name = "net"
                elif alert is not None:
                    requested_name = "quakes"
                else:
                    requested_name = user_mode
                if requested_name != current_name:
                    current_name = requested_name
                    current_mode = build_mode(
                        current_name, config, alert_source=quake_watcher.current_alert
                    )
                target_brightness = config.current_brightness()
                # A brightness floor while the setup screen is up. The night
                # schedule drops to 8%, at which point an eight-character
                # password is not readable off the panel -- and with the hotspot
                # up the panel is the only place it exists. The floor is lifted
                # again the moment the notice clears, so the schedule still owns
                # brightness for every other screen.
                if notice:
                    target_brightness = max(target_brightness, SETUP_BRIGHTNESS_FLOOR)
            canvas.clear()
            try:
                current_mode.render(canvas, tick)
            except Exception as error:
                # Rate-limited so a persistent mode failure doesn't drown
                # journalctl. The panel still shows the tiny error frame every
                # tick; the log line just gives us a traceback for diagnosis.
                now_mono = time.monotonic()
                last = _last_mode_error_at.get(current_name, 0.0)
                if now_mono - last >= _MODE_ERROR_THROTTLE_SEC:
                    _last_mode_error_at[current_name] = now_mono
                    LOGGER.exception("%s mode render failed", current_name)
                canvas.clear()
                # canvas.text() sanitizes for the Latin-1 bitmap font, but if
                # the exception message itself contains non-ASCII we still want
                # readable output rather than a further-mangled string.
                canvas.text(1, 10, f"{current_name} error", (255, 60, 60), 8)
                canvas.text(1, 21, str(error)[:24], (140, 140, 140), 7)
            # Scale pixels here rather than relying on an undocumented driver
            # brightness property; this also makes the web slider hardware-neutral.
            brightness += max(-ramp, min(ramp, target_brightness - brightness))
            pixels = np.asarray(canvas.image_buffer, dtype=np.float32)
            if reorder_channels:
                pixels = pixels[:, :, channel_index]
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
