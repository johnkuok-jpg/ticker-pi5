# MIT License — Copyright (c) 2026 John Kuok
"""Tests for the ambient ``vibes`` mode.

The vibes mode is a full-screen screensaver that dispatches to one of
several sub-vibes (Campfire, Rain, Aquarium). These tests cover the
dispatch contract, the config persistence, and enough per-vibe pixel
sampling to catch a regression that turns the campfire cold, freezes
the rain drops in place, or empties the aquarium of fish.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticker.canvas import Canvas
from ticker.config import VALID_MODES, load_config
from ticker.modes import MODE_TYPES, VibesMode
from ticker.modes.vibes import (
    DEFAULT_VIBE,
    _Aquarium,
    _Campfire,
    _Rain,
    valid_vibes,
    vibe_labels,
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "ticker.config._first_writable_state_dir",
        lambda: tmp_path / "state",
    )
    env = tmp_path / ".env"
    env.write_text(
        "TICKER_WIDTH=128\nTICKER_HEIGHT=32\nTICKER_SYMBOLS=AAPL,NVDA\n"
        "WEATHER_LAT=37.7749\nWEATHER_LON=-122.4194\n",
        encoding="utf-8",
    )
    return load_config(env)


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


def test_vibes_is_a_registered_top_level_mode() -> None:
    """The webapp routes on VALID_MODES; a missing entry breaks the picker."""
    assert "vibes" in VALID_MODES
    assert "vibes" in MODE_TYPES
    assert MODE_TYPES["vibes"] is VibesMode


def test_vibe_registry_shape_is_frozen() -> None:
    """A silent rename of a vibe key would leave old vibe.txt files stranded.

    Keeping the tuple frozen here forces the change to be explicit: if we
    ever rename or reorder a vibe, this test fails and the diff has to
    include a migration story for the persisted vibe.txt values.
    """
    assert valid_vibes() == ("campfire", "rain", "aquarium", "driving")
    assert vibe_labels() == {
        "campfire": "Campfire",
        "rain":     "Rain",
        "aquarium": "Aquarium",
        "driving":  "Driving",
    }
    assert DEFAULT_VIBE == "campfire"


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def test_current_vibe_defaults_to_campfire_before_any_write(config) -> None:  # type: ignore[no-untyped-def]
    """Cold-boot with no state file must not crash and must pick the default."""
    assert not config.vibe_file.exists()
    assert config.current_vibe() == DEFAULT_VIBE


def test_set_vibe_round_trips_and_persists(config) -> None:  # type: ignore[no-untyped-def]
    """After ``set_vibe``, ``current_vibe`` reads the same value back."""
    config.set_vibe("rain")
    assert config.current_vibe() == "rain"
    assert config.vibe_file.read_text(encoding="utf-8").strip() == "rain"


def test_set_vibe_rejects_unknown_keys(config) -> None:  # type: ignore[no-untyped-def]
    """A typo in the webapp must not silently persist a bad value."""
    with pytest.raises(ValueError):
        config.set_vibe("bogus")


def test_current_vibe_ignores_corrupted_state_file(config) -> None:  # type: ignore[no-untyped-def]
    """A hand-edited vibe.txt with garbage must fall back to the default.

    The webapp POST path guards against bad values, but the file lives
    on disk and can be tampered with. current_vibe must be defensive so
    a corrupt read doesn't lock the panel into an unhandled key.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.vibe_file.write_text("not-a-vibe", encoding="utf-8")
    assert config.current_vibe() == DEFAULT_VIBE


# ---------------------------------------------------------------------------
# VibesMode dispatch
# ---------------------------------------------------------------------------


def test_vibes_mode_dispatches_to_configured_vibe(config) -> None:  # type: ignore[no-untyped-def]
    """The active vibe is read every render, so a mid-run switch takes effect."""
    mode = VibesMode(config)
    canvas = Canvas(config.width, config.height)

    config.set_vibe("campfire")
    for t in range(5):
        mode.render(canvas, tick=t)
    assert isinstance(mode._cache["campfire"], _Campfire)

    config.set_vibe("rain")
    mode.render(canvas, tick=100)
    assert isinstance(mode._cache["rain"], _Rain)

    config.set_vibe("aquarium")
    mode.render(canvas, tick=200)
    assert isinstance(mode._cache["aquarium"], _Aquarium)


