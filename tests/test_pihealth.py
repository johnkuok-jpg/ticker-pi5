# MIT License — Copyright (c) 2026 John Kuok
"""Pi Health mode: local diagnostic reads, colour ladder, and slide rotation.

The module has no network, but four different readings that each fail
independently (a missing thermal_zone0 on a laptop, a systemctl that isn't
in $PATH, etc.). The tests target each reader separately so a broken one
points at itself.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from ticker.canvas import Canvas
from ticker.config import Config
from ticker.modes.pihealth import (
    HealthReading,
    PiHealthMode,
    _disk_color,
    _format_bytes,
    _format_uptime,
    _load_color,
    _mem_color,
    _read_meminfo,
    _read_temp_c,
    _read_uptime,
    _systemctl_active,
    _temp_color,
    BAD,
    GOOD,
    WARN,
)


@pytest.fixture
def config(tmp_path):
    return Config(state_dir=tmp_path, fps=15)


# -- formatters -----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0B"),
        (999, "999B"),
        (2 * 1024, "2K"),
        (4 * 1024 * 1024, "4M"),
        (int(2.5 * 1024**3), "2.5G"),
    ],
)
def test_format_bytes(value, expected):
    assert _format_bytes(value) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (30, "0m"),
        (60 * 45, "45m"),
        (60 * 60 * 3 + 60 * 12, "3h 12m"),
        (60 * 60 * 26, "1d 2h"),
        (60 * 60 * 24 * 5 + 60 * 60 * 7, "5d 7h"),
    ],
)
def test_format_uptime(seconds, expected):
    assert _format_uptime(seconds) == expected


# -- colour ladders -------------------------------------------------------


def test_temp_color_ladder():
    """<70 green, 70-77 amber, >=78 red -- matches Pi 5 throttle behaviour."""
    assert _temp_color(60.0) == GOOD
    assert _temp_color(69.9) == GOOD
    assert _temp_color(70.0) == WARN
    assert _temp_color(77.9) == WARN
    assert _temp_color(78.0) == BAD
    assert _temp_color(85.0) == BAD


def test_load_color_ladder():
    """4-core Pi 5: <3 green, 3-3.99 amber, >=4 red."""
    assert _load_color(0.5) == GOOD
    assert _load_color(2.9) == GOOD
    assert _load_color(3.0) == WARN
    assert _load_color(3.99) == WARN
    assert _load_color(4.0) == BAD
    assert _load_color(9.0) == BAD


def test_mem_disk_color_ladders_use_free_fraction():
    """Colours track FREE fraction so an 8GB Pi with 60% used doesn't alarm."""
    # 50% used = 50% free, plenty
    assert _mem_color(0.5) == GOOD
    # 85% used = 15% free, warn territory
    assert _mem_color(0.85) == WARN
    # 95% used = 5% free, red
    assert _mem_color(0.95) == BAD

    assert _disk_color(0.5) == GOOD
    assert _disk_color(0.92) == WARN
    assert _disk_color(0.97) == BAD


# -- readers --------------------------------------------------------------


def test_read_temp_c_returns_none_when_sysfs_missing(monkeypatch, tmp_path):
    """CI/laptop: /sys/class/thermal/thermal_zone0/temp doesn't exist."""
    fake = tmp_path / "does-not-exist"
    monkeypatch.setattr("ticker.modes.pihealth.Path", lambda p: fake)
    # Direct call bypasses the Path monkey-patch above (we monkey-patched Path,
    # but the helper takes no args). Simpler: test that a missing file yields
    # None by patching Path.read_text via a stub Path class.
    monkeypatch.undo()
    with patch("ticker.modes.pihealth.Path") as MockPath:
        MockPath.return_value.read_text.side_effect = OSError("missing")
        assert _read_temp_c() is None


def test_read_temp_c_parses_millidegrees(monkeypatch):
    """Kernel gives 55123 for 55.123C; we round-trip that as a float."""
    with patch("ticker.modes.pihealth.Path") as MockPath:
        MockPath.return_value.read_text.return_value = "55123\n"
        assert _read_temp_c() == pytest.approx(55.123)


def test_read_meminfo_uses_memavailable(monkeypatch):
    """MemAvailable, not MemFree, so cache doesn't look like memory pressure."""
    fake = "MemTotal:       8000000 kB\nMemFree:         200000 kB\nMemAvailable:   4000000 kB\nBuffers:         100000 kB\n"
    with patch("ticker.modes.pihealth.Path") as MockPath:
        MockPath.return_value.read_text.return_value = fake
        used, total = _read_meminfo()
    assert total == 8_000_000 * 1024
    # used == total - available (in bytes), not total - free
    assert used == (8_000_000 - 4_000_000) * 1024


