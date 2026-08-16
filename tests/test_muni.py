# MIT License — Copyright (c) 2026 John Kuok
"""SF Muni mode: client parsing + wordmark + arrivals card layout.

These pin the pieces that would silently break: the umoiq payload parser
shrugging off missing fields, the route-colour luma floor, the "muni"
worm-style wordmark's bounding box, and the arrivals card actually
drawing rows the panel can read.
"""

from __future__ import annotations

import json

import pytest

from ticker import muni
from ticker.canvas import Canvas
from ticker.modes.muni import (
    MUNI_RED,
    MuniMode,
    WORDMARK_HEIGHT,
    draw_worm,
    wordmark_width,
)


@pytest.fixture(autouse=True)
def _reset_muni_cache():
    muni._reset_for_tests()
    yield
    muni._reset_for_tests()


def _lit(canvas: Canvas) -> set[tuple[int, int]]:
    """Coordinates of every non-black pixel on the canvas."""
    pixels = canvas.image_buffer.load()
    return {
        (x, y)
        for y in range(canvas.height)
        for x in range(canvas.width)
        if pixels[x, y] != (0, 0, 0)
    }


# ---------------------------------------------------------------------------
# Stop code shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["13463", "1234", "123456"])
def test_stop_code_accepts_shelter_digits(value: str) -> None:
    assert muni.is_stop_code(value)


@pytest.mark.parametrize("value", ["", "abc", "12", "1234567", "13a63", None])
def test_stop_code_rejects_junk(value) -> None:  # type: ignore[no-untyped-def]
    assert not muni.is_stop_code(value)


# ---------------------------------------------------------------------------
# Route label + destination trimming
# ---------------------------------------------------------------------------


def test_route_label_uppercases_and_trims() -> None:
    assert muni.route_label("38") == "38"
    assert muni.route_label(" n ") == "N"
    assert muni.route_label("38r") == "38R"
    assert muni.route_label("") == ""


def test_route_label_caps_absurd_ids() -> None:
    """A cursed 20-char id must not blow the badge column."""
    label = muni.route_label("SOME-MADE-UP-LONG-ID")
    assert len(label) <= 6


def test_shorten_cleans_nextbus_glue() -> None:
    assert muni._shorten("24th St + Castro St") == "24TH ST & CASTRO ST".title().replace(
        "Th", "th"
    ) or muni._shorten("24th St + Castro St") == "24th St & Castro St"


def test_shorten_drops_parenthetical_qualifiers() -> None:
    assert muni._shorten("Ocean Beach (last stop)") == "Ocean Beach"


def test_shorten_hard_truncates_with_ellipsis() -> None:
    long = "A" * 80
    trimmed = muni._shorten(long)
    assert len(trimmed) < len(long)
    assert trimmed.endswith("\u2026")


# ---------------------------------------------------------------------------
# Route colour luma floor
# ---------------------------------------------------------------------------


def test_brighten_scales_dark_colours_uniformly() -> None:
    """Dark teal must be scaled toward its own hue, not clipped."""
    dim_teal = (10, 30, 40)
    out = muni._brighten_to_floor(dim_teal)
    # Every channel got scaled by the same factor, hue preserved.
    ratios = [o / c for o, c in zip(out, dim_teal) if c > 0]
    assert max(ratios) - min(ratios) < 0.01, ratios
    # And it cleared the floor.
    assert muni._luma(out) >= 90 - 1  # rounding


def test_brighten_leaves_bright_colours_alone() -> None:
    bright = (240, 180, 40)
    assert muni._brighten_to_floor(bright) == bright


def test_brighten_falls_back_for_pure_black() -> None:
    assert muni._brighten_to_floor((0, 0, 0)) == (235, 240, 250)


# ---------------------------------------------------------------------------
# Predictions parser
# ---------------------------------------------------------------------------


_SYNTHETIC_PREDICTIONS = [
    {
        "agency": {"id": "sfmta-cis"},
        "route": {"id": "38", "title": "Geary", "color": "#003366", "textColor": "#FFFFFF"},
        "stop": {"id": "s1", "code": "13463", "name": "24th St & Castro St", "lat": 37.75, "lon": -122.43},
        "values": [
            {
                "minutes": 4,
                "direction": {"destinationName": "Ocean Beach", "name": "Outbound"},
                "vehicleId": "1234",
                "isDeparture": True,
                "delay": 0,
            },
            {
                "minutes": 12,
                "direction": {"destinationName": "Ocean Beach", "name": "Outbound"},
                "vehicleId": "1235",
                "isDeparture": True,
                "delay": 0,
            },
        ],
    },
    {
        "agency": {"id": "sfmta-cis"},
        "route": {"id": "T", "title": "T Third", "color": "#B10DC9", "textColor": "#FFFFFF"},
        "stop": {"id": "s1", "code": "13463", "name": "24th St & Castro St", "lat": 37.75, "lon": -122.43},
        "values": [
            {
                "minutes": 0,
                "direction": {"destinationName": "Sunnydale", "name": "Inbound"},
                "vehicleId": "T44",
                "isDeparture": True,
                "delay": 0,
            }
        ],
    },
]


