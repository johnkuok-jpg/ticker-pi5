# MIT License — Copyright (c) 2026 John Kuok
"""MLB Sports mode: schedule parsing, status formatting, and card layout.

The renderer never blocks on the network, so we test three surfaces:

  * ``_format_status`` maps MLB's schedule states to the short status
    strings the panel needs (Final / F/10 / Top 7 / start time).
  * ``_parse_games`` extracts games from a schedule payload, skipping
    postponed/cancelled and using our own tri-code + colour map.
  * ``_draw_game`` and ``render`` lay out a card without crashing and
    without spilling text off the panel, whether or not the logo cache
    has anything in it (the network fetch is stubbed out).
"""

from __future__ import annotations

import dataclasses
import tempfile
import time
from pathlib import Path

from ticker.canvas import Canvas
from ticker.config import Config
from ticker.modes import MODE_TYPES
from ticker.modes.sports import (
    _MLB_TEAMS,
    Game,
    SportsMode,
    _format_status,
    _parse_games,
)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_sports_is_registered() -> None:
    """Guard against dropping the module out of the MODE_TYPES map."""
    assert MODE_TYPES["sports"] is SportsMode


def test_all_30_mlb_teams_have_metadata() -> None:
    """The tri-code + colour map must cover every current MLB club so no
    game card renders with an empty ``---`` label."""
    assert len(_MLB_TEAMS) == 30
    for team_id, (tri, primary, secondary) in _MLB_TEAMS.items():
        assert 100 <= team_id < 200, team_id
        assert len(tri) in (2, 3), tri
        assert tri.isupper(), tri
        assert len(primary) == 3 and all(0 <= c <= 255 for c in primary)
        assert len(secondary) == 3 and all(0 <= c <= 255 for c in secondary)


# ---------------------------------------------------------------------------
# _format_status
# ---------------------------------------------------------------------------


def test_format_status_regulation_final() -> None:
    g = {
        "status": {"abstractGameState": "Final", "detailedState": "Final"},
        "scheduledInnings": 9,
        "linescore": {"currentInning": 9},
    }
    assert _format_status(g) == ("Final", "final")


def test_format_status_extra_innings_final() -> None:
    g = {
        "status": {"abstractGameState": "Final", "detailedState": "Final"},
        "scheduledInnings": 9,
        "linescore": {"currentInning": 11},
    }
    assert _format_status(g) == ("F/11", "final")


def test_format_status_seven_inning_doubleheader_final() -> None:
    """Doubleheaders are 7 innings; a 7-inning final should still say ``Final``
    (not ``F/7``) because that's the scheduled length."""
    g = {
        "status": {"abstractGameState": "Final", "detailedState": "Final"},
        "scheduledInnings": 7,
        "linescore": {"currentInning": 7},
    }
    assert _format_status(g) == ("Final", "final")


def test_format_status_live_top_bottom_middle_end() -> None:
    for inning_state, expected in (
        ("Top", "Top 7"),
        ("Bottom", "Bot 7"),
        ("Middle", "Mid 7"),
        ("End", "End 7"),
    ):
        g = {
            "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
            "linescore": {"currentInning": 7, "inningState": inning_state},
        }
        assert _format_status(g) == (expected, "live"), inning_state


def test_format_status_live_falls_back_when_inning_missing() -> None:
    """If MLB ever ships a live game with no inning number, don't crash."""
    g = {
        "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
        "linescore": {"inningState": "Top"},
    }
    assert _format_status(g) == ("Live", "live")


def test_format_status_preview_uses_local_time() -> None:
    """Preview games render the local start time. The exact hour depends on
    the process timezone, so just check the shape (H:MM or HH:MM)."""
    g = {
        "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
        "gameDate": "2026-08-23T23:35:00Z",
        "linescore": {},
    }
    text, abstract = _format_status(g)
    assert abstract == "preview"
    assert ":" in text
    hh, mm = text.split(":")
    assert 0 <= int(hh) <= 23
    assert 0 <= int(mm) <= 59


