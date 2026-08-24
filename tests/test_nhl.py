# MIT License — Copyright (c) 2026 John Kuok
"""NHL league mode: schedule parsing, status formatting, registration, and
the per-league favorite-team config plumbing.

Card rendering itself is shared and tested in ``test_sports_common.py``.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

from ticker.config import Config
from ticker.modes import MODE_TYPES
from ticker.modes.nhl import NHLMode, _NHL_TEAMS, _format_status, _parse_games


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_nhl_is_registered() -> None:
    assert MODE_TYPES["nhl"] is NHLMode


def test_all_nhl_teams_have_metadata() -> None:
    """33 entries: the 32 current NHL clubs plus the ARI (id 53) legacy
    Coyotes-lineage alias kept alongside UTA (id 59) Utah Mammoth so old
    payloads referencing the retired franchise id still resolve."""
    assert len(_NHL_TEAMS) == 33
    for team_id, (tri, primary, secondary) in _NHL_TEAMS.items():
        assert isinstance(team_id, int) and team_id > 0, team_id
        assert len(tri) == 3, tri
        assert tri.isupper(), tri
        assert len(primary) == 3 and all(0 <= c <= 255 for c in primary)
        assert len(secondary) == 3 and all(0 <= c <= 255 for c in secondary)


def test_nhl_teams_include_arizona_and_utah_lineage() -> None:
    """Team-id 53 (ARI/Coyotes lineage) and 59 (Utah Mammoth) both need
    entries so historical and current payloads both resolve."""
    assert 53 in _NHL_TEAMS
    assert 59 in _NHL_TEAMS
    assert _NHL_TEAMS[59][0] == "UTA"


# ---------------------------------------------------------------------------
# _format_status
# ---------------------------------------------------------------------------


def test_format_status_final_regulation() -> None:
    g = {"gameState": "OFF", "gameOutcome": {"lastPeriodType": "REG"}}
    assert _format_status(g) == ("F", "final")


def test_format_status_final_overtime() -> None:
    g = {"gameState": "FINAL", "gameOutcome": {"lastPeriodType": "OT"}}
    assert _format_status(g) == ("F/OT", "final")


def test_format_status_final_shootout() -> None:
    g = {"gameState": "OFF", "gameOutcome": {"lastPeriodType": "SO"}}
    assert _format_status(g) == ("F/SO", "final")


def test_format_status_live_regulation_period_with_clock() -> None:
    g = {
        "gameState": "LIVE",
        "periodDescriptor": {"number": 3, "periodType": "REG"},
        "clock": {"timeRemaining": "4:12", "inIntermission": False},
    }
    assert _format_status(g) == ("3rd 4:12", "live")


def test_format_status_live_overtime() -> None:
    g = {
        "gameState": "CRIT",
        "periodDescriptor": {"number": 4, "periodType": "OT"},
        "clock": {"timeRemaining": "1:03", "inIntermission": False},
    }
    assert _format_status(g) == ("OT 1:03", "live")


def test_format_status_live_shootout() -> None:
    g = {
        "gameState": "LIVE",
        "periodDescriptor": {"number": 5, "periodType": "SO"},
        "clock": {"timeRemaining": "", "inIntermission": False},
    }
    assert _format_status(g) == ("Shootout", "live")


def test_format_status_live_intermission() -> None:
    g = {
        "gameState": "LIVE",
        "periodDescriptor": {"number": 1, "periodType": "REG"},
        "clock": {"timeRemaining": "0:00", "inIntermission": True},
    }
    status_line, abstract = _format_status(g)
    assert abstract == "live"
    assert status_line.startswith("Int")


def test_format_status_preview_uses_local_time() -> None:
    g = {"gameState": "FUT", "startTimeUTC": "2026-08-23T23:35:00Z"}
    text, abstract = _format_status(g)
    assert abstract == "preview"
    assert ":" in text


def test_format_status_unknown_state_falls_back_to_preview() -> None:
    g = {"gameState": "", "startTimeUTC": "not-a-real-timestamp"}
    text, abstract = _format_status(g)
    assert abstract == "preview"
    assert text == "Sched"


# ---------------------------------------------------------------------------
# _parse_games
# ---------------------------------------------------------------------------


def test_parse_games_returns_expected_games_and_computes_winner() -> None:
    """NHL's payload has no explicit winner flag -- winner is derived from
    score comparison on final games."""
    payload = {"games": [
        {
            "awayTeam": {"id": 10, "score": 4},
            "homeTeam": {"id": 6, "score": 2},
            "gameState": "OFF",
            "gameOutcome": {"lastPeriodType": "REG"},
        },
    ]}
    games = _parse_games(payload)
    assert len(games) == 1
    g = games[0]
    assert g.league == "nhl"
    assert (g.away_tri, g.home_tri) == ("TOR", "BOS")
    assert g.away_winner is True
    assert g.home_winner is False


def test_parse_games_live_game_no_winner_yet() -> None:
    payload = {"games": [
        {
            "awayTeam": {"id": 10, "score": 4},
            "homeTeam": {"id": 6, "score": 2},
            "gameState": "LIVE",
            "periodDescriptor": {"number": 2, "periodType": "REG"},
            "clock": {"timeRemaining": "8:41", "inIntermission": False},
        },
    ]}
    games = _parse_games(payload)
    g = games[0]
    assert g.abstract == "live"
    assert g.away_winner is False and g.home_winner is False


def test_parse_games_handles_empty_slate() -> None:
    assert _parse_games({"games": []}) == []
    assert _parse_games({}) == []


def test_parse_games_uses_placeholder_for_unknown_team_id() -> None:
    payload = {"games": [
        {
            "awayTeam": {"id": 99999, "score": 1},
            "homeTeam": {"id": 10, "score": 2},
            "gameState": "OFF",
            "gameOutcome": {"lastPeriodType": "REG"},
        },
    ]}
    games = _parse_games(payload)
    assert games[0].away_tri == "---"
    assert games[0].home_tri == "TOR"


def test_parse_games_skips_entries_with_missing_team_ids() -> None:
    payload = {"games": [
        {"awayTeam": {}, "homeTeam": {"id": 6, "score": 2}, "gameState": "OFF"},
    ]}
    assert _parse_games(payload) == []


# ---------------------------------------------------------------------------
# NHLMode wiring
# ---------------------------------------------------------------------------


def _isolated_config() -> Config:
    return dataclasses.replace(
        Config(),
        state_dir=Path(tempfile.mkdtemp(prefix="ticker-nhl-test-")),
    )


def test_nhl_mode_no_games_message() -> None:
    mode = NHLMode(_isolated_config())
    assert mode.NO_GAMES_MESSAGE == "No NHL today"
    assert mode.LEAGUE == "nhl"


def test_nhl_mode_favorite_config_method_round_trips() -> None:
    config = _isolated_config()
    config.set_favorite_team_nhl("TOR")
    assert config.current_favorite_team_nhl() == "TOR"
    mode = NHLMode(config)
    assert mode._favorite_team() == "TOR"


def test_nhl_mode_rejects_invalid_tri_code() -> None:
    config = _isolated_config()
    try:
        config.set_favorite_team_nhl("ZZZ")
        assert False, "invalid tri-code should raise"
    except ValueError:
        pass


def test_nhl_logo_url_is_empty_stub() -> None:
    """NHL serves SVG-only logos Pillow can't rasterize, so the URL
    function always returns empty -- cards fall back to colour blocks."""
    from ticker.modes.nhl import _logo_url_for

    assert _logo_url_for(10) == ""
