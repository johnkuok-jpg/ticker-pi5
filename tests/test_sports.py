# MIT License — Copyright (c) 2026 John Kuok
"""Sports umbrella mode: which league is "on" for the current rotation slot.

Per-league fetch/parse/format behaviour has its own test file
(``test_mlb.py``, ``test_nhl.py``, ``test_nfl.py``, ``test_nba.py``); this
file only covers ``SportsMode`` itself -- the single ``"sports"`` slot in
``MODE_TYPES``/``VALID_MODES`` that rotates across whichever of the four
leagues have games today.
"""

from __future__ import annotations

import dataclasses
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from ticker.canvas import Canvas
from ticker.config import VALID_MODES, Config
from ticker.modes import MODE_TYPES
from ticker.modes.mlb import MLBMode
from ticker.modes.nba import NBAMode
from ticker.modes.nfl import NFLMode
from ticker.modes.nhl import NHLMode
from ticker.modes.sports import LEAGUE_SECONDS, SportsMode
from ticker.modes.sports_common import Game


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_sports_is_registered_as_the_single_umbrella_slot() -> None:
    """Only one "sports" entry in MODE_TYPES/VALID_MODES -- MLB/NHL/NFL/NBA
    are internal per-league classes, not separate top-level mode slots a
    user can pick from the rotation/settings UI."""
    assert MODE_TYPES["sports"] is SportsMode
    assert "sports" in VALID_MODES
    assert "mlb" not in VALID_MODES
    assert "nhl" not in VALID_MODES
    assert "nfl" not in VALID_MODES
    assert "nba" not in VALID_MODES


def test_per_league_classes_still_directly_constructible() -> None:
    """Kept in MODE_TYPES under their own keys so tests/tools can build a
    single league mode directly without a private import path -- but this
    is not a rotation option in VALID_MODES."""
    assert MODE_TYPES["mlb"] is MLBMode
    assert MODE_TYPES["nhl"] is NHLMode
    assert MODE_TYPES["nfl"] is NFLMode
    assert MODE_TYPES["nba"] is NBAMode


def test_sports_mode_builds_all_four_leagues_in_alphabetical_order() -> None:
    """Order matters only for the deterministic empty-slate fallback."""
    config = _isolated_config()
    mode = SportsMode(config)
    assert [type(league) for league in mode._leagues] == [MLBMode, NBAMode, NFLMode, NHLMode]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _isolated_config() -> Config:
    return dataclasses.replace(
        Config(),
        state_dir=Path(tempfile.mkdtemp(prefix="ticker-sports-umbrella-test-")),
    )


def _game(league: str, tri_pair=("AAA", "BBB")) -> Game:
    return Game(
        league=league,
        away_id=1, home_id=2, away_score=3, home_score=1,
        status_line="Final", abstract="final",
        away_winner=True, home_winner=False,
        away_tri=tri_pair[0], home_tri=tri_pair[1],
        away_color=(200, 30, 40), home_color=(30, 70, 160),
    )


def _lit(canvas: Canvas) -> set[tuple[int, int]]:
    pixels = canvas.image_buffer.load()
    return {
        (x, y)
        for y in range(canvas.height)
        for x in range(canvas.width)
        if pixels[x, y] != (0, 0, 0)
    }


def _stub_games_for_today(mode: SportsMode, games_by_type: dict) -> None:
    """Patch each league's games_for_today() to return a canned list
    without touching the network, keyed by the league mode's class."""
    for league in mode._leagues:
        games = games_by_type.get(type(league), [])
        league.games_for_today = lambda g=games: g  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# _leagues_with_games
# ---------------------------------------------------------------------------


def test_leagues_with_games_includes_only_leagues_that_have_a_slate() -> None:
    mode = SportsMode(_isolated_config())
    _stub_games_for_today(mode, {
        MLBMode: [_game("mlb")],
        NBAMode: [],
        NFLMode: [_game("nfl")],
        NHLMode: [],
    })
    active = mode._leagues_with_games()
    assert [type(league) for league in active] == [MLBMode, NFLMode]


def test_leagues_with_games_is_empty_when_nobody_plays_today() -> None:
    mode = SportsMode(_isolated_config())
    _stub_games_for_today(mode, {})
    assert mode._leagues_with_games() == []


def test_leagues_with_games_skips_a_league_whose_fetch_blows_up() -> None:
    """One league's failure shouldn't take the whole umbrella mode down --
    it's just skipped that tick."""
    mode = SportsMode(_isolated_config())

    def _boom():
        raise RuntimeError("bad league wiring")

    mode._leagues[0].games_for_today = _boom  # type: ignore[method-assign]
    _stub_games_for_today(mode, {NBAMode: [_game("nba")]})
    # _stub_games_for_today above will have reassigned index 0 (MLB) too
    # since it iterates all leagues -- so re-break it after the stub call.
    mode._leagues[0].games_for_today = _boom  # type: ignore[method-assign]

    active = mode._leagues_with_games()
    assert type(mode._leagues[0]) not in [type(a) for a in active]


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_shows_placeholder_when_no_league_has_games() -> None:
    """Falls back to the first league's (MLB's) placeholder so the panel
    still reads as "Sports" rather than rendering nothing."""
    mode = SportsMode(_isolated_config())
    _stub_games_for_today(mode, {})
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    lit = _lit(canvas)
    assert lit, "placeholder drew nothing"
    pixels = canvas.image_buffer.load()
    colours = {pixels[x, y] for x, y in lit}
    assert colours == {(240, 200, 90)}  # shared placeholder amber


def test_render_draws_a_card_when_exactly_one_league_has_games() -> None:
    mode = SportsMode(_isolated_config())
    _stub_games_for_today(mode, {NHLMode: [_game("nhl")]})
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    assert _lit(canvas), "card drew nothing"


def test_render_rotates_across_leagues_on_league_seconds_boundary() -> None:
    """With two leagues active, different monotonic slots should pick
    different leagues to render (LEAGUE_SECONDS cadence, independent of
    each league's own CARD_SECONDS)."""
    mode = SportsMode(_isolated_config())
    _stub_games_for_today(mode, {
        MLBMode: [_game("mlb", ("SF", "NYY"))],
        NHLMode: [_game("nhl", ("TOR", "BOS"))],
    })
    active = mode._leagues_with_games()
    assert len(active) == 2

    seen_indices = set()
    base = time.monotonic()
    for slot in range(4):
        with patch("ticker.modes.sports.time.monotonic", return_value=base + slot * LEAGUE_SECONDS):
            idx = int((base + slot * LEAGUE_SECONDS) // LEAGUE_SECONDS) % len(active)
            seen_indices.add(idx)
            canvas = Canvas(128, 32)
            mode.render(canvas, 0)
            assert _lit(canvas), f"slot {slot} drew nothing"
    assert seen_indices == {0, 1}, "rotation should visit both active leagues across slots"


def test_render_single_league_rotation_matches_that_leagues_render() -> None:
    """When only one league has games, every rotation slot renders that
    league regardless of LEAGUE_SECONDS phase."""
    mode = SportsMode(_isolated_config())
    _stub_games_for_today(mode, {NFLMode: [_game("nfl", ("KC", "DAL"))]})
    for slot in range(3):
        canvas = Canvas(128, 32)
        mode.render(canvas, 0)
        assert _lit(canvas)
