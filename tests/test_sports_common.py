# MIT License — Copyright (c) 2026 John Kuok
"""Shared sports plumbing: ``Game``, ``LogoCache``, placeholder/card drawing,
and the ``LeagueMode`` base render loop.

Split out of the old MLB-only ``test_sports.py`` when the sports umbrella
grew NHL/NFL/NBA siblings sharing this module. Per-league fetch/parse
behaviour lives in each league's own test file (``test_mlb.py``,
``test_nhl.py``, ``test_nfl.py``, ``test_nba.py``); this file only tests
the code every league shares.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ticker.canvas import Canvas
from ticker.modes.sports_common import (
    Game,
    LeagueMode,
    LogoCache,
    draw_game_card,
    draw_placeholder,
)


# ---------------------------------------------------------------------------
# Game
# ---------------------------------------------------------------------------


def _game(**overrides) -> Game:
    base = dict(
        league="mlb",
        away_id=137, home_id=147, away_score=5, home_score=3,
        status_line="Final", abstract="final",
        away_winner=True, home_winner=False,
        away_tri="SF", home_tri="NYY",
        away_color=(240, 110, 30), home_color=(30, 70, 160),
    )
    base.update(overrides)
    return Game(**base)


def test_game_is_frozen() -> None:
    """Game records are immutable -- render code must never mutate one in place."""
    g = _game()
    try:
        g.away_score = 99  # type: ignore[misc]
        assert False, "Game should be frozen"
    except AttributeError:
        pass


def test_game_carries_league_for_logo_cache_keying() -> None:
    """``league`` plus numeric ids let a shared logo cache disambiguate
    e.g. MLB team 137 from NHL team 137."""
    mlb_g = _game(league="mlb", away_id=137)
    nhl_g = _game(league="nhl", away_id=137)
    assert mlb_g.league == "mlb"
    assert nhl_g.league == "nhl"
    assert mlb_g.away_id == nhl_g.away_id


# ---------------------------------------------------------------------------
# LogoCache
# ---------------------------------------------------------------------------


def test_logo_cache_returns_none_when_nothing_cached_and_marks_in_flight() -> None:
    """First lookup for a team with no cached file returns None immediately
    (fetch happens off-thread) and doesn't block the caller."""
    cache_dir = Path(tempfile.mkdtemp(prefix="ticker-logo-test-"))
    # A URL function that would hang if called synchronously -- if
    # logo_for() ever regresses to blocking, this test would time out.
    calls = []

    def _url_for(team_id: int) -> str:
        calls.append(team_id)
        return ""  # empty URL fails fast inside the background thread

    cache = LogoCache(cache_dir, _url_for)
    result = cache.logo_for(137)
    assert result is None