def test_parse_predictions_sorts_and_shapes_arrivals() -> None:
    parsed = muni._parse_predictions("13463", _SYNTHETIC_PREDICTIONS)
    assert parsed is not None
    assert parsed.stop_code == "13463"
    assert parsed.stop_name == "24th St & Castro St"
    assert [a.minutes for a in parsed.arrivals] == [0, 4, 12]
    assert parsed.arrivals[0].route == "T"
    assert parsed.arrivals[0].is_leaving is True
    assert parsed.arrivals[0].countdown() == "NOW"
    assert parsed.arrivals[1].countdown() == "4M"
    assert parsed.arrivals[1].destination == "Ocean Beach"


def test_parse_predictions_returns_empty_on_empty_list() -> None:
    parsed = muni._parse_predictions("13463", [])
    assert parsed is not None
    assert parsed.arrivals == ()


def test_parse_predictions_rejects_non_list_payload() -> None:
    assert muni._parse_predictions("13463", {"oops": True}) is None
    assert muni._parse_predictions("13463", None) is None


def test_lookup_hits_fake_opener_and_caches() -> None:
    """A live network call would flake; inject a bytes-returning opener."""
    calls = []

    def opener(url: str) -> bytes:
        calls.append(url)
        return json.dumps(_SYNTHETIC_PREDICTIONS).encode()

    muni.set_opener(opener)
    result = muni.lookup("13463")
    assert result is not None
    assert result.arrivals[0].route == "T"
    # Second call inside TTL must not re-fetch.
    muni.lookup("13463")
    assert len(calls) == 1


def test_lookup_returns_none_for_bad_stopcode() -> None:
    assert muni.lookup("abc") is None
    assert muni.lookup("") is None


# ---------------------------------------------------------------------------
# Worm wordmark
# ---------------------------------------------------------------------------


def test_worm_fits_declared_box() -> None:
    canvas = Canvas(128, 32)
    draw_worm(canvas, 0, 0)
    lit = _lit(canvas)
    assert lit, "worm drew nothing"
    assert max(x for x, _ in lit) < wordmark_width(canvas)
    assert max(y for _, y in lit) < WORDMARK_HEIGHT


def test_worm_is_muni_red() -> None:
    canvas = Canvas(128, 32)
    draw_worm(canvas, 0, 0)
    pixels = canvas.image_buffer.load()
    colors = {pixels[x, y] for x, y in _lit(canvas)}
    assert colors == {MUNI_RED}


# ---------------------------------------------------------------------------
# Mode rendering
# ---------------------------------------------------------------------------


class _Config:
    """Minimum surface MuniMode needs, matching the other mode tests."""

    fps = 30

    def __init__(self, stop: str = "13463") -> None:
        self._stop = stop

    def current_muni_stop(self) -> str:
        return self._stop

    def clock_text(self) -> str:
        return "5:07P"


def _seeded_mode(stop: str = "13463") -> MuniMode:
    def opener(url: str) -> bytes:
        return json.dumps(_SYNTHETIC_PREDICTIONS).encode()

    muni.set_opener(opener)
    return MuniMode(_Config(stop))  # type: ignore[arg-type]


def test_mode_renders_header_and_rows() -> None:
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    lit = _lit(canvas)
    # Header band and each of the three arrival rows have content.
    assert any(y < WORDMARK_HEIGHT for _, y in lit), "header empty"
    for row in (8, 16, 24):
        assert any(y in (row, row + 1, row + 2, row + 3, row + 4, row + 5) for _, y in lit), (
            f"row {row} empty"
        )


def test_mode_places_worm_in_muni_red() -> None:
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    header_colors = {pixels[x, y] for x, y in _lit(canvas) if y < WORDMARK_HEIGHT and x < wordmark_width(canvas)}
    assert MUNI_RED in header_colors


def test_mode_shows_pick_stop_when_unset() -> None:
    mode = MuniMode(_Config(""))  # type: ignore[arg-type]
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    lit = _lit(canvas)
    assert lit, "unset state drew nothing"


def test_mode_refetches_when_stop_changes() -> None:
    calls: list[str] = []

    def opener(url: str) -> bytes:
        calls.append(url)
        return json.dumps(_SYNTHETIC_PREDICTIONS).encode()

    muni.set_opener(opener)
    config = _Config("13463")
    mode = MuniMode(config)  # type: ignore[arg-type]
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    first = len(calls)
    # Same stop, same render: cache should hold.
    mode.render(canvas, 1)
    assert len(calls) == first
    # New stop: cache must be bypassed so the rider isn't stranded on the
    # previous stop's arrivals until the TTL rolls over.
    config._stop = "13462"
    mode.render(canvas, 2)
    assert len(calls) == first + 1
