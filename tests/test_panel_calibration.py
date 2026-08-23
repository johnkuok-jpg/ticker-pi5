# MIT License — Copyright (c) 2026 John Kuok
"""Tests for per-half panel brightness calibration.

The two chained 64x32 panels are rarely a perfect brightness match, so each
half gets a gain applied after the global brightness scale. Three things are
worth pinning:

1. The config layer round-trips and clamps gains, and the flat-grey test
   target expires on its own rather than stranding the panel.
2. The renderer scales the correct AXIS. `np.asarray` on a PIL RGB image is
   ``(height, width, 3)``, so the halves are a slice of axis 1; slicing axis 0
   by mistake would band the display top/bottom and still look "calibrated"
   in a screenshot.
3. Gains above 1.0 clip rather than wrap. uint8 wraps on overflow, which would
   turn a white pixel into near-black speckle.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ticker.config import load_config
from ticker.web.app import create_app


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text("TICKER_WIDTH=128\nTICKER_HEIGHT=32\n", encoding="utf-8")
    return load_config(env)


# -- config layer ----------------------------------------------------------


def test_settings_page_renders_without_the_calibration_context() -> None:
    """The settings page must render even when the route omits the gains.

    This is not hypothetical. ``git pull`` replaces the template on disk the
    moment it lands, but a running gunicorn worker keeps serving the old
    route code until ticker-web is restarted -- and Jinja reads a template
    from disk the first time it is asked for it. So a worker that predates
    this feature can be handed the template that ships with it, and a bare
    ``{{ panel_left_gain }}`` raises UndefinedError: HTTP 500 on the one
    page that is also the Wi-Fi recovery screen, which is exactly the screen
    someone needs when the ticker is unreachable any other way. Defaults in
    the template turn that into a card showing 100%.
    """
    from flask import render_template

    from ticker import net as net_module
    from ticker.config import VALID_MODES

    app = create_app()
    with app.test_request_context("/settings"):
        html = render_template(
            "settings.html",
            available=net_module.available(),
            setup_ssid=net_module.HOTSPOT_SSID,
            all_modes=VALID_MODES,
            hidden_modes=[],
            mode_labels={},
        )

    assert "Panel calibration" in html
    # Both sliders fall back to unity gain rather than blowing up.
    assert html.count('value="100"') >= 2, "expected both gains to default to 100%"


def test_calibration_defaults_to_unity(config) -> None:  # type: ignore[no-untyped-def]
    assert config.current_panel_calibration() == (1.0, 1.0)


def test_calibration_round_trips(config) -> None:  # type: ignore[no-untyped-def]
    assert config.set_panel_calibration(0.9, 1.05) == (0.9, 1.05)
    left, right = config.current_panel_calibration()
    assert (round(left, 3), round(right, 3)) == (0.9, 1.05)


def test_calibration_clamps_out_of_range_gains(config) -> None:  # type: ignore[no-untyped-def]
    assert config.set_panel_calibration(0.1, 9.0) == (
        config.PANEL_GAIN_MIN,
        config.PANEL_GAIN_MAX,
    )


def test_calibration_rejects_non_numbers(config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        config.set_panel_calibration("bright", 1.0)


def test_calibration_rejects_nan(config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        config.set_panel_calibration(float("nan"), 1.0)


def test_corrupt_calibration_file_falls_back_to_unity(config) -> None:  # type: ignore[no-untyped-def]
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.panel_calibration_file.write_text("garbage\n", encoding="utf-8")
    assert config.current_panel_calibration() == (1.0, 1.0)


def test_test_pattern_defaults_off_and_round_trips(config) -> None:  # type: ignore[no-untyped-def]
    assert config.current_panel_calibration_test() is False
    config.set_panel_calibration_test(True)
    assert config.current_panel_calibration_test() is True
    config.set_panel_calibration_test(False)
    assert config.current_panel_calibration_test() is False


def test_test_pattern_expires_on_its_own(config, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A phone closed mid-calibration must not leave the panel on grey."""
    config.set_panel_calibration_test(True)
    stale = config.now().timestamp() - config.PANEL_TEST_TTL_SEC - 1
    config.panel_calibration_test_file.write_text(f"{stale:.0f}\n", encoding="utf-8")
    assert config.current_panel_calibration_test() is False


