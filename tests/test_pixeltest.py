# MIT License — Copyright (c) 2026 John Kuok
"""Pixel test mode: solid-color cycle for spotting dead/stuck LEDs.

The mode is a pure function of ``time.time()`` with no persisted state, so
every test drives it by patching ``time.time`` rather than by ticking a
counter -- that's the one thing the render loop actually reads.
"""

from __future__ import annotations

from unittest.mock import patch

from ticker.canvas import Canvas
from ticker.config import Config
from ticker.modes.pixeltest import PixelTestMode, STEP_SECONDS, _SEQUENCE, _label_color


def _fixture_config(tmp_path) -> Config:
    return Config(state_dir=tmp_path, fps=15)


def _corner_pixel(canvas: Canvas) -> tuple[int, int, int]:
    """A pixel far from the corner label, safe to assert the fill color on."""
    return canvas.image_buffer.getpixel((canvas.width - 1, canvas.height - 1))


def _all_pixels_match(canvas: Canvas, color: tuple[int, int, int], *, skip_label_corner: bool) -> bool:
    pixels = canvas.image_buffer.load()
    for y in range(canvas.height):
        for x in range(canvas.width):
            if skip_label_corner and x < 48 and y < 10:
                # The corner label legitimately overwrites a small patch --
                # exclude it rather than asserting the whole field is untouched.
                continue
            if pixels[x, y] != color:
                return False
    return True


# -- sequence shape ---------------------------------------------------------


def test_sequence_is_red_green_blue_white_black_in_that_order():
    """Per-channel isolation first, then the two checks the RGB frames can't do."""
    names = [name for name, _ in _SEQUENCE]
    assert names == ["RED", "GREEN", "BLUE", "WHITE", "BLACK"]


def test_sequence_colors_are_pure():
    colors = {name: fill for name, fill in _SEQUENCE}
    assert colors["RED"] == (255, 0, 0)
    assert colors["GREEN"] == (0, 255, 0)
    assert colors["BLUE"] == (0, 0, 255)
    assert colors["WHITE"] == (255, 255, 255)
    assert colors["BLACK"] == (0, 0, 0)


# -- label legibility ---------------------------------------------------------


def test_label_color_is_light_on_black_dark_otherwise():
    assert _label_color((0, 0, 0)) == (140, 140, 140)
    for fill in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)):
        assert _label_color(fill) == (0, 0, 0)


# -- render: correct color per step ------------------------------------------


def test_render_fills_screen_with_first_step_at_t_zero(tmp_path):
    """At the very start of the cycle the panel should be solid red."""
    mode = PixelTestMode(_fixture_config(tmp_path))
    canvas = Canvas(width=128, height=32)
    with patch("ticker.modes.pixeltest.time.time", return_value=0.0):
        mode.render(canvas, tick=0)
    assert _corner_pixel(canvas) == (255, 0, 0)
    assert _all_pixels_match(canvas, (255, 0, 0), skip_label_corner=True)


def test_render_advances_through_each_step_on_schedule(tmp_path):
    """Stepping time forward by STEP_SECONDS walks the sequence in order."""
    mode = PixelTestMode(_fixture_config(tmp_path))
    for index, (_name, fill) in enumerate(_SEQUENCE):
        # Land comfortably inside the step's window, not right on the boundary.
        t = index * STEP_SECONDS + (STEP_SECONDS / 2)
        canvas = Canvas(width=128, height=32)
        with patch("ticker.modes.pixeltest.time.time", return_value=t):
            mode.render(canvas, tick=0)
        assert _corner_pixel(canvas) == fill, f"step {index} ({_name}) mismatch at t={t}"


def test_render_wraps_back_to_first_step_after_full_cycle(tmp_path):
    """One full cycle later, the sequence should have looped back to red."""
    mode = PixelTestMode(_fixture_config(tmp_path))
    cycle_length = STEP_SECONDS * len(_SEQUENCE)
    canvas = Canvas(width=128, height=32)
    with patch("ticker.modes.pixeltest.time.time", return_value=cycle_length + 1.0):
        mode.render(canvas, tick=0)
    assert _corner_pixel(canvas) == (255, 0, 0)


def test_render_is_a_pure_function_of_wall_clock_not_tick(tmp_path):
    """Restarting the renderer mid-cycle must resume at the right color --
    the mode holds no counters of its own, so two different tick values at
    the same wall-clock time must render identically."""
    mode_a = PixelTestMode(_fixture_config(tmp_path))
    mode_b = PixelTestMode(_fixture_config(tmp_path))
    t = STEP_SECONDS * 2 + 1.0  # partway into the BLUE step
    canvas_a = Canvas(width=128, height=32)
    canvas_b = Canvas(width=128, height=32)
    with patch("ticker.modes.pixeltest.time.time", return_value=t):
        mode_a.render(canvas_a, tick=0)
        mode_b.render(canvas_b, tick=99_999)
    assert _corner_pixel(canvas_a) == _corner_pixel(canvas_b) == (0, 0, 255)


def test_render_label_stays_legible_against_black_field(tmp_path):
    """On the BLACK step the label must still be lit (gray-on-black), or the
    panel would look indistinguishable from 'off'."""
    mode = PixelTestMode(_fixture_config(tmp_path))
    t = STEP_SECONDS * 4 + 1.0  # inside the BLACK step
    canvas = Canvas(width=128, height=32)
    with patch("ticker.modes.pixeltest.time.time", return_value=t):
        mode.render(canvas, tick=0)
    pixels = canvas.image_buffer.load()
    label_area_lit = any(
        pixels[x, y] != (0, 0, 0) for y in range(8) for x in range(24)
    )
    assert label_area_lit


# -- registry -----------------------------------------------------------


def test_pixeltest_is_registered_in_the_mode_registry():
    """The mode has to be reachable through the same registry as every other mode."""
    from ticker.modes import MODE_TYPES

    assert MODE_TYPES["pixeltest"] is PixelTestMode


def test_pixeltest_is_a_valid_mode_id():
    """A mode not in VALID_MODES can't be set through the webapp mode picker."""
    from ticker.config import VALID_MODES

    assert "pixeltest" in VALID_MODES


def test_pixeltest_has_a_web_label():
    """Without a MODE_LABELS entry the settings grid would show the raw slug."""
    from ticker.web.app import MODE_LABELS

    assert MODE_LABELS.get("pixeltest") == "Pixel Test"
