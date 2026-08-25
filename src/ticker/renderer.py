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

# ---------------------------------------------------------------------------
# Frame timing diagnostics
#
# Added to chase a fixed-row brightness artifact that only shows up during
# high-motion content (YouTube video, Pokemon sprite animation) and not on a
# static field (confirmed via pixel-test mode) -- pointing at frame-timing
# jitter under CPU load rather than a dead/stuck LED. This section turns that
# hunch into numbers: how long each stage of the loop actually takes, and how
# often a frame runs so late it can't have slept at all before the next one.
#
# Off by default (adds a getenv + a few time.monotonic() calls per frame,
# cheap either way, but there's no reason to pay even that on a panel that
# isn't being debugged). Enable with TICKER_FRAME_DIAG=1 in .env or the
# environment, restart the `ticker` service, then `journalctl -u ticker -f`.
# ---------------------------------------------------------------------------
FRAME_DIAG_ENABLED = os.getenv("TICKER_FRAME_DIAG", "").strip().lower() in {"1", "true", "yes"}

# Emit one summary line on this cadence rather than every frame -- at 30fps a
# per-frame line would be 30 log lines/sec, unreadable and disk-expensive on
# a Pi's SD card. A frame that actually drops still bumps the counters this
# summary reports; you just see it on the next tick of the clock instead of
# the instant it happens, which is fine for a hardware-timing hunt where the
# pattern (not the exact frame) is what matters.
FRAME_DIAG_SUMMARY_SEC = 5.0

# Minimum brightness while the Wi-Fi setup screen is forced on. Chosen to match
# the lowest scheduled daytime step rather than something brighter: it has to be
# readable across a room, not attention-grabbing, and the screen appears
# unbidden.
SETUP_BRIGHTNESS_FLOOR = 0.45

# Level of the flat field shown while calibrating the two panel halves. Mid-grey
# rather than white: at full white both halves clip against the same ceiling and
# a mismatch disappears, which is exactly the wrong behaviour for a test target.
_PANEL_TEST_LEVEL = 128.0


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


