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
    _CAMPFIRE_PALETTE,
    _Aquarium,
    _Campfire,
    _FluidFlame,
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


def test_campfire_flame_is_temporally_coherent(config) -> None:  # type: ignore[no-untyped-def]
    """Consecutive frames must be a continuation, not a re-roll.

    This is the whole reason the Doom automaton was replaced. That
    automaton re-rolled every cell's decay and x-jitter each step, so
    successive frames were uncorrelated and the flame sizzled; the fixes
    for it were all display-side filters fighting the generator. A real
    solver advects the previous frame's temperature field along the
    velocity field, so coherence is intrinsic and no smoothing is needed.

    We measure it: the mean absolute frame-to-frame change in the heat
    field must be small compared with the field's own spatial spread. A
    stateless generator scores near 1.0 on that ratio; advected fluid
    scores a fraction of it.
    """
    np = pytest.importorskip("numpy")

    flame = _FluidFlame()
    for _ in range(90):  # let the plume establish
        flame.step()

    prev = flame.heat().copy()
    ratios = []
    for _ in range(40):
        flame.step()
        cur = flame.heat()
        spread = float(np.mean(np.abs(cur - cur.mean())))
        change = float(np.mean(np.abs(cur - prev)))
        ratios.append(change / max(spread, 1e-6))
        prev = cur.copy()

    worst = max(ratios)
    assert worst < 0.35, f"flame re-rolls rather than flows: worst ratio {worst:.2f}"
    # ...and it must not be frozen either, or a static image would pass.
    assert max(ratios) > 0.01, "flame is not moving at all"


def test_campfire_turbulence_stays_out_of_the_still_air(config) -> None:  # type: ignore[no-untyped-def]
    """The air outside the plume must be still.

    Curl-noise forcing applied to the whole grid shimmers the entire
    panel, including the black region either side of the fire and above
    the tips -- it reads as static, not as fire. Turbulence is therefore
    masked by temperature AND a height ramp. Assert the cold cells carry
    far less speed than the hot ones.
    """
    np = pytest.importorskip("numpy")

    flame = _FluidFlame()
    for _ in range(120):
        flame.step()

    speed = np.hypot(flame._u, flame._v)
    temp = flame._t
    cold = temp < 0.02
    hot = temp > 0.30
    assert cold.any() and hot.any(), "no cold/hot split to compare"

    cold_speed = float(speed[cold].mean())
    hot_speed = float(speed[hot].mean())
    assert cold_speed < hot_speed * 0.35, (
        f"still air is being stirred: cold {cold_speed:.3f} vs hot {hot_speed:.3f}"
    )


def test_campfire_hot_spots_wander_along_the_log_line(config) -> None:  # type: ignore[no-untyped-def]
    """The bright tongue must migrate, not stand in a fixed column.

    With static per-spot weights the fuel bed produced two bright pillars
    parked in the same x columns in every frame -- correct as fluid,
    wrong as fire, because a log burns through and the flame front moves.
    Each spot's weight now swings on its own non-harmonic period, so the
    fuel centroid drifts. Assert the drift is real and bounded (it must
    stay over the log pile, not walk off the panel).
    """
    np = pytest.importorskip("numpy")

    flame = _FluidFlame()
    xs = np.arange(flame._w, dtype=np.float32)
    centroids = []
    for i in range(600):
        flame.step()
        if i % 10 == 0:
            profile = flame._fuel
            centroids.append(float((profile * xs).sum() / max(profile.sum(), 1e-6)))

    span = (max(centroids) - min(centroids)) / flame._SS  # in panel px
    assert span > 2.0, f"fuel centroid barely moves: {span:.2f} px"
    assert span < 20.0, f"fuel centroid wanders off the log pile: {span:.2f} px"