def test_vibes_mode_falls_back_when_state_file_is_garbage(config, caplog) -> None:  # type: ignore[no-untyped-def]
    """Unknown key in vibe.txt: render the default and don't raise."""
    # Bypass ``set_vibe``'s validation to plant a bad value.
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.vibe_file.write_text("nonsense", encoding="utf-8")

    mode = VibesMode(config)
    canvas = Canvas(config.width, config.height)
    # current_vibe reads the file, sees an unknown key, and returns
    # DEFAULT_VIBE, so VibesMode._get_vibe should be called with
    # "campfire" and never hit the warning path.
    mode.render(canvas, tick=0)
    assert "campfire" in mode._cache


# ---------------------------------------------------------------------------
# Per-vibe rendering
# ---------------------------------------------------------------------------


def _pixels(canvas: Canvas) -> list[tuple[int, int, int]]:
    return list(canvas.image_buffer.getdata())


def test_campfire_produces_warm_pixels_and_dark_gaps(config) -> None:  # type: ignore[no-untyped-def]
    """Real fire has warm colour AND dark negative space.

    A regression that lost the fuel dropouts turned the panel into a
    solid yellow-orange wall. We assert both: many warm pixels (R > B)
    and many fully-black pixels so the flames read as distinct tongues.
    """
    config.set_vibe("campfire")
    mode = VibesMode(config)
    canvas = Canvas(config.width, config.height)
    # Warm up the plasma so the fuel row has propagated up the buffer.
    for t in range(120):
        mode.render(canvas, tick=t)

    pixels = _pixels(canvas)
    lit = [p for p in pixels if p != (0, 0, 0)]
    warm = [p for p in lit if p[0] > p[2]]  # red channel dominates blue
    black = [p for p in pixels if p == (0, 0, 0)]

    # Majority of lit pixels are warm.
    assert len(warm) > len(lit) * 0.9, (
        f"expected mostly warm pixels, got {len(warm)}/{len(lit)}"
    )
    # And there's plenty of black -- gaps between flame tongues plus
    # the top of the panel that heat never reached.
    assert len(black) > 400, f"expected >400 black pixels, got {len(black)}"


def test_campfire_draws_logs_in_the_bottom_rows(config) -> None:  # type: ignore[no-untyped-def]
    """Log body pixels use the fixed dark-brown palette entry.

    If the log renderer were skipped or drawn off-panel, no pixel would
    match the exact _LOG_DARK triple in the bottom rows.
    """
    from ticker.modes.vibes import _LOG_DARK, _LOG_TOP

    config.set_vibe("campfire")
    mode = VibesMode(config)
    canvas = Canvas(config.width, config.height)
    mode.render(canvas, tick=0)

    img = canvas.image_buffer
    dark_hits = 0
    for y in range(_LOG_TOP, 32):
        for x in range(128):
            if img.getpixel((x, y)) == _LOG_DARK:
                dark_hits += 1
    # Two angled logs at thickness 5, minus the notches and the pixels the
    # flame overdraws. The bar is deliberately well below the actual count so
    # that retuning the log geometry doesn't require retuning the test -- this
    # is asserting "logs got drawn on-panel in the right colour", not a
    # specific silhouette.
    assert dark_hits > 60, f"expected >60 log-body pixels, got {dark_hits}"


def test_campfire_holds_its_buffer_between_steps(config) -> None:  # type: ignore[no-untyped-def]
    """The plasma is a simulation, and the cross-fade depends on that.

    A rewrite replaced the buffer with a stateless noise field. It moved
    beautifully and looked like a blob: the tongues that make this read
    as fire come from the automaton's per-cell decay and x-jitter, and no
    smooth envelope reproduces them. This pins the mechanism so the trade
    is made deliberately -- the buffer must persist across frames, only
    advancing on a step boundary, with the in-between frames blending
    from the snapshot in ``_prev_buffer``.
    """
    fire = _Campfire()
    canvas = Canvas(config.width, config.height)

    fire.render(canvas, tick=0)
    after_step = [row[:] for row in fire._buffer]

    # A frame between step boundaries only moves the cross-fade; the
    # simulation state must be untouched.
    fire.render(canvas, tick=1)
    assert fire._buffer == after_step

    fire.render(canvas, tick=_Campfire._STEP_EVERY)
    assert fire._buffer != after_step, "buffer never advanced at a step boundary"
    # The snapshot is what the in-between frames blend from, so it has to
    # hold the state we just left behind.
    assert fire._prev_buffer == after_step


