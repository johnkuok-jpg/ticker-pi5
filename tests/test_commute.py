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
    _CONTENT_X,
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
    _format_duration,
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


def test_format_duration_switches_to_hours_at_sixty():
    """``72 min`` on transit reads worse than ``1H12``; phones' commute
    widgets label the same way. Boundaries checked:

    - Under 60 keeps the trailing ``M`` (compact and consistent with the
      panel-wide use of terse suffixes elsewhere).
    - Exactly 60 flips to ``1H`` -- ``1H0`` looks like a placeholder.
    - Past the top of the hour zero-pads: ``1H05`` not ``1H5``, so the
      right-flushed label doesn't jitter one character every 5 minutes.
    - Multi-hour still works.
    - Negative minutes never render as ``-1H`` -- if a bad upstream payload
      sneaks in, we clamp to zero rather than draw a minus sign the LED
      panel would misrender anyway.
    """
    assert _format_duration(0) == "0M"
    assert _format_duration(9) == "9M"
    assert _format_duration(59) == "59M"
    assert _format_duration(60) == "1H"
    assert _format_duration(61) == "1H01"
    assert _format_duration(72) == "1H12"
    assert _format_duration(119) == "1H59"
    assert _format_duration(120) == "2H"
    assert _format_duration(150) == "2H30"
    assert _format_duration(-5) == "0M"


def test_format_duration_never_exceeds_the_minutes_column():
    """The label is right-flushed against the panel edge, with the ``HOME ->
    WORK`` route text on its left. The wide-value budget is set by the
    pre-hour formatter's worst case (``59M``, 18px in MEDIUM) plus one
    character for the ``H`` in ``NHmm``. 4-character labels like ``1H12``
    and ``9H59`` are 24px, which leaves >=40px of gap against the route
    text at every realistic Directions duration.
    """
    from ticker.canvas import Canvas, MEDIUM

    canvas = Canvas(128, 32)
    # 24px covers every 4-character ``NHmm`` label; a hard cap here catches
    # a future format change (e.g. adding a trailing ``M``, spaces, or a
    # 5-character variant) before it lands on the panel and clips WORK.
    for minutes in (60, 61, 72, 119, 120, 599):
        label = _format_duration(minutes)
        assert canvas.text_width(label, MEDIUM) <= 24, (minutes, label)


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
    the panel is a touchscreen). The content column starts at ``_CONTENT_X``,
    so a long edit would run off the right edge -- and because the renderer draws to a
    fixed-size canvas, the overflow is silently clipped rather than raising.
    """
    canvas = Canvas(128, 32)
    available = canvas.width - _CONTENT_X
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


def test_icon_column_glyphs_are_uniform():
    """Every mode icon is exactly 24x16 and fits the reserved column.

    The column width and the glyph size are declared in two places, and a
    mismatch does not raise: `_draw_icon` blits pixel by pixel, so an oversized
    glyph would quietly paint over the text to its right instead of failing.
    """
    from ticker.modes.commute import _CONTENT_X, _ICON_WIDTH, _ICONS, _WALK_FRAMES

    for name, glyph in list(_ICONS.items()) + [
        (f"walk[{i}]", frame) for i, frame in enumerate(_WALK_FRAMES)
    ]:
        assert len(glyph) == 16, f"{name} is {len(glyph)} rows, want 16"
        for row in glyph:
            assert len(row) == _ICON_WIDTH, f"{name} row is {len(row)}, want {_ICON_WIDTH}"
            assert set(row) <= {"#", "."}, f"{name} row has stray characters: {row!r}"
    assert _ICON_WIDTH < _CONTENT_X, "icons need a gutter before the text column"


def test_every_travel_mode_has_an_icon():
    """A mode with no icon silently falls back to the transit one."""
    from ticker.modes.commute import _ICONS, TRAVEL_MODES

    assert set(TRAVEL_MODES) <= set(_ICONS)


def test_walk_cycles_through_its_frames_and_repeats():
    """The stride is tick-driven, deterministic, and returns to its start.

    Tick-driven rather than clock-driven so a dropped render does not skip the
    animation forward, and so this test can assert on an exact frame.
    """
    from ticker.modes.commute import (
        _WALK_FRAMES,
        _WALK_SEQUENCE,
        _WALK_STEPS_PER_SECOND,
        _walk_frame,
    )

    fps = 30
    per_step = fps // _WALK_STEPS_PER_SECOND
    seen = [_walk_frame(step * per_step, fps) for step in range(len(_WALK_SEQUENCE))]
    assert seen == [_WALK_FRAMES[i] for i in _WALK_SEQUENCE]
    # Assert the motion itself, not just that the sequence was followed:
    # comparing against _WALK_SEQUENCE is tautological, so a sequence of
    # (0, 0, 0, 0) -- a frozen figure -- would otherwise pass.
    assert len(set(seen)) > 1, "the walk cycle never changes pose"
    for step, (before, after) in enumerate(zip(seen, seen[1:] + seen[:1])):
        assert before != after, f"step {step} holds the same pose as the next"
    # Wraps cleanly, and holds each frame for the whole step.
    assert _walk_frame(len(_WALK_SEQUENCE) * per_step, fps) == seen[0]
    assert _walk_frame(per_step - 1, fps) == seen[0]
    assert _walk_frame(per_step, fps) == seen[1]
    # Every pose is distinct, or the animation would visibly stall.
    assert len({frame for frame in _WALK_FRAMES}) == len(_WALK_FRAMES)


def test_walk_frame_survives_a_low_fps():
    """fps is configurable; fps // steps must not become a zero divisor."""
    from ticker.modes.commute import _walk_frame

    assert _walk_frame(0, fps=1) is not None
    assert _walk_frame(3, fps=1) is not None