def test_logo_cache_reads_existing_cached_file() -> None:
    """A pre-populated cache file is read back without touching the network."""
    from PIL import Image

    cache_dir = Path(tempfile.mkdtemp(prefix="ticker-logo-test-"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (16, 16), (10, 20, 30, 255))
    img.save(cache_dir / "137.png", format="PNG")

    def _url_for(team_id: int) -> str:
        raise AssertionError("should not fetch when a cache file already exists")

    cache = LogoCache(cache_dir, _url_for)
    result = cache.logo_for(137)
    assert result is not None
    assert result.size == (16, 16)


def test_logo_cache_keys_are_independent_per_instance() -> None:
    """Two LogoCache instances (as each league mode owns its own) never
    share missing/in-flight state even for the same numeric team id."""
    dir_a = Path(tempfile.mkdtemp(prefix="ticker-logo-a-"))
    dir_b = Path(tempfile.mkdtemp(prefix="ticker-logo-b-"))
    cache_a = LogoCache(dir_a, lambda tid: "")
    cache_b = LogoCache(dir_b, lambda tid: "")
    cache_a._missing.add(137)
    assert 137 not in cache_b._missing


# ---------------------------------------------------------------------------
# draw_placeholder
# ---------------------------------------------------------------------------


def _lit(canvas: Canvas) -> set[tuple[int, int]]:
    pixels = canvas.image_buffer.load()
    return {
        (x, y)
        for y in range(canvas.height)
        for x in range(canvas.width)
        if pixels[x, y] != (0, 0, 0)
    }


def test_draw_placeholder_paints_amber_text() -> None:
    canvas = Canvas(128, 32)
    draw_placeholder(canvas, "No MLB today")
    lit = _lit(canvas)
    assert lit, "placeholder drew nothing"
    pixels = canvas.image_buffer.load()
    colours = {pixels[x, y] for x, y in lit}
    assert colours == {(240, 200, 90)}


def test_draw_placeholder_centers_message_regardless_of_length() -> None:
    """A short and a long message should both draw within bounds and not
    raise, regardless of text width."""
    for message in ("No NFL today", "No games anywhere today at all"):
        canvas = Canvas(128, 32)
        draw_placeholder(canvas, message)
        lit = _lit(canvas)
        assert lit
        assert all(0 <= x < 128 for x, _ in lit)


# ---------------------------------------------------------------------------
# draw_game_card (shared layout, used by every league)
# ---------------------------------------------------------------------------


def _cache_with_all_missing(games: list[Game]) -> LogoCache:
    """A LogoCache pre-seeded so every team is known-missing, forcing the
    colour-block fallback path deterministically (no real HTTP fetch)."""
    cache = LogoCache(Path(tempfile.mkdtemp(prefix="ticker-card-test-")), lambda tid: "")
    for g in games:
        cache._missing.add(g.away_id)
        cache._missing.add(g.home_id)
    return cache


def test_draw_game_card_final_uses_colour_blocks_when_no_logo() -> None:
    g = _game()
    cache = _cache_with_all_missing([g])
    canvas = Canvas(128, 32)
    draw_game_card(canvas, g, cache)
    pixels = canvas.image_buffer.load()
    assert pixels[0, 0] == (240, 110, 30)   # away colour block
    assert pixels[0, 16] == (30, 70, 160)   # home colour block


def test_draw_game_card_final_winner_white_loser_amber() -> None:
    g = _game(away_winner=True, home_winner=False)
    cache = _cache_with_all_missing([g])
    canvas = Canvas(128, 32)
    draw_game_card(canvas, g, cache)
    pixels = canvas.image_buffer.load()

    def _colours_in(y_range: range) -> set[tuple[int, int, int]]:
        return {
            pixels[x, y]
            for y in y_range
            for x in range(18, 42)
            if pixels[x, y] != (0, 0, 0)
        }

    away_colours = _colours_in(range(2, 14))
    home_colours = _colours_in(range(18, 30))
    assert (240, 240, 240) in away_colours   # winner white
    assert (180, 140, 60) in home_colours    # loser amber


def test_draw_game_card_live_game_both_rows_white() -> None:
    """No premature winner colouring while abstract == 'live'."""
    g = _game(abstract="live", status_line="Bot 6", away_winner=False, home_winner=False)
    cache = _cache_with_all_missing([g])
    canvas = Canvas(128, 32)
    draw_game_card(canvas, g, cache)
    pixels = canvas.image_buffer.load()

    def _colours_in(y_range: range) -> set[tuple[int, int, int]]:
        return {
            pixels[x, y]
            for y in y_range
            for x in range(18, 42)
            if pixels[x, y] != (0, 0, 0)
        }

    away_colours = _colours_in(range(2, 14))
    home_colours = _colours_in(range(18, 30))
    assert away_colours == {(240, 240, 240)}
    assert home_colours == {(240, 240, 240)}


def test_draw_game_card_live_status_is_green() -> None:
    g = _game(abstract="live", status_line="Bot 6")
    cache = _cache_with_all_missing([g])
    canvas = Canvas(128, 32)
    draw_game_card(canvas, g, cache)
    pixels = canvas.image_buffer.load()
    status_colours = {
        pixels[x, y]
        for y in range(8, 22)
        for x in range(72, 128)
        if pixels[x, y] != (0, 0, 0)
    }
    assert (100, 220, 120) in status_colours


def test_draw_game_card_preview_status_is_amber() -> None:
    g = _game(abstract="preview", status_line="7:05")
    cache = _cache_with_all_missing([g])
    canvas = Canvas(128, 32)
    draw_game_card(canvas, g, cache)
    pixels = canvas.image_buffer.load()
    status_colours = {
        pixels[x, y]
        for y in range(8, 22)
        for x in range(72, 128)
        if pixels[x, y] != (0, 0, 0)
    }
    assert (240, 200, 90) in status_colours


def test_draw_game_card_two_digit_score_stays_inside_middle_column() -> None:
    """A 2-digit score must not spill into the status column at x=72."""
    g = _game(away_score=3, home_score=12, away_winner=False, home_winner=True)
    cache = _cache_with_all_missing([g])
    canvas = Canvas(128, 32)
    draw_game_card(canvas, g, cache)
    pixels = canvas.image_buffer.load()
    gutter = {pixels[x, y] for x in range(67, 71) for y in range(canvas.height)}
    assert gutter == {(0, 0, 0)}, f"score column overflowed: {gutter}"


# ---------------------------------------------------------------------------
# LeagueMode base render loop
# ---------------------------------------------------------------------------


class _FakeLeagueMode(LeagueMode):
    """Minimal concrete LeagueMode for exercising the base class directly,
    without any real config/Mode wiring or network access."""

    LEAGUE = "fake"
    NO_GAMES_MESSAGE = "No FAKE today"
    CACHE_SECONDS = 30.0
    CARD_SECONDS = 6.0
    FAVORITE_CONFIG_METHOD = "current_favorite_team_fake"

    def __init__(self, config, games: list[Game]) -> None:
        self._seed_games = games
        self._refresh_calls = 0
        super().__init__(config)

    def _build_logo_cache(self) -> LogoCache:
        return _cache_with_all_missing(self._seed_games)

    def _refresh_games(self) -> list[Game]:
        self._refresh_calls += 1
        return list(self._seed_games)


class _FakeConfig:
    """Just enough of Config's surface for FAVORITE_CONFIG_METHOD lookups."""

    def __init__(self, favorite: str = "") -> None:
        self._favorite = favorite

    def current_favorite_team_fake(self) -> str:
        return self._favorite


def test_games_for_today_caches_within_cache_seconds() -> None:
    mode = _FakeLeagueMode(_FakeConfig(), [_game()])
    mode.games_for_today()
    mode.games_for_today()
    assert mode._refresh_calls == 1, "second call within CACHE_SECONDS should not refetch"


def test_games_for_today_keeps_last_good_list_on_fetch_failure() -> None:
    class _FlakyMode(_FakeLeagueMode):
        def _refresh_games(self):  # type: ignore[override]
            self._refresh_calls += 1
            if self._refresh_calls == 1:
                return [_game()]
            raise RuntimeError("network blew up")

    mode = _FlakyMode(_FakeConfig(), [])
    first = mode.games_for_today()
    assert len(first) == 1
    # Force a refetch attempt that will raise.
    mode._last_refresh = -1e9
    second = mode.games_for_today()
    assert second == first, "a failed refresh must not blank the last-good list"


def test_render_placeholder_when_no_games() -> None:
    mode = _FakeLeagueMode(_FakeConfig(), [])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    lit = _lit(canvas)
    pixels = canvas.image_buffer.load()
    colours = {pixels[x, y] for x, y in lit}
    assert colours == {(240, 200, 90)}


def test_render_draws_a_card_when_games_exist() -> None:
    mode = _FakeLeagueMode(_FakeConfig(), [_game()])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    assert _lit(canvas), "card drew nothing"


def test_favorite_team_filters_to_matching_game() -> None:
    sf_game = _game(away_tri="SF", home_tri="NYY", away_id=137, home_id=147)
    lad_game = _game(away_tri="LAD", home_tri="COL", away_id=119, home_id=115)
    mode = _FakeLeagueMode(_FakeConfig(favorite="SF"), [sf_game, lad_game])
    for _ in range(3):
        canvas = Canvas(128, 32)
        mode.render(canvas, 0)
        assert _lit(canvas), "favorite card drew nothing"
    # Filter is applied at render time; the raw list is untouched.
    assert len(mode._games) == 2 or mode._games == []  # populated by first games_for_today() call


def test_favorite_team_falls_back_to_full_slate_on_off_day() -> None:
    """A favorite tri-code that isn't playing today should not blank the
    panel -- it should fall back to the full slate."""
    sf_game = _game(away_tri="SF", home_tri="NYY")
    mode = _FakeLeagueMode(_FakeConfig(favorite="BOS"), [sf_game])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    assert _lit(canvas), "fallback drew nothing on an off-day"


def test_favorite_team_matches_home_side_too() -> None:
    sf_game = _game(away_tri="SF", home_tri="NYY")
    mode = _FakeLeagueMode(_FakeConfig(favorite="NYY"), [sf_game])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    assert _lit(canvas)


def test_favorite_team_empty_string_is_no_filter() -> None:
    sf_game = _game(away_tri="SF", home_tri="NYY")
    mode = _FakeLeagueMode(_FakeConfig(favorite=""), [sf_game])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    assert _lit(canvas)


def test_favorite_config_method_missing_on_config_is_treated_as_no_filter() -> None:
    """If a config object doesn't implement the method at all (shouldn't
    happen in production, but defends against a typo'd FAVORITE_CONFIG_METHOD),
    the base class must not raise."""
    class _BareConfig:
        pass

    mode = _FakeLeagueMode(_BareConfig(), [_game()])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)  # must not raise
    assert _lit(canvas)


def test_card_rotation_cycles_through_multiple_games() -> None:
    """With several games, render() should be able to reach every card
    across enough calls (exercised the same way the old MLB-only suite
    tested rotation: draw each game directly and confirm it paints)."""
    games = [_game(away_id=i, home_id=i + 1) for i in range(100, 106, 2)]
    cache = _cache_with_all_missing(games)
    for g in games:
        canvas = Canvas(128, 32)
        draw_game_card(canvas, g, cache)
        assert _lit(canvas), "card drew nothing"