def test_campfire_smoothing_is_asymmetric_and_cuts_the_worst_frame_jump(config) -> None:  # type: ignore[no-untyped-def]
    """The display filter must smooth the sizzle without flattening a tongue.

    Two failure modes bracket this. Raw stepping strobes: the buffer holds
    for three frames then jumps a whole palette step, and that peak jump
    is what reads as jarring. A symmetric low-pass fixes the strobe by
    rounding off every leading edge, which is how the fire turns into a
    blob. So the filter has to be asymmetric -- fast up, slow down -- and
    it has to measurably reduce the WORST single-frame jump, not just the
    average.
    """
    assert _Campfire._ATTACK > _Campfire._RELEASE, "filter must rise faster than it falls"

    def peak_frame_jump(attack: float, release: float) -> float:
        original = (_Campfire._ATTACK, _Campfire._RELEASE)
        _Campfire._ATTACK, _Campfire._RELEASE = attack, release
        try:
            fire = _Campfire()
            warm = Canvas(config.width, config.height)
            for tick in range(60):
                fire.render(warm, tick=tick)
            frames = []
            for tick in range(60, 120):
                canvas = Canvas(config.width, config.height)
                fire.render(canvas, tick=tick)
                frames.append(list(canvas.image_buffer.convert("RGB").getdata()))
        finally:
            _Campfire._ATTACK, _Campfire._RELEASE = original
        jumps = []
        for a, b in zip(frames, frames[1:]):
            total = sum(
                abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2])
                for p, q in zip(a, b)
            )
            jumps.append(total / (len(a) * 3))
        return max(jumps)

    unfiltered = peak_frame_jump(1.0, 1.0)
    filtered = peak_frame_jump(_Campfire._ATTACK, _Campfire._RELEASE)
    assert filtered < unfiltered * 0.8, (
        f"filter barely helps: peak jump {filtered:.2f} vs unfiltered {unfiltered:.2f}"
    )


def test_campfire_display_filter_never_feeds_back_into_the_plasma(config) -> None:  # type: ignore[no-untyped-def]
    """Smoothed output must stay downstream of the simulation.

    An earlier fire died because the thing being displayed was also the
    thing being stepped, so every multiplication compounded until the
    flame either smeared or went out. ``_shown`` follows ``_buffer``; it
    must never be the source the automaton reads.
    """
    fire = _Campfire()
    canvas = Canvas(config.width, config.height)
    for tick in range(40):
        fire.render(canvas, tick=tick)

    poisoned = [row[:] for row in fire._buffer]
    for row in fire._shown:
        for x in range(len(row)):
            row[x] = 0.0
    fire.render(canvas, tick=41)  # not a step boundary
    assert fire._buffer == poisoned, "clearing the display field disturbed the plasma"


