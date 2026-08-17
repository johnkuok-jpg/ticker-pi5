# MIT License — Copyright (c) 2026 John Kuok
"""Commute mode: config plumbing, fetch parsing, and card layout.

The mode is small but has three separate concerns that could break
independently -- (1) address/mode persistence, (2) response parsing
from Google Directions, and (3) the on-panel layout. The tests are
grouped by those three concerns so a failure points straight at the
piece that broke.
"""

from __future__ import annotations

import io
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from ticker.canvas import SMALL, Canvas
from ticker.config import Config, load_config
from ticker.modes.commute import (
    _GREEN_MAX,
    _AMBER_MAX,
    AMBER,
    CommuteMode,
    CommuteResult,
    DIRECTIONS_URL,
    GREEN,
    RED,
    TRAVEL_MODES,
    _extract_result,
    _minutes_color,
)


@pytest.fixture
def config(tmp_path):
    return Config(state_dir=tmp_path)


def _lit(canvas: Canvas) -> set[tuple[int, int]]:
    pixels = canvas.image_buffer.load()
    return {
        (x, y)
        for y in range(canvas.height)
        for x in range(canvas.width)
        if pixels[x, y] != (0, 0, 0)
    }


# -- config --------------------------------------------------------------


def test_env_seeds_addresses_and_mode(monkeypatch, tmp_path):
    """First-boot: env vars populate the dataclass, no state files needed."""
    monkeypatch.setenv("COMMUTE_HOME", "221 Clara St, San Francisco CA 94107")
    monkeypatch.setenv("COMMUTE_WORK", "181 Fremont St, San Francisco")
    monkeypatch.setenv("COMMUTE_MODE", "walking")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    # Point state at a fresh dir so we don't pick up a real user's overrides.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.commute_origin == "221 Clara St, San Francisco CA 94107"
    assert cfg.commute_destination == "181 Fremont St, San Francisco"
    assert cfg.commute_mode == "walking"
    assert cfg.google_maps_api_key == "test-key"
    # And the readers -- which callers should use everywhere -- return the
    # same values because no state file overrides them yet.
    assert cfg.current_commute_origin() == cfg.commute_origin
    assert cfg.current_commute_destination() == cfg.commute_destination
    assert cfg.current_commute_mode() == "walking"


def test_state_file_overrides_env_seed(config):
    """A saved override wins over the seed on subsequent boots."""
    config.commute_origin_file.parent.mkdir(parents=True, exist_ok=True)
    config.commute_origin_file.write_text("999 Overridden Ave\n", encoding="utf-8")
    seeded = replace(config, commute_origin="123 Seed St")
    assert seeded.current_commute_origin() == "999 Overridden Ave"


def test_set_addresses_rejects_empty(config):
    """An empty submit shouldn't wipe the last good address pair."""
    with pytest.raises(ValueError):
        config.set_commute_addresses("home", "")


def test_set_addresses_rejects_too_long(config):
    with pytest.raises(ValueError):
        config.set_commute_addresses("a" * 5, "b" * 500)


def test_set_addresses_persists_both(config):
    config.set_commute_addresses("221 Clara St", "181 Fremont St")
    assert config.current_commute_origin() == "221 Clara St"
    assert config.current_commute_destination() == "181 Fremont St"


def test_set_mode_rejects_unknown(config):
    with pytest.raises(ValueError):
        config.set_commute_mode("teleport")


@pytest.mark.parametrize("travel", TRAVEL_MODES)
def test_set_mode_accepts_each_travel_mode(config, travel):
    """The webapp exposes exactly these four; a fifth would drift the UI."""
    assert config.set_commute_mode(travel) == travel
    assert config.current_commute_mode() == travel


def test_current_mode_falls_back_when_state_file_is_junk(config):
    config.commute_mode_file.parent.mkdir(parents=True, exist_ok=True)
    config.commute_mode_file.write_text("teleport\n", encoding="utf-8")
    assert config.current_commute_mode() == "transit"  # dataclass default


# -- parsing -------------------------------------------------------------


def _mock_urlopen(payload: dict):
    """urlopen replacement that returns ``payload`` as JSON, and captures the URL."""
    captured: dict[str, str] = {}

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def _opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response(json.dumps(payload).encode("utf-8"))

    return _opener, captured