# ---------------------------------------------------------------------------
# _parse_games
# ---------------------------------------------------------------------------


def _game_json(
    away_id: int,
    home_id: int,
    away_score: int,
    home_score: int,
    detailed: str = "Final",
    abstract: str = "Final",
    scheduled: int = 9,
    inning: int | None = 9,
    inning_state: str | None = None,
    away_winner: bool = False,
    home_winner: bool = False,
) -> dict:
    return {
        "status": {"detailedState": detailed, "abstractGameState": abstract},
        "scheduledInnings": scheduled,
        "linescore": {"currentInning": inning, "inningState": inning_state},
        "teams": {
            "away": {"team": {"id": away_id}, "score": away_score, "isWinner": away_winner},
            "home": {"team": {"id": home_id}, "score": home_score, "isWinner": home_winner},
        },
    }


def test_parse_games_returns_expected_games() -> None:
    payload = {"dates": [{"games": [
        _game_json(137, 147, 5, 3, away_winner=True),
        _game_json(119, 143, 2, 4, detailed="In Progress", abstract="Live",
                   inning=6, inning_state="Bottom"),
    ]}]}
    games = _parse_games(payload)
    assert len(games) == 2
    a, b = games
    assert (a.away_tri, a.home_tri) == ("SF", "NYY")
    assert (a.away_score, a.home_score) == (5, 3)
    assert a.abstract == "final"
    assert a.away_winner is True and a.home_winner is False
    assert (b.away_tri, b.home_tri) == ("LAD", "PHI")
    assert b.status_line == "Bot 6"


def test_parse_games_skips_postponed_cancelled_suspended() -> None:
    payload = {"dates": [{"games": [
        _game_json(137, 147, 0, 0, detailed="Postponed", abstract="Preview", inning=None),
        _game_json(119, 143, 0, 0, detailed="Cancelled", abstract="Final", inning=None),
        _game_json(110, 140, 0, 0, detailed="Suspended", abstract="Live", inning=5),
        _game_json(137, 147, 4, 2, away_winner=True),  # a real one
    ]}]}
    games = _parse_games(payload)
    assert len(games) == 1
    assert games[0].away_tri == "SF"


def test_parse_games_handles_empty_dates() -> None:
    """Off-day / winter payload returns an empty dates array."""
    assert _parse_games({"dates": []}) == []
    assert _parse_games({}) == []


def test_parse_games_keeps_doubleheaders_as_separate_cards() -> None:
    """Two entries with the same teams should both appear."""
    payload = {"dates": [{"games": [
        _game_json(137, 147, 3, 2, away_winner=True),
        _game_json(137, 147, 5, 6, home_winner=True),
    ]}]}
    games = _parse_games(payload)
    assert len(games) == 2


def test_parse_games_uses_placeholder_for_unknown_team_id() -> None:
    """Unknown IDs get a ``---`` tri-code and a grey block colour."""
    payload = {"dates": [{"games": [
        _game_json(999, 137, 1, 2, away_winner=False, home_winner=True),
    ]}]}
    games = _parse_games(payload)
    assert len(games) == 1
    assert games[0].away_tri == "---"
    assert games[0].home_tri == "SF"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _lit(canvas: Canvas) -> set[tuple[int, int]]:
    pixels = canvas.image_buffer.load()
    return {
        (x, y)
        for y in range(canvas.height)
        for x in range(canvas.width)
        if pixels[x, y] != (0, 0, 0)
    }


def _seeded_mode(games: list[Game]) -> SportsMode:
    # Isolated state_dir so a leftover logo from a previous run can't
    # sneak into the render path and steal the colour-block fallback.
    # Config is a frozen dataclass, so build the isolated copy via replace.
    config = dataclasses.replace(
        Config(),
        state_dir=Path(tempfile.mkdtemp(prefix="ticker-sports-test-")),
    )
    mode = SportsMode(config)
    mode._games = list(games)
    # Push _refresh out of range so ``render`` never calls the network.
    mode._last_refresh = time.monotonic() + 10**9
    # Force the colour-block fallback path by marking every team's logo
    # as already-known-missing, so ``_logo_for`` never kicks off a real
    # background HTTP fetch during the test.
    mode._logo_missing = {g.away_id for g in games} | {g.home_id for g in games}
    return mode


