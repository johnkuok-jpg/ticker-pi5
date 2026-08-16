# MIT License — Copyright (c) 2026 John Kuok
"""Tests for the quake-alert watcher.

The watcher owns three subtle behaviours that are easy to get wrong:

* Region filter -- California via substring OR bounding box, other regions via
  substring only, empty region = worldwide.
* Freshness gate -- a stale event at boot must not fire the panel.
* Dedup -- a repeated poll seeing the same event must not re-fire.

Each of those gets its own test. The USGS fetch is stubbed out with a
JSON-serving fake so nothing hits the network.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ticker.config import load_config
from ticker.quake_alert import QuakeAlert, QuakeAlertWatcher


def _feature(*, event_id: str, mag: float, place: str, time_ms: int, lon: float, lat: float) -> dict:
    return {
        "id": event_id,
        "properties": {"mag": mag, "place": place, "time": time_ms},
        "geometry": {"coordinates": [lon, lat, 5.0]},
    }


def _feed(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _fake_opener(payload: dict):
    """Return a callable that mimics urllib.request.urlopen returning *payload*."""

    def opener(_request, timeout=None):  # noqa: ARG001
        body = json.dumps(payload).encode("utf-8")

        class _Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp()

    return opener


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ticker.config._first_writable_state_dir", lambda: tmp_path / "state"
    )
    # Force alerting on so the fixture doesn't have to re-check env parsing.
    monkeypatch.setenv("QUAKE_ALERT_ENABLED", "true")
    monkeypatch.setenv("QUAKE_ALERT_MIN_MAG", "3.0")
    monkeypatch.setenv("QUAKE_ALERT_REGION", "California")
    monkeypatch.setenv("QUAKE_ALERT_DWELL_SECONDS", "120")
    return load_config()


def _watcher(config, opener, *, wall_now: float, mono_now: float = 100.0) -> QuakeAlertWatcher:
    return QuakeAlertWatcher(
        config,
        opener=opener,
        now_monotonic=lambda: mono_now,
        now_seconds=lambda: wall_now,
        poll_interval=0,  # always poll on tick()
    )


def test_alert_fires_for_california_event_above_threshold(config):
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="nc123",
                mag=3.6,
                place="6km NNW of The Geysers, CA",
                time_ms=int((now_wall - 60) * 1000),  # 1 min old
                lon=-122.79,
                lat=38.79,
            ),
        ]
    )
    w = _watcher(config, _fake_opener(feed), wall_now=now_wall)
    w.tick()
    alert = w.current_alert()
    assert alert is not None
    assert alert.event_id == "nc123"
    assert alert.magnitude == pytest.approx(3.6)


def test_bounding_box_catches_events_without_california_in_place(config):
    """USGS occasionally omits 'California' from offshore aftershocks. Bbox picks them up."""
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="offshore1",
                mag=4.1,
                place="offshore Northern region",  # no 'California'
                time_ms=int((now_wall - 30) * 1000),
                lon=-124.0,
                lat=40.5,  # Mendocino coast
            ),
        ]
    )
    w = _watcher(config, _fake_opener(feed), wall_now=now_wall)
    w.tick()
    assert w.current_alert() is not None


def test_out_of_region_events_do_not_fire(config):
    """A big Alaska shake shouldn't hijack a California-configured panel."""
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="ak999",
                mag=5.4,
                place="120km SW of Anchorage, Alaska",
                time_ms=int((now_wall - 60) * 1000),
                lon=-150.0,
                lat=61.0,
            ),
        ]
    )
    w = _watcher(config, _fake_opener(feed), wall_now=now_wall)
    w.tick()
    assert w.current_alert() is None


def test_below_threshold_events_do_not_fire(config):
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="nc-small",
                mag=2.8,  # below M3.0
                place="5km E of Berkeley, CA",
                time_ms=int((now_wall - 60) * 1000),
                lon=-122.25,
                lat=37.87,
            ),
        ]
    )
    w = _watcher(config, _fake_opener(feed), wall_now=now_wall)
    w.tick()
    assert w.current_alert() is None