def test_panel_text_is_ascii(config, tmp_path):
    """Panel strings must be ASCII, because a missing glyph fails silently.

    The Spleen bitmap fonts advance the cursor for a character they do not have
    and draw nothing, so a middot renders as a stray gap rather than an error.
    This caught "STALE" being separated by one.
    """
    import json
    import time

    mode = CommuteMode(config)
    for age in (0, mode.STALE_SECONDS + 60):
        (config.state_dir / "commute_result.json").write_text(
            json.dumps({
                "minutes": 23,
                "mode": "walking",
                "hint": "1.2 MI",
                "fetched_epoch": time.time() - age,
            })
        )
        drawn: list[str] = []
        canvas = Canvas(128, 32)
        original = canvas.text

        def record(x, y, text, font, color, _orig=original):
            drawn.append(text)
            return _orig(x, y, text, font, color)

        canvas.text = record  # type: ignore[method-assign]
        mode.render(canvas, 0)
        assert drawn, "nothing was drawn"
        for text in drawn:
            assert text.isascii(), f"non-ASCII panel text {text!r} will render with gaps"


def test_freshness_stamp_fits_the_narrower_content_column(config):
    """The stamp is the widest line on the card, so it sets the column budget."""
    import json
    import time

    from ticker.modes.commute import _CONTENT_X

    canvas = Canvas(128, 32)
    available = canvas.width - _CONTENT_X
    mode = CommuteMode(config)
    for age in (0, mode.STALE_SECONDS + 60):
        (config.state_dir / "commute_result.json").write_text(
            json.dumps({
                "minutes": 23,
                "mode": "transit",
                "hint": "VIA 38",
                "fetched_epoch": time.time() - age,
            })
        )
        drawn: list[str] = []
        original = canvas.text
        canvas.text = lambda x, y, t, f, c, _o=original: (drawn.append(t), _o(x, y, t, f, c))[1]  # type: ignore[method-assign]
        mode.render(canvas, 0)
        for text in drawn:
            assert canvas.text_width(text, SMALL) <= available, (
                f"{text!r} is {canvas.text_width(text, SMALL)}px, over the "
                f"{available}px column"
            )


# --- address autocomplete ---------------------------------------------------
#
# Autocomplete is billable per request, so these tests pin the request shape
# (field mask, bias, method) as much as the parsing: a wrong field mask still
# returns results, it just silently bills a more expensive SKU.


def _places_opener(payload, captured=None, error=None):
    """Fake urlopen for the Places endpoint."""
    import io
    from contextlib import contextmanager

    @contextmanager
    def opener(request, timeout=None):  # type: ignore[no-untyped-def]
        if captured is not None:
            captured.append(request)
        if error is not None:
            raise error
        yield io.BytesIO(json.dumps(payload).encode("utf-8"))

    return opener


def _suggestion(text):
    return {"placePrediction": {"text": {"text": text}}}


def test_autocomplete_returns_formatted_addresses():
    from ticker.modes.commute import autocomplete_addresses

    payload = {"suggestions": [
        _suggestion("181 Fremont St, San Francisco, CA 94105, USA"),
        _suggestion("181 Fremont Ave, Los Altos, CA, USA"),
    ]}
    result = autocomplete_addresses("181 fremont", "key", opener=_places_opener(payload))
    # The trailing ", USA" is trimmed -- see test_autocomplete_trims_the_country_suffix.
    assert result == [
        "181 Fremont St, San Francisco, CA 94105",
        "181 Fremont Ave, Los Altos, CA",
    ]