# -- renderer math ---------------------------------------------------------
#
# The renderer loop needs a live matrix, so rather than driving run() these
# reproduce its scaling step exactly and assert on the result. If the loop's
# arithmetic changes, `test_renderer_uses_the_same_scaling_expression` below
# is the tripwire.


def _scale(pixels: np.ndarray, brightness: float, gains: tuple[float, float], seam: int):
    out = pixels * brightness
    left_gain, right_gain = gains
    if left_gain != 1.0:
        out[:, :seam] *= left_gain
    if right_gain != 1.0:
        out[:, seam:] *= right_gain
    return np.clip(out, 0, 255).astype(np.uint8)


def test_gains_apply_to_columns_not_rows() -> None:
    """The seam is vertical: a left gain must not dim the top of the display."""
    pixels = np.full((32, 128, 3), 200.0, dtype=np.float32)
    out = _scale(pixels, 1.0, (0.5, 1.0), seam=64)
    # Left half dimmed, right half untouched.
    assert out[:, :64].max() == 100
    assert out[:, 64:].min() == 200
    # Every row treated identically -- no horizontal banding.
    assert out[0, 0, 0] == out[31, 0, 0]


def test_right_gain_only_touches_the_right_half() -> None:
    pixels = np.full((32, 128, 3), 100.0, dtype=np.float32)
    out = _scale(pixels, 1.0, (1.0, 0.8), seam=64)
    assert out[:, :64].min() == 100
    assert out[:, 64:].max() == 80


def test_gain_above_one_clips_instead_of_wrapping() -> None:
    """uint8 wraps on overflow, which would turn white into near-black."""
    pixels = np.full((32, 128, 3), 250.0, dtype=np.float32)
    out = _scale(pixels, 1.0, (1.4, 1.0), seam=64)
    assert out[:, :64].max() == 255


def test_calibration_composes_with_global_brightness() -> None:
    pixels = np.full((32, 128, 3), 200.0, dtype=np.float32)
    out = _scale(pixels, 0.5, (0.5, 1.0), seam=64)
    assert out[:, :64].max() == 50  # 200 * 0.5 * 0.5
    assert out[:, 64:].max() == 100  # 200 * 0.5


def test_renderer_uses_the_same_scaling_expression() -> None:
    """Guard that the loop still clips before casting and slices axis 1.

    These two details are invisible in a screenshot but produce wrapped
    speckle and horizontal banding respectively, so pin the source.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "ticker" / "renderer.py"
    ).read_text(encoding="utf-8")
    assert "pixels[:, :seam] *= left_gain" in source
    assert "pixels[:, seam:] *= right_gain" in source
    assert "np.clip(pixels, 0, 255).astype(np.uint8)" in source


# -- web route -------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def test_route_saves_gains(client) -> None:  # type: ignore[no-untyped-def]
    payload = client.post("/settings/calibration", json={"left": 0.9, "right": 1.0}).get_json()
    assert payload["left"] == pytest.approx(0.9)
    assert payload["right"] == pytest.approx(1.0)


def test_route_clamps_rather_than_erroring(client) -> None:  # type: ignore[no-untyped-def]
    payload = client.post("/settings/calibration", json={"left": 99, "right": 1.0}).get_json()
    assert payload["left"] == pytest.approx(1.5)


def test_route_rejects_junk(client) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/settings/calibration", json={"left": "nope", "right": 1.0})
    assert resp.status_code == 400


def test_route_toggles_test_pattern_independently(client) -> None:  # type: ignore[no-untyped-def]
    """The beacon on page exit posts only {"test": false} and must not
    reset the gains the user just dialled in."""
    client.post("/settings/calibration", json={"left": 0.8, "right": 1.1})
    payload = client.post("/settings/calibration", json={"test": True}).get_json()
    assert payload["test"] is True
    assert payload["left"] == pytest.approx(0.8)
    assert payload["right"] == pytest.approx(1.1)

    payload = client.post("/settings/calibration", json={"test": False}).get_json()
    assert payload["test"] is False
    assert payload["left"] == pytest.approx(0.8)


def test_settings_page_renders_the_calibration_card(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get("/settings").get_data(as_text=True)
    assert 'id="calib-left"' in body
    assert 'id="calib-right"' in body
    assert 'id="calib-test"' in body
