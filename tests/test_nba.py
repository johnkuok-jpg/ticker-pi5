# MIT License — Copyright (c) 2026 John Kuok
"""NBA league mode: schedule parsing, status formatting, registration, and
the per-league favorite-team config plumbing.

Card rendering itself is shared and tested in ``test_sports_common.py``.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

from ticker.config import Config
from ticker.modes import MODE_TYPES
from ticker.modes.nba import (
    NBAMode,
    _NBA_TEAMS,
    _format_status,
    _iso8601_duration_to_clock,
    _parse_games,
)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_nba_is_registered() -> None:
    assert MODE_TYPES["nba"] is NBAMode


def test_all_30_nba_teams_have_metadata() -> None:
    assert len(_NBA_TEAMS) == 30
    for team_id, (tri, primary, secondary) in _NBA_TEAMS.items():
        assert team_id >= 1610612737, team_id  # real NBA numeric id range
        assert len(tri) == 3, tri
        assert tri.isupper(), tri
        assert len(primary) == 3 and all(0 <= c <= 255 for c in primary)
        assert len(secondary) == 3 and all(0 <= c <= 255 for c in secondary)


# ---------------------------------------------------------------------------
# _iso8601_duration_to_clock
# ---------------------------------------------------------------------------


def test_iso8601_duration_to_clock_normal() -> None:
    assert _iso8601_duration_to_clock("PT08M41.00S") == "8:41"


def test_iso8601_duration_to_clock_pads_seconds() -> None:
    assert _iso8601_duration_to_clock("PT01M05.00S") == "1:05"


def test_iso8601_duration_to_clock_missing_minutes() -> None:
    assert _iso8601_duration_to_clock("PT45.00S") == "0:45"


def test_iso8601_duration_to_clock_unparseable_returns_input() -> None:
    assert _iso8601_duration_to_clock("garbage") == "garbage"


# ---------------------------------------------------------------------------
# _format_status
# ---------------------------------------------------------------------------


def test_format_status_final_regulation() -> None:
    assert _format_status({"gameStatus": 3, "period": 4}) == ("Final", "final")


def test_format_status_final_single_overtime() -> None:
    assert _format_status({"gameStatus": 3, "period": 5}) == ("F/OT", "final")


def test_format_status_final_double_overtime() -> None:
    assert _format_status({"gameStatus": 3, "period": 6}) == ("F/OT2", "final")


def test_format_status_live_quarter_with_plain_clock() -> None:
    g = {"gameStatus": 2, "period": 3, "gameClock": "8:41"}
    assert _format_status(g) == ("Q3 8:41", "live")


def test_format_status_live_quarter_with_iso8601_clock() -> None:
    g = {"gameStatus": 2, "period": 3, "gameClock": "PT08M41.00S"}
    assert _format_status(g) == ("Q3 8:41", "live")


def test_format_status_live_overtime_period() -> None:
    g = {"gameStatus": 2, "period": 5, "gameClock": "2:15"}
    assert _format_status(g) == ("OT1 2:15", "live")


def test_format_status_preview_uses_local_time() -> None:
    text, abstract = _format_status({"gameStatus": 1, "gameTimeUTC": "2026-08-23T23:35:00Z"})
    assert abstract == "preview"
    assert ":" in text


def test_format_status_preview_bad_date_falls_back_to_sched() -> None:
    text, abstract = _format_status({"gameStatus": 1, "gameTimeUTC": "not-a-date"})
    assert text == "Sched"
    assert abstract == "preview"


# ---------------------------------------------------------------------------
# _parse_games
# ---------------------------------------------------------------------------


def _nba_game(away_id, home_id, away_score, home_score, status=3, period=4,
              away_tri=None, home_tri=None):
    away = {"teamId": away_id, "score": away_score}
    home = {"teamId": home_id, "score": home_score}
    if away_tri:
        away["teamTricode"] = away_tri
    if home_tri:
        home["teamTricode"] = home_tri
    return {
        "awayTeam": away, "homeTeam": home,
        "gameStatus": status, "period": period, "gameClock": "",
    }


def test_parse_games_returns_expected_games_and_computes_winner() -> None:
    payload = {"scoreboard": {"games": [
        _nba_game(1610612744, 1610612747, 120, 110),
    ]}}
    games = _parse_games(payload)
    assert len(games) == 1
    g = games[0]
    assert g.league == "nba"
    assert (g.away_tri, g.home_tri) == ("GSW", "LAL")
    assert g.away_winner is True and g.home_winner is False


def test_parse_games_prefers_payload_tricode_over_table() -> None:
    """The payload's own teamTricode (when present) wins over the local
    team table -- useful if the NBA ever renames/relocates a franchise
    mid-season before this table is updated."""
    payload = {"scoreboard": {"games": [
        _nba_game(1610612744, 1610612747, 100, 90, away_tri="SFW"),
    ]}}
    games = _parse_games(payload)
    assert games[0].away_tri == "SFW"


def test_parse_games_handles_empty_slate() -> None:
    assert _parse_games({"scoreboard": {"games": []}}) == []
    assert _parse_games({}) == []


def test_parse_games_uses_placeholder_for_unknown_team_id() -> None:
    payload = {"scoreboard": {"games": [
        _nba_game(999999999, 1610612747, 0, 0, status=1, period=0),
    ]}}
    games = _parse_games(payload)
    assert games[0].away_tri == "---"
    assert games[0].home_tri == "LAL"


def test_parse_games_live_game_no_winner_yet() -> None:
    payload = {"scoreboard": {"games": [
        _nba_game(1610612744, 1610612747, 60, 55, status=2, period=2),
    ]}}
    games = _parse_games(payload)
    g = games[0]
    assert g.abstract == "live"
    assert g.away_winner is False and g.home_winner is False


# ---------------------------------------------------------------------------
# NBAMode wiring
# ---------------------------------------------------------------------------


def _isolated_config() -> Config:
    return dataclasses.replace(
        Config(),
        state_dir=Path(tempfile.mkdtemp(prefix="ticker-nba-test-")),
    )


def test_nba_mode_no_games_message() -> None:
    mode = NBAMode(_isolated_config())
    assert mode.NO_GAMES_MESSAGE == "No NBA today"
    assert mode.LEAGUE == "nba"


def test_nba_mode_cache_seconds_is_shorter_than_other_leagues() -> None:
    """NBA's CDN feed already caches at roughly this cadence, so it uses
    20s instead of the 30s the other three leagues use."""
    assert NBAMode.CACHE_SECONDS == 20.0


def test_nba_mode_favorite_config_method_round_trips() -> None:
    config = _isolated_config()
    config.set_favorite_team_nba("GSW")
    assert config.current_favorite_team_nba() == "GSW"
    mode = NBAMode(config)
    assert mode._favorite_team() == "GSW"


def test_nba_mode_rejects_invalid_tri_code() -> None:
    config = _isolated_config()
    try:
        config.set_favorite_team_nba("ZZZ")
        assert False, "invalid tri-code should raise"
    except ValueError:
        pass


def test_nba_logo_cache_uses_empty_stub_despite_svg_url_existing() -> None:
    """NBA's real logo URL function returns an SVG (Pillow can't rasterize
    it), so the mode wires its LogoCache with an empty-string stub instead
    of the module-level _logo_url_for -- verify that wiring choice."""
    mode = NBAMode(_isolated_config())
    assert mode._logo_cache._logo_url_for(1610612744) == ""