def test_autocomplete_request_shape_keeps_the_cheap_billing_tier():
    """Field mask, method and headers are a billing contract, not a preference.

    Requesting a field outside the Essentials tier silently promotes the whole
    request to a pricier SKU, and Places Autocomplete is a POST to
    places.googleapis.com -- not a GET on the Directions host.
    """
    from ticker.modes.commute import PLACES_AUTOCOMPLETE_URL, autocomplete_addresses

    captured: list = []
    autocomplete_addresses(
        "181 fremont", "secret-key",
        opener=_places_opener({"suggestions": []}, captured=captured),
    )
    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == PLACES_AUTOCOMPLETE_URL
    assert request.get_method() == "POST"
    assert request.headers["X-goog-api-key"] == "secret-key"
    assert request.headers["X-goog-fieldmask"] == "suggestions.placePrediction.text.text"
    body = json.loads(request.data.decode("utf-8"))
    assert body["input"] == "181 fremont"
    # Bias, not restrict: an out-of-area address must still be reachable.
    assert "locationBias" in body
    assert "locationRestriction" not in body


def test_autocomplete_skips_short_queries_without_spending_a_request():
    from ticker.modes.commute import AUTOCOMPLETE_MIN_CHARS, autocomplete_addresses

    captured: list = []
    opener = _places_opener({"suggestions": []}, captured=captured)
    short = "x" * (AUTOCOMPLETE_MIN_CHARS - 1)
    assert autocomplete_addresses(short, "key", opener=opener) == []
    assert captured == [], "a sub-minimum query must not reach the API"


def test_autocomplete_dedupes_and_caps_results():
    from ticker.modes.commute import autocomplete_addresses

    payload = {"suggestions": [_suggestion("181 Fremont St")] * 3
               + [_suggestion(f"{n} Fremont St") for n in range(10)]}
    result = autocomplete_addresses("fremont", "key", opener=_places_opener(payload), limit=4)
    assert len(result) == 4
    assert len(set(result)) == 4


def test_autocomplete_without_a_key_is_unavailable_not_empty():
    """An empty list would read as "no such address", which is a different bug."""
    from ticker.modes.commute import AutocompleteUnavailable, autocomplete_addresses

    with pytest.raises(AutocompleteUnavailable) as excinfo:
        autocomplete_addresses("181 fremont", "   ")
    assert excinfo.value.reason == "no_key"


@pytest.mark.parametrize(
    "code,expected",
    [(403, "not_enabled"), (401, "not_enabled"), (429, "api"), (500, "api")],
)
def test_autocomplete_maps_http_errors_to_actionable_reasons(code, expected):
    """403 means Places API (New) is off or missing from the key restrictions.

    That is a one-time Cloud console fix, so it must not be reported as a
    generic API error -- the user needs to know which knob to turn.
    """
    import urllib.error

    from ticker.modes.commute import AutocompleteUnavailable, autocomplete_addresses

    err = urllib.error.HTTPError(
        "https://places.googleapis.com", code, "boom", {}, io.BytesIO(b"denied")
    )
    with pytest.raises(AutocompleteUnavailable) as excinfo:
        autocomplete_addresses("181 fremont", "key", opener=_places_opener({}, error=err))
    assert excinfo.value.reason == expected


def test_autocomplete_network_failure_is_unavailable():
    import urllib.error

    from ticker.modes.commute import AutocompleteUnavailable, autocomplete_addresses

    with pytest.raises(AutocompleteUnavailable) as excinfo:
        autocomplete_addresses(
            "181 fremont", "key",
            opener=_places_opener({}, error=urllib.error.URLError("offline")),
        )
    assert excinfo.value.reason == "network"


def test_autocomplete_trims_the_country_suffix():
    """", USA" is never the distinguishing part of a San Francisco commute.

    It is also ~5 characters that pushed every row of the dropdown onto a
    second line. Dropping it is routing-safe: the state and ZIP already pin the
    address for Directions.
    """
    from ticker.modes.commute import autocomplete_addresses

    payload = {"suggestions": [
        _suggestion("181 Fremont Street, San Francisco, CA 94105, USA"),
        _suggestion("10 Downing St, London, United Kingdom"),
    ]}
    assert autocomplete_addresses("x" * 3, "key", opener=_places_opener(payload)) == [
        "181 Fremont Street, San Francisco, CA 94105",
        # Only USA/United States are trimmed; other countries are meaningful.
        "10 Downing St, London, United Kingdom",
    ]