def test_rain_draws_drops_and_gradient(config) -> None:  # type: ignore[no-untyped-def]
    """Rain paints a night gradient sky plus visible drop heads.

    Full scene coverage: every pixel is at least the gradient bg, and
    across a short run we expect drop heads (matching ``_RAIN_HEAD``)
    to land in a variety of x positions -- i.e. the drops are actually
    moving down the pane, not stuck at one x.
    """
    from ticker.modes.vibes import _RAIN_HEAD, _RAIN_BG_TOP, _RAIN_BG_BOTTOM

    config.set_vibe("rain")
    mode = VibesMode(config)
    canvas = Canvas(config.width, config.height)

    head_positions: set[tuple[int, int]] = set()
    seen_bg_top = False
    seen_bg_bottom = False
    for tick in range(20):
        canvas = Canvas(config.width, config.height)
        mode.render(canvas, tick=tick)
        pixels = _pixels(canvas)
        # Full-panel gradient means no pixel is pure black.
        assert all(p != (0, 0, 0) for p in pixels), "rain vibe should paint every pixel"
        for idx, p in enumerate(pixels):
            if p == _RAIN_HEAD:
                head_positions.add((idx % 128, idx // 128))
            if p == _RAIN_BG_TOP:
                seen_bg_top = True
            if p == _RAIN_BG_BOTTOM:
                seen_bg_bottom = True

    # Drops moved: several distinct head positions over the run.
    assert len(head_positions) >= 5, (
        f"expected multiple drop-head positions over 20 ticks, got {len(head_positions)}"
    )
    # Gradient endpoints render somewhere on the panel.
    assert seen_bg_top, "expected the top gradient colour on the panel"
    assert seen_bg_bottom, "expected the bottom gradient colour on the panel"


def test_aquarium_paints_scene_with_fish_and_sand(config) -> None:  # type: ignore[no-untyped-def]
    """Aquarium renders a full tank: gradient sky, sand strip, fish body colors.

    Assertions target scene identity rather than exact pixel positions so
    the test tolerates small physics tweaks: no black pixels (bg covers
    everything), sand row present, and at least one fish body colour
    from the sprite palette lands on the panel.
    """
    from ticker.modes.vibes import _AQ_SAND, _FISH_SPRITES

    config.set_vibe("aquarium")
    mode = VibesMode(config)
    canvas = Canvas(config.width, config.height)
    mode.render(canvas, tick=0)

    pixels = _pixels(canvas)
    # Background gradient covers the whole panel.
    assert all(p != (0, 0, 0) for p in pixels), "aquarium should paint every pixel"
    # Sand strip lit along the bottom rows.
    assert _AQ_SAND in pixels, "expected the sand colour somewhere on the panel"
    # At least one fish body colour visible.
    body_colors = {sprite[1] for sprite in _FISH_SPRITES}
    assert body_colors & set(pixels), "expected at least one fish body colour visible"


def test_driving_paints_dusk_scene_with_road_and_lane_lines(config) -> None:  # type: ignore[no-untyped-def]
    """Driving renders a full first-person road scene at dusk.

    Assertions target scene identity, not exact pixel positions:
    every pixel is painted (sky + hills + road cover the panel), the
    road colour appears, and at least one lane-line dash colour lands
    on the panel.
    """
    from ticker.modes.vibes import _DR_ROAD, _DR_LANE_LINE, _DR_LANE_EDGE

    config.set_vibe("driving")
    mode = VibesMode(config)
    canvas = Canvas(config.width, config.height)
    mode.render(canvas, tick=0)

    pixels = _pixels(canvas)
    assert all(p != (0, 0, 0) for p in pixels), "driving should paint every pixel"
    assert _DR_ROAD in pixels, "expected the road colour somewhere on the panel"
    assert (_DR_LANE_LINE in pixels) or (_DR_LANE_EDGE in pixels), (
        "expected at least one lane-line dash colour visible"
    )


# ---------------------------------------------------------------------------
# Webapp wiring
# ---------------------------------------------------------------------------


def test_index_template_declares_vibe_context() -> None:
    """Static template guard: the vibes card must reference both variables.

    We keep this static rather than firing a request because the full
    index page pulls in Spotify auth, quake state, and a dozen other
    live subsystems; a full-render test would either be flaky or
    demand extensive mocking that adds no signal.
    """
    from pathlib import Path

    index = (
        Path(__file__).resolve().parents[1]
        / "src" / "ticker" / "web" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    # The picker loops vibe_labels and highlights the current vibe.
    assert "vibe_labels.items()" in index
    assert "vibe == key" in index


def test_index_route_lists_load_config_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODE_LABELS in the webapp must include the vibes label.

    A blank label would render a bare mode key in the picker grid.
    """
    import ticker.web.app as web

    assert web.MODE_LABELS.get("vibes") == "Vibes"
    assert "campfire" in web._VIBE_LABELS_FOR_TEMPLATE
    assert web._VIBE_LABELS_FOR_TEMPLATE["campfire"] == "Campfire"


def test_post_vibes_vibe_switches_mode_and_persists(config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """POST /vibes/vibe sets the vibe AND flips the top-level mode.

    That's the "pick one and stick" contract: clicking a vibe should
    immediately switch the panel into the vibes mode too, so the user
    doesn't have to first switch mode and then choose a vibe.
    """
    import ticker.web.app as web

    monkeypatch.setattr(web, "load_config", lambda: config)
    client = web.app.test_client()
    resp = client.post("/vibes/vibe", json={"vibe": "rain"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["vibe"] == "rain"
    assert data["current_mode"] == "vibes"
    assert config.current_vibe() == "rain"
    assert config.current_mode() == "vibes"


def test_post_vibes_vibe_rejects_bogus_key(config, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Bad key -> 400, and neither the mode nor the vibe change."""
    import ticker.web.app as web

    monkeypatch.setattr(web, "load_config", lambda: config)
    original_mode = config.current_mode()
    original_vibe = config.current_vibe()

    client = web.app.test_client()
    resp = client.post("/vibes/vibe", json={"vibe": "not-real"})
    assert resp.status_code == 400
    assert config.current_mode() == original_mode
    assert config.current_vibe() == original_vibe