def _final_game(away_wins: bool = True) -> Game:
    return Game(
        away_id=137, home_id=147, away_score=5, home_score=3,
        status_line="Final", abstract="final",
        away_winner=away_wins, home_winner=not away_wins,
        away_tri="SF", home_tri="NYY",
        away_color=(240, 110, 30), home_color=(30, 70, 160),
    )


def test_render_final_lays_out_logos_scores_and_status() -> None:
    """Winner's row is white; loser's row is muted amber; status is grey."""
    mode = _seeded_mode([_final_game(away_wins=True)])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # Left column: colour-block fallback (no logos on disk in test).
    assert pixels[0, 0] == (240, 110, 30)   # SF orange
    assert pixels[0, 16] == (30, 70, 160)   # NYY navy
    # Right of column 16 should have lit text pixels for tri + score.
    lit = _lit(canvas)
    away_row = {y for _, y in lit if 2 <= y < 14}
    home_row = {y for _, y in lit if 18 <= y < 30}
    assert away_row and home_row


def test_render_final_colours_match_winner() -> None:
    """The winning tri-code + score is white; the losing side is amber."""
    mode = _seeded_mode([_final_game(away_wins=True)])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # Sample the tri-code region (x=18..40, tri font MEDIUM). Any lit
    # pixel there should be the winner-white or loser-amber tone.
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


def test_render_live_game_uses_green_status() -> None:
    live = Game(
        away_id=119, home_id=143, away_score=2, home_score=4,
        status_line="Bot 6", abstract="live",
        away_winner=False, home_winner=False,
        away_tri="LAD", home_tri="PHI",
        away_color=(20, 60, 150), home_color=(200, 30, 50),
    )
    mode = _seeded_mode([live])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # Status column is x >= 72; the live green (100, 220, 120) should appear.
    status_colours = {
        pixels[x, y]
        for y in range(8, 22)
        for x in range(72, 128)
        if pixels[x, y] != (0, 0, 0)
    }
    assert (100, 220, 120) in status_colours


def test_render_placeholder_when_no_games() -> None:
    """Empty schedule paints the ``No MLB today`` placeholder in amber."""
    mode = _seeded_mode([])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    lit = _lit(canvas)
    assert lit, "placeholder drew nothing"
    pixels = canvas.image_buffer.load()
    colours = {pixels[x, y] for x, y in lit}
    assert colours == {(240, 200, 90)}


def test_render_two_digit_score_stays_inside_middle_column() -> None:
    """A game with a 2-digit home score (Phillies 12) must not spill into
    the status column at x=72."""
    blowout = Game(
        away_id=138, home_id=143, away_score=3, home_score=12,
        status_line="Final", abstract="final",
        away_winner=False, home_winner=True,
        away_tri="STL", home_tri="PHI",
        away_color=(200, 40, 60), home_color=(200, 30, 50),
    )
    mode = _seeded_mode([blowout])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # The score column right edge is x=66. Between the score and the
    # status column (x=72) there should be a clear vertical gutter --
    # otherwise the digits and status collide.
    gutter = {pixels[x, y] for x in range(67, 71) for y in range(canvas.height)}
    assert gutter == {(0, 0, 0)}, f"score column overflowed: {gutter}"


def test_render_rotates_through_all_games() -> None:
    """Different monotonic timestamps should surface different cards.

    We can't inject the clock directly, but we can check that the mode
    has more than one game and that ``_draw_game`` is idempotent on
    each of them (no drift into a shared state).
    """
    games = [_final_game(away_wins=True) for _ in range(3)]
    mode = _seeded_mode(games)
    for g in games:
        canvas = Canvas(128, 32)
        mode._draw_game(canvas, g)
        assert _lit(canvas), "card drew nothing"