def test_stale_event_at_boot_does_not_fire_but_is_remembered(config):
    """A 45-minute-old event is past the MAX_FRESH_SECONDS gate -- ignore it."""
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="nc-stale",
                mag=4.2,
                place="Central California",
                time_ms=int((now_wall - 45 * 60) * 1000),
                lon=-121.0,
                lat=36.5,
            ),
        ]
    )
    w = _watcher(config, _fake_opener(feed), wall_now=now_wall)
    w.tick()
    assert w.current_alert() is None
    # It IS in the seen list, so a fresh poll wouldn't reconsider it if USGS
    # kept publishing the same feature. (Belt-and-braces against re-fires.)
    assert "nc-stale" in w._seen_ids


def test_repeated_polls_do_not_refire_same_event(config):
    """Second tick sees the same event; must not restart the dwell window."""
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="nc-repeat",
                mag=3.5,
                place="8km SE of Livermore, CA",
                time_ms=int((now_wall - 60) * 1000),
                lon=-121.72,
                lat=37.62,
            ),
        ]
    )
    monotone = [100.0]
    w = QuakeAlertWatcher(
        config,
        opener=_fake_opener(feed),
        now_monotonic=lambda: monotone[0],
        now_seconds=lambda: now_wall,
        poll_interval=0,
    )
    w.tick()
    initial = w.current_alert()
    assert initial is not None
    # Advance monotonic but keep alert file present; second tick would try to
    # fire again but the seen-set blocks it.
    monotone[0] += 5
    w.tick()
    second = w.current_alert()
    assert second is not None
    assert second.first_detected_monotonic == initial.first_detected_monotonic


def test_alert_expires_after_dwell_window(config):
    """Reading the file after dwell removes it and returns None."""
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="nc-expiry",
                mag=3.2,
                place="Central California",
                time_ms=int((now_wall - 60) * 1000),
                lon=-121.0,
                lat=36.5,
            ),
        ]
    )
    monotone = [100.0]
    w = QuakeAlertWatcher(
        config,
        opener=_fake_opener(feed),
        now_monotonic=lambda: monotone[0],
        now_seconds=lambda: now_wall,
        poll_interval=0,
    )
    w.tick()
    assert w.current_alert() is not None
    # Advance monotonic past the dwell window and verify the file is gone.
    monotone[0] += config.quake_alert_dwell_seconds + 1
    assert w.current_alert() is None
    assert not w.alert_file.exists()


def test_disabled_watcher_never_polls(config, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "ticker.config._first_writable_state_dir", lambda: tmp_path / "state2"
    )
    monkeypatch.setenv("QUAKE_ALERT_ENABLED", "false")
    disabled_cfg = load_config()
    opener = MagicMock()
    w = QuakeAlertWatcher(
        disabled_cfg,
        opener=opener,
        now_monotonic=lambda: 100.0,
        now_seconds=lambda: 1_700_000_000.0,
        poll_interval=0,
    )
    w.tick()
    opener.assert_not_called()
    assert w.current_alert() is None


def test_worldwide_region_matches_any_place(config, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "ticker.config._first_writable_state_dir", lambda: tmp_path / "state3"
    )
    monkeypatch.setenv("QUAKE_ALERT_REGION", "")
    global_cfg = load_config()
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="jp1",
                mag=5.9,
                place="Off the coast of Fukushima, Japan",
                time_ms=int((now_wall - 60) * 1000),
                lon=141.0,
                lat=37.5,
            ),
        ]
    )
    w = _watcher(global_cfg, _fake_opener(feed), wall_now=now_wall)
    w.tick()
    assert w.current_alert() is not None


def test_corrupt_alert_file_is_ignored_and_removed(config):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "quake_alert").write_text("not-json", encoding="utf-8")
    w = _watcher(config, _fake_opener(_feed([])), wall_now=1_700_000_000.0)
    assert w.current_alert() is None
    assert not (config.state_dir / "quake_alert").exists()


def test_clear_removes_active_alert(config):
    now_wall = 1_700_000_000.0
    feed = _feed(
        [
            _feature(
                event_id="nc-clear",
                mag=4.0,
                place="Central California",
                time_ms=int((now_wall - 60) * 1000),
                lon=-121.0,
                lat=36.5,
            ),
        ]
    )
    w = _watcher(config, _fake_opener(feed), wall_now=now_wall)
    w.tick()
    assert w.current_alert() is not None
    w.clear()
    assert w.current_alert() is None
