# MIT License — Copyright (c) 2026 John Kuok
"""MLB league mode: schedule parsing, status formatting, registration, and
the per-league favorite-team config plumbing.

Card rendering itself (layout, colours, placeholder) is shared with the
other three leagues and tested once in ``test_sports_common.py``; this
file only covers what's specific to MLB's own data shape.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

from ticker.config import Config
from ticker.modes import MODE_TYPES
from ticker.modes.mlb import MLBMode, _MLB_TEAMS, _format_status, _parse_games
from ticker.modes.sports_common import Game


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_mlb_is_registered() -> None:
    assert MODE_TYPES["mlb"] is MLBMode


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
    assert a.league == "mlb"
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
# MLBMode wiring
# ---------------------------------------------------------------------------


def _isolated_config() -> Config:
    return dataclasses.replace(
        Config(),
        state_dir=Path(tempfile.mkdtemp(prefix="ticker-mlb-test-")),
    )


def test_mlb_mode_no_games_message() -> None:
    mode = MLBMode(_isolated_config())
    assert mode.NO_GAMES_MESSAGE == "No MLB today"
    assert mode.LEAGUE == "mlb"


def test_mlb_mode_favorite_config_method_round_trips() -> None:
    """MLBMode reads its favorite through current_favorite_team_mlb, which
    is backed by its own state file distinct from the other 3 leagues."""
    config = _isolated_config()
    config.set_favorite_team_mlb("SF")
    assert config.current_favorite_team_mlb() == "SF"
    mode = MLBMode(config)
    assert mode._favorite_team() == "SF"


def test_mlb_mode_rejects_invalid_tri_code() -> None:
    config = _isolated_config()
    try:
        config.set_favorite_team_mlb("ZZZ")
        assert False, "invalid tri-code should raise"
    except ValueError:
        pass
    assert config.current_favorite_team_mlb() == ""


def test_mlb_favorite_state_is_independent_of_other_leagues() -> None:
    """Favoriting an MLB team must not touch NHL/NFL/NBA's own favorite files."""
    config = _isolated_config()
    config.set_favorite_team_mlb("SF")
    assert config.current_favorite_team_nhl() == ""
    assert config.current_favorite_team_nfl() == ""
    assert config.current_favorite_team_nba() == ""