class _FrameDiag:
    """Rolling frame-timing stats, reset every time a summary line is emitted.

    Three numbers per frame, in milliseconds: how long ``mode.render()`` took
    (the one stage under a mode's own control -- this is what would show up
    high for a genuinely slow YouTube blit or Pokemon sprite pass), how long
    ``matrix.show()`` took (the DMA push to the panel -- driver/hardware side,
    not something any mode's Python code touches), and the total loop time.

    \"Dropped\" is defined the same way the loop itself defines being behind
    schedule: ``total`` exceeding the per-frame budget (``1/fps``) means
    ``time.sleep()`` at the bottom of the loop got zero or negative time, i.e.
    the next frame started later than it should have. That's the same
    condition that would misalign a DMA row-latch against the panel's own
    scan clock -- so a spike in drop rate lining up with what's on screen is
    exactly the signal this is built to catch.
    """

    __slots__ = (
        "frame_count",
        "drop_count",
        "render_ms_total",
        "show_ms_total",
        "total_ms_total",
        "max_total_ms",
        "window_started",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.frame_count = 0
        self.drop_count = 0
        self.render_ms_total = 0.0
        self.show_ms_total = 0.0
        self.total_ms_total = 0.0
        self.max_total_ms = 0.0
        self.window_started = time.monotonic()

    def record(self, render_ms: float, show_ms: float, total_ms: float, budget_ms: float) -> None:
        self.frame_count += 1
        self.render_ms_total += render_ms
        self.show_ms_total += show_ms
        self.total_ms_total += total_ms
        self.max_total_ms = max(self.max_total_ms, total_ms)
        if total_ms > budget_ms:
            self.drop_count += 1

    def maybe_log_summary(self, mode_name: str, fps: int) -> None:
        """Emit one line every FRAME_DIAG_SUMMARY_SEC and reset the window.

        Guards on ``frame_count`` rather than assuming the caller only calls
        this once the window has elapsed -- keeps the reset logic in one
        place instead of duplicated at each call site.
        """
        elapsed = time.monotonic() - self.window_started
        if elapsed < FRAME_DIAG_SUMMARY_SEC or self.frame_count == 0:
            return
        avg_render = self.render_ms_total / self.frame_count
        avg_show = self.show_ms_total / self.frame_count
        avg_total = self.total_ms_total / self.frame_count
        drop_pct = 100.0 * self.drop_count / self.frame_count
        LOGGER.info(
            "frame-diag mode=%s frames=%d fps_target=%d avg_render_ms=%.2f "
            "avg_show_ms=%.2f avg_total_ms=%.2f max_total_ms=%.2f "
            "dropped=%d (%.1f%%)",
            mode_name,
            self.frame_count,
            fps,
            avg_render,
            avg_show,
            avg_total,
            self.max_total_ms,
            self.drop_count,
            drop_pct,
        )
        self.reset()


def run() -> None:
    """Run at the configured frame rate until systemd stops the process."""
    if FRAME_DIAG_ENABLED:
        # Only raises this logger's own level; leaves the root logger (and
        # every other module's LOGGER.exception/.warning calls) untouched.
        # journald already captures stderr regardless of level via the
        # service's StandardError=journal, so this is purely about letting
        # our own INFO summary lines through the default WARNING floor.
        LOGGER.setLevel(logging.INFO)
        LOGGER.info(
            "frame-diag enabled: summarizing every %.0fs, journalctl -u ticker -f to watch",
            FRAME_DIAG_SUMMARY_SEC,
        )
    frame_diag = _FrameDiag() if FRAME_DIAG_ENABLED else None

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

    # Per-half panel gains, on the same once-a-second cadence as brightness.
    # Applied unramped: these move only when someone is dragging a slider on
    # the settings page and watching the seam, where the lag of a ramp reads
    # as the control being broken.
    panel_gains = config.current_panel_calibration()
    panel_test = config.current_panel_calibration_test()
    # The seam sits at the boundary between the two chained 64x32 panels.
    seam = canvas.width // 2

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
                panel_gains = config.current_panel_calibration()
                panel_test = config.current_panel_calibration_test()
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
            if frame_diag is not None:
                rendered_at = time.monotonic()
            # Scale pixels here rather than relying on an undocumented driver
            # brightness property; this also makes the web slider hardware-neutral.
            brightness += max(-ramp, min(ramp, target_brightness - brightness))
            pixels = np.asarray(canvas.image_buffer, dtype=np.float32)
            if reorder_channels:
                pixels = pixels[:, :, channel_index]
            if panel_test:
                # Flat mid-grey across both halves. Text and graphics make a
                # brightness mismatch almost impossible to judge -- the eye
                # latches onto the shapes instead of the level -- so matching
                # the halves needs a uniform field to compare.
                pixels = np.full_like(pixels, _PANEL_TEST_LEVEL)
            pixels = pixels * brightness
            left_gain, right_gain = panel_gains
            if left_gain != 1.0:
                pixels[:, :seam] *= left_gain
            if right_gain != 1.0:
                pixels[:, seam:] *= right_gain
            # Clip before the cast: a gain above 1.0 can push a bright pixel
            # past 255, and uint8 wraps rather than saturates, which would turn
            # white into near-black speckle.
            framebuffer[:] = np.clip(pixels, 0, 255).astype(np.uint8)
            matrix.show()
            if frame_diag is not None:
                shown_at = time.monotonic()
                budget_ms = 1000.0 / config.fps
                frame_diag.record(
                    render_ms=(rendered_at - started) * 1000.0,
                    show_ms=(shown_at - rendered_at) * 1000.0,
                    total_ms=(shown_at - started) * 1000.0,
                    budget_ms=budget_ms,
                )
                frame_diag.maybe_log_summary(current_name, config.fps)
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