def test_extract_result_prefers_traffic_duration():
    """``duration_in_traffic`` is why we ask -- honor it over baseline duration."""
    payload = {
        "status": "OK",
        "routes": [{
            "legs": [{
                "duration": {"value": 600, "text": "10 mins"},
                "duration_in_traffic": {"value": 1200, "text": "20 mins"},
                "distance": {"text": "0.6 mi"},
                "steps": [],
            }],
        }],
    }
    result = _extract_result(payload, "driving", now=1000.0)
    assert result is not None
    assert result.minutes == 20  # from duration_in_traffic
    assert result.hint == "0.6 MI"


def test_extract_result_pulls_transit_line():
    """The hint on transit trips names the first non-walking line."""
    payload = {
        "status": "OK",
        "routes": [{
            "legs": [{
                "duration": {"value": 900, "text": "15 mins"},
                "steps": [
                    {"travel_mode": "WALKING"},
                    {
                        "travel_mode": "TRANSIT",
                        "transit_details": {"line": {"short_name": "38", "name": "Geary"}},
                    },
                ],
            }],
        }],
    }
    result = _extract_result(payload, "transit", now=1000.0)
    assert result is not None
    assert result.hint == "VIA 38"


def test_extract_result_returns_none_on_zero_results():
    """No route means idle placeholder should keep showing, not "0M"."""
    payload = {"status": "ZERO_RESULTS", "routes": []}
    assert _extract_result(payload, "walking", now=1000.0) is None


def test_extract_result_returns_none_on_zero_duration():
    """A payload with a zero-second duration is a bug, not a valid answer."""
    payload = {
        "status": "OK",
        "routes": [{"legs": [{"duration": {"value": 0}}]}],
    }
    assert _extract_result(payload, "walking", now=1000.0) is None


def test_extract_result_rounds_up_from_zero(_=None):
    """A 30-second walk should read 1M, never 0M -- ``max(1, round(...))``."""
    payload = {"status": "OK", "routes": [{"legs": [{"duration": {"value": 30}}]}]}
    result = _extract_result(payload, "walking", now=1000.0)
    assert result is not None
    assert result.minutes == 1


# -- fetch integration ---------------------------------------------------


def test_fetch_writes_result_to_state_file(config):
    """The webapp and renderer are separate processes -- the result MUST be
    file-backed so the LED picks it up after the tap."""
    payload = {
        "status": "OK",
        "routes": [{"legs": [{
            "duration": {"value": 720},
            "duration_in_traffic": {"value": 720},
            "distance": {"text": "0.6 mi"},
            "steps": [],
        }]}],
    }
    cfg = replace(
        config,
        commute_origin="221 Clara St",
        commute_destination="181 Fremont St",
        commute_mode="walking",
        google_maps_api_key="test-key",
    )
    opener, captured = _mock_urlopen(payload)
    mode = CommuteMode(cfg, opener=opener)
    result = mode.fetch()
    assert result is not None
    # The state file exists and round-trips.
    reread = mode._read_result()
    assert reread is not None
    assert reread.minutes == result.minutes
    # And the URL carried the required parameters -- if any of these
    # slipped the request would 400 or return the wrong routing.
    assert DIRECTIONS_URL in captured["url"]
    for needle in ("origin=", "destination=", "mode=walking", "key=test-key", "departure_time=now"):
        assert needle in captured["url"], needle


def test_fetch_requires_api_key(config):
    """Missing key surfaces on the placeholder rather than crashing."""
    cfg = replace(
        config,
        commute_origin="221 Clara St",
        commute_destination="181 Fremont St",
        google_maps_api_key="",
    )
    mode = CommuteMode(cfg, opener=lambda *_a, **_k: pytest.fail("should not be called"))
    assert mode.fetch() is None
    assert mode._error_state == "no_key"


def test_fetch_requires_addresses(config):
    cfg = replace(config, google_maps_api_key="test-key")
    mode = CommuteMode(cfg, opener=lambda *_a, **_k: pytest.fail("should not be called"))
    assert mode.fetch() is None
    assert mode._error_state == "no_route"


def test_fetch_marks_network_error(config):
    cfg = replace(
        config,
        commute_origin="A",
        commute_destination="B",
        google_maps_api_key="test-key",
    )
    # But addresses must be >= min length to bypass the pre-check --
    # so we go through the mode instead of Config so the fetch actually
    # gets to the network layer.
    cfg = replace(
        cfg,
        commute_origin="221 Clara St, SF",
        commute_destination="181 Fremont St, SF",
    )

    def _broken(*_a, **_k):
        raise OSError("no route to host")

    mode = CommuteMode(cfg, opener=_broken)
    assert mode.fetch() is None
    assert mode._error_state == "network"


