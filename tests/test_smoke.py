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
    # Bay Wheels GBFS is a live external call; a smoke test must not touch it.
    monkeypatch.setattr("ticker.baywheels.fetch_station", lambda station_id: None)
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
    # Register ONLY a module name the package actually publishes. An earlier
    # version of this test faked a top-level "piomatter" module, which made the
    # suite pass while the renderer died on the Pi with ModuleNotFoundError.
    # Faking a module that exists nowhere in the wild proves nothing.
    monkeypatch.delitem(sys.modules, "piomatter", raising=False)
    monkeypatch.setitem(sys.modules, "adafruit_blinka_raspberry_pi5_piomatter", fake_module)

    import ticker.renderer

    matrix, framebuffer = ticker.renderer._open_matrix(config)
    assert isinstance(matrix, FakePioMatter)
    assert framebuffer.shape == (32, 128, 3)
    assert created["pinout"] == "bonnet"


def test_baywheels_search_and_nearest_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetcher parses a fake GBFS pair and answers search + nearest correctly."""
    from ticker import baywheels

    fake_info = {
        "data": {
            "stations": [
                {"station_id": "a", "name": "Market St & 10th St", "lat": 37.775, "lon": -122.416, "capacity": 23},
                {"station_id": "b", "name": "Ferry Building", "lat": 37.795, "lon": -122.393, "capacity": 27},
                {"station_id": "c", "name": "Berkeley BART", "lat": 37.870, "lon": -122.268, "capacity": 19},
            ]
        }
    }
    fake_status = {
        "data": {
            "stations": [
                {"station_id": "a", "num_bikes_available": 11, "num_ebikes_available": 3, "num_docks_available": 12, "is_renting": 1, "is_installed": 1, "last_reported": 0},
                {"station_id": "b", "num_bikes_available": 4, "num_ebikes_available": 2, "num_docks_available": 23, "is_renting": 1, "is_installed": 1, "last_reported": 0},
                {"station_id": "c", "num_bikes_available": 0, "num_ebikes_available": 0, "num_docks_available": 19, "is_renting": 0, "is_installed": 1, "last_reported": 0},
            ]
        }
    }

    def fake_get_json(url: str):
        if "gbfs.json" in url:
            return {"data": {"en": {"feeds": [
                {"name": "station_information", "url": "https://example/station_information.json"},
                {"name": "station_status", "url": "https://example/station_status.json"},
            ]}}}
        if "station_information" in url:
            return fake_info
        return fake_status

    baywheels._reset_cache_for_tests()
    monkeypatch.setattr(baywheels, "_get_json", fake_get_json)

    hits = baywheels.search_stations("ferry")
    assert [hit.name for hit in hits] == ["Ferry Building"]

    market = baywheels.fetch_station("a")
    assert market is not None
    assert market.ebikes == 3
    assert market.classic_bikes == 8
    assert market.docks == 12

    nearest = baywheels.nearest_station(37.7749, -122.4194)
    assert nearest is not None
    assert nearest.station_id == "a"


def test_bikes_mode_renders_seeded_station(config) -> None:  # type: ignore[no-untyped-def]
    """With a pre-seeded station, the renderer draws all three columns of counts."""
    from ticker import baywheels
    from ticker.modes.bikes import BikesMode

    config.set_bike_station("seed")
    mode = BikesMode(config)
    mode._station = baywheels.Station(
        station_id="seed", name="Market St & 10th St", lat=37.775, lon=-122.416,
        capacity=23, num_bikes_available=11, num_ebikes_available=3,
        num_docks_available=12, is_renting=True, is_installed=True, last_reported=0,
    )
    mode._station_id = "seed"
    mode._checked = float("inf")

    canvas = Canvas(config.width, config.height)
    mode.render(canvas, tick=0)

    # A meaningful chunk of the panel is lit up (labels row, values row, logo).
    # The Lyft-pink logo alone lights ~60 pixels; three labelled columns add
    # another ~120. A blank frame would be near zero.
    non_black = sum(1 for pixel in canvas.image_buffer.getdata() if pixel != (0, 0, 0))
    assert non_black > 150


def test_renderer_uses_the_adafruit_module_name() -> None:
    """Guard the exact import name Adafruit ships, since only hardware catches it."""
    source = (Path(__file__).resolve().parents[1] / "src/ticker/renderer.py").read_text()
    assert "import adafruit_blinka_raspberry_pi5_piomatter as piomatter" in source
    assert "import piomatter" not in source.replace("as piomatter", "")