def test_read_meminfo_returns_none_when_field_missing():
    """A meminfo without MemAvailable (very old kernel) yields None, None."""
    with patch("ticker.modes.pihealth.Path") as MockPath:
        MockPath.return_value.read_text.return_value = "MemTotal:  8000000 kB\n"
        assert _read_meminfo() == (None, None)


def test_read_uptime_parses_first_number():
    with patch("ticker.modes.pihealth.Path") as MockPath:
        MockPath.return_value.read_text.return_value = "12345.67 45678.90\n"
        assert _read_uptime() == pytest.approx(12345.67)


def test_read_uptime_none_on_missing():
    with patch("ticker.modes.pihealth.Path") as MockPath:
        MockPath.return_value.read_text.side_effect = OSError()
        assert _read_uptime() is None


def test_systemctl_active_returns_true_on_active():
    """is-active prints 'active' on stdout; anything else is not active."""

    class _FakeResult:
        stdout = "active\n"

    with patch("ticker.modes.pihealth.subprocess.run", return_value=_FakeResult()):
        assert _systemctl_active("ticker") is True


def test_systemctl_active_returns_false_on_inactive():
    class _FakeResult:
        stdout = "inactive\n"

    with patch("ticker.modes.pihealth.subprocess.run", return_value=_FakeResult()):
        assert _systemctl_active("ticker") is False


def test_systemctl_active_returns_false_when_systemctl_missing():
    """No systemctl on the box shouldn't crash the mode - Pi Health must still render."""
    with patch("ticker.modes.pihealth.subprocess.run", side_effect=FileNotFoundError()):
        assert _systemctl_active("ticker") is False


# -- render smoke ---------------------------------------------------------


def _lit(canvas: Canvas) -> bool:
    pixels = canvas.image_buffer.load()
    return any(
        pixels[x, y] != (0, 0, 0) for y in range(canvas.height) for x in range(canvas.width)
    )


def test_all_three_slides_render(config):
    mode = PiHealthMode(config)
    mode.reading = HealthReading(
        temp_c=62.4,
        load1=1.2,
        load5=0.8,
        load15=0.6,
        mem_used_bytes=int(3.2 * 1024**3),
        mem_total_bytes=int(8.0 * 1024**3),
        disk_used_bytes=int(12 * 1024**3),
        disk_total_bytes=int(58 * 1024**3),
        uptime_seconds=60 * 60 * 26 + 60 * 5,
    )
    mode._service_state = {"ticker": True, "ticker-web": True}
    mode._last_refresh = 1e12
    mode._last_services_refresh = 1e12

    ticks_per_slide = 7 * config.fps
    for slide in range(3):
        canvas = Canvas(width=128, height=32)
        mode.render(canvas, tick=slide * ticks_per_slide)
        assert _lit(canvas), f"slide {slide} rendered nothing"


def test_render_loading_when_no_reading_yet(config):
    """Before the first refresh runs, the mode must show LOADING - not crash on `assert reading is not None`."""
    mode = PiHealthMode(config)
    # Refuse to refresh so the reading stays None.
    with patch.object(mode, "_refresh"):
        with patch.object(mode, "_refresh_services"):
            canvas = Canvas(width=128, height=32)
            mode.render(canvas, tick=0)
    assert _lit(canvas)


def test_render_survives_missing_temp_and_load(config):
    """On a machine without thermal_zone0 or getloadavg, the mode still lights up."""
    mode = PiHealthMode(config)
    mode.reading = HealthReading(
        temp_c=None,
        load1=None,
        load5=None,
        load15=None,
        mem_used_bytes=None,
        mem_total_bytes=None,
        disk_used_bytes=None,
        disk_total_bytes=None,
        uptime_seconds=None,
    )
    mode._service_state = {"ticker": False, "ticker-web": False}
    mode._last_refresh = 1e12
    mode._last_services_refresh = 1e12
    ticks_per_slide = 7 * config.fps
    for slide in range(3):
        canvas = Canvas(width=128, height=32)
        mode.render(canvas, tick=slide * ticks_per_slide)
        # Even the all-None case should still paint labels, not a black card.
        assert _lit(canvas)


def test_pihealth_is_registered_in_the_mode_registry():
    """The mode has to be reachable through the same registry as every other mode."""
    from ticker.modes import MODE_TYPES

    assert MODE_TYPES["pihealth"] is PiHealthMode


def test_pihealth_is_a_valid_mode_id():
    """A mode not in VALID_MODES can't be set through the webapp mode picker."""
    from ticker.config import VALID_MODES

    assert "pihealth" in VALID_MODES