def test_fetch_marks_api_error_on_non_ok(config):
    cfg = replace(
        config,
        commute_origin="221 Clara St, SF",
        commute_destination="181 Fremont St, SF",
        google_maps_api_key="test-key",
    )
    opener, _ = _mock_urlopen({"status": "REQUEST_DENIED", "error_message": "bad key"})
    mode = CommuteMode(cfg, opener=opener)
    assert mode.fetch() is None
    assert mode._error_state == "api"


def test_fetch_marks_no_route_on_zero_results(config):
    cfg = replace(
        config,
        commute_origin="221 Clara St, SF",
        commute_destination="181 Fremont St, SF",
        google_maps_api_key="test-key",
    )
    opener, _ = _mock_urlopen({"status": "ZERO_RESULTS", "routes": []})
    mode = CommuteMode(cfg, opener=opener)
    assert mode.fetch() is None
    assert mode._error_state == "no_route"


# -- render --------------------------------------------------------------


def test_placeholder_renders_when_no_result(config):
    """Idle card should be non-empty (label + hint) so the LED isn't black."""
    mode = CommuteMode(config)
    canvas = Canvas(128, 32)
    mode.render(canvas, tick=0)
    assert _lit(canvas), "idle placeholder drew nothing"


def test_loaded_card_shows_minutes(config):
    """A cached result should paint minutes and address labels."""
    mode = CommuteMode(config)
    mode._write_result(
        CommuteResult(mode="transit", minutes=14, hint="VIA 38", fetched_epoch=1_700_000_000.0)
    )
    canvas = Canvas(128, 32)
    mode.render(canvas, tick=0)
    assert _lit(canvas)


def test_color_thresholds():
    """Green under 20, amber under 45, red beyond."""
    assert _minutes_color(_GREEN_MAX - 1) == GREEN
    assert _minutes_color(_GREEN_MAX) == AMBER
    assert _minutes_color(_AMBER_MAX - 1) == AMBER
    assert _minutes_color(_AMBER_MAX) == RED


def test_state_snapshot_shape(config):
    """The webapp echoes this dict; if the shape drifts the UI text goes blank."""
    mode = CommuteMode(config)
    idle = mode.state()
    assert idle == {"has_result": False, "error_state": "idle"}
    mode._write_result(
        CommuteResult(mode="driving", minutes=8, hint="0.6 MI", fetched_epoch=1234.0)
    )
    loaded = mode.state()
    assert loaded["has_result"] is True
    assert loaded["mode"] == "driving"
    assert loaded["minutes"] == 8
    assert loaded["hint"] == "0.6 MI"


def test_travel_modes_are_the_google_directions_set():
    """These four are what the Directions API supports for the ``mode`` param.
    Adding a fifth here without upstream support would produce a silent 400."""
    assert TRAVEL_MODES == ("transit", "driving", "walking", "bicycling")


def test_placeholder_labels_fit_the_panel():
    """Every placeholder string must fit the panel's content column.

    These labels are hand-edited copy (the idle one was reworded once already,
    from "TAP TO ROUTE" to something that names the web app instead of implying
    the panel is a touchscreen). The content column starts at x=20, so a long
    edit would run off the right edge -- and because the renderer draws to a
    fixed-size canvas, the overflow is silently clipped rather than raising.
    """
    canvas = Canvas(128, 32)
    available = canvas.width - 20
    for state, (label, detail, _color) in CommuteMode._PLACEHOLDER_LABELS.items():
        for text in (label, detail):
            width = canvas.text_width(text, SMALL)
            assert width <= available, (
                f"{state!r} placeholder text {text!r} is {width}px, "
                f"which exceeds the {available}px content column"
            )


def test_idle_placeholder_does_not_imply_a_touchscreen():
    """The panel has no touch input. The idle label must point at the web app.

    Regression guard: the first version read "TAP TO ROUTE", which was read as
    an instruction to tap the LED matrix itself.
    """
    _label, detail, _color = CommuteMode._PLACEHOLDER_LABELS["idle"]
    assert "WEB" in detail.upper()
    assert detail.upper() != "TAP TO ROUTE"
