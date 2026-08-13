# MIT License — Copyright (c) 2026 John Kuok
"""Hardware-free import, configuration, and mode-render smoke tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from ticker.canvas import Canvas
from ticker.config import load_config
from ticker.modes import MODE_TYPES


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text(
        "TICKER_WIDTH=128\nTICKER_HEIGHT=32\nTICKER_SYMBOLS=AAPL,NVDA\n"
        "WEATHER_LAT=37.7749\nWEATHER_LON=-122.4194\n",
        encoding="utf-8",
    )
    return load_config(env)


def test_config_loads_minimal_env(config):  # type: ignore[no-untyped-def]
    assert config.width == 128
    assert config.symbols == ("AAPL", "NVDA")
    assert config.current_mode() == "stocks"


def test_all_modes_instantiate_and_render(config, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Avoid external I/O while exercising each public Mode.render method."""
    canvas = Canvas(config.width, config.height)
    for mode_type in MODE_TYPES.values():
        mode = mode_type(config)
        monkeypatch.setattr(mode, "_last_refresh", 1e20, raising=False)
        mode.render(canvas, tick=1)


def test_renderer_opens_with_mocked_piomatter(config, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Mock PioMatter so the documented initialization path needs no hardware."""
    created: dict[str, object] = {}

    class FakePioMatter:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)

    fake_module = SimpleNamespace(
        Geometry=lambda **kwargs: kwargs,
        Orientation=SimpleNamespace(Normal="normal"),
        Colorspace=SimpleNamespace(RGB888Packed="rgb888"),
        Pinout=SimpleNamespace(AdafruitMatrixBonnet="bonnet"),
        PioMatter=FakePioMatter,
    )
    monkeypatch.setitem(sys.modules, "piomatter", fake_module)

    import ticker.renderer

    matrix, framebuffer = ticker.renderer._open_matrix(config)
    assert isinstance(matrix, FakePioMatter)
    assert framebuffer.shape == (32, 128, 3)
    assert created["pinout"] == "bonnet"