def test_campfire_falls_back_to_the_automaton_without_numpy(config, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No numpy must degrade, not crash.

    The solver is vectorised and hard-depends on numpy. numpy is pinned
    in requirements, but the Doom automaton is kept intact as the
    fallback so a bare checkout (or a wheel that failed to build on the
    Pi) still shows a fire instead of an empty panel.
    """
    import ticker.modes.vibes as vibes

    monkeypatch.setattr(vibes, "_np", None)
    fire = _Campfire()
    assert fire._fluid is None, "fallback path did not engage"

    canvas = Canvas(config.width, config.height)
    for tick in range(120):
        fire.render(canvas, tick=tick)

    pixels = _pixels(canvas)
    warm = [p for p in pixels if p != (0, 0, 0) and p[0] > p[2]]
    assert len(warm) > 200, f"automaton fallback drew almost nothing: {len(warm)}"


def test_campfire_uses_the_solver_when_numpy_is_available(config) -> None:  # type: ignore[no-untyped-def]
    """The default path is the fluid solver, stepped once per frame.

    The automaton ran at a divided step rate with a cross-fade in
    between; the solver is coherent on its own, so it advances every
    frame and there is no filter downstream of it. Pin both halves of
    that: the solver is the active renderer, and one render is one step.
    """
    pytest.importorskip("numpy")

    fire = _Campfire()
    assert fire._fluid is not None, "solver did not initialise"

    canvas = Canvas(config.width, config.height)
    before = fire._fluid._tick
    for tick in range(10):
        fire.render(canvas, tick=tick)
    assert fire._fluid._tick == before + 10, (
        f"solver stepped {fire._fluid._tick - before} times in 10 frames"
    )


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


def test_driving_traffic_shows_lamps_and_stays_on_the_asphalt() -> None:
    """Traffic must appear, and never off the edge of the road.

    Two separate regressions live here. Lamps: the tail and head lights
    are the only thing that identifies a vehicle at this resolution, so
    if the spawner stalls or the lamps project off-panel the road looks
    permanently empty -- which is what happened when the first spawn
    cooldown was drawn from the full steady-state range. Placement: the
    vehicle centre plus half its width has to stay inside the projected
    asphalt half-width at its own depth, or a car drifts out over the
    sand while still facing down the lane.
    """
    from ticker.modes.vibes import (
        _DR_F,
        _DR_HEADLIGHT,
        _DR_ROAD_HALF,
        _DR_TAIL,
        _DR_Z_FAR,
        _Driving,
    )

    scene = _Driving()
    canvas = Canvas(128, 32)
    saw_head = saw_tail = False
    for tick in range(900):
        canvas.clear()
        scene.render(canvas, tick)
        pixels = _pixels(canvas)
        saw_head = saw_head or _DR_HEADLIGHT in pixels
        saw_tail = saw_tail or _DR_TAIL in pixels
        for v in scene._traffic:
            z = v["z"]
            if z < scene._EXIT_Z or z > _DR_Z_FAR:
                continue
            offset = abs(scene._project(v["lane"] + v["drift"], z)
                         - scene._centre_x(z))
            half_body = _DR_F * v["w"] * 0.5 / z
            road_half = _DR_F * _DR_ROAD_HALF / z
            assert offset + half_body <= road_half + 0.6, (
                f"vehicle off the asphalt at z={z:.2f}"
            )

    assert saw_tail, "expected a tail light from same-direction traffic"
    assert saw_head, "expected a headlight from oncoming traffic"


def test_driving_wire_colour_picks_contrast_against_the_background() -> None:
    """Wires switch colour based on what is behind them.

    The wires were never missing -- they were being drawn in a colour
    within a couple of units of the near mesa they crossed, so half the
    run was a dark line on an equally dark hill. The fix is per-pixel:
    dark wire over bright sky, rim-lit wire over a dark ridge. Pin both
    branches so a future palette change cannot silently collapse them
    back into one colour.
    """
    from PIL import Image

    from ticker.modes.vibes import _DR_WIRE, _DR_WIRE_LIT, _Driving

    img = Image.new("RGB", (2, 1))
    img.putpixel((0, 0), (240, 200, 150))   # bright sky
    img.putpixel((1, 0), (20, 14, 24))      # dark ridge
    assert _Driving._wire_colour(img, 0, 0) == _DR_WIRE
    assert _Driving._wire_colour(img, 1, 0) == _DR_WIRE_LIT



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
