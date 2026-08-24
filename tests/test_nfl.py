# MIT License — Copyright (c) 2026 John Kuok
"""NFL league mode: schedule parsing, status formatting, registration, and
the per-league favorite-team config plumbing.

Card rendering itself is shared and tested in ``test_sports_common.py``.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

from ticker.config import Config
from ticker.modes import MODE_TYPES
from ticker.modes.nfl import NFLMode, _NFL_TEAMS, _format_status, _parse_games


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_nfl_is_registered() -> None:
    assert MODE_TYPES["nfl"] is NFLMode


def test_all_32_nfl_teams_have_metadata() -> None:
    assert len(_NFL_TEAMS) == 32
    for team_id, (tri, primary, secondary) in _NFL_TEAMS.items():
        assert isinstance(team_id, int) and team_id > 0, team_id
        assert len(tri) in (2, 3), tri
        assert tri.isupper(), tri
        assert len(primary) == 3 and all(0 <= c <= 255 for c in primary)
        assert len(secondary) == 3 and all(0 <= c <= 255 for c in secondary)


# ---------------------------------------------------------------------------
# _format_status
# ---------------------------------------------------------------------------


def _competition(state: str, period: int = 0, clock: str = "", date: str = "") -> dict:
    return {
        "status": {"type": {"state": state}, "period": period, "displayClock": clock},
        "date": date,
    }


def test_format_status_final_regulation() -> None:
    assert _format_status(_competition("post", period=4)) == ("Final", "final")


def test_format_status_final_overtime() -> None:
    assert _format_status(_competition("post", period=5)) == ("F/OT", "final")


def test_format_status_live_quarter_with_clock() -> None:
    assert _format_status(_competition("in", period=3, clock="8:41")) == ("Q3 8:41", "live")


def test_format_status_live_overtime() -> None:
    assert _format_status(_competition("in", period=5, clock="2:00")) == ("OT 2:00", "live")


def test_format_status_live_no_clock_falls_back_to_label_only() -> None:
    assert _format_status(_competition("in", period=2, clock="")) == ("Q2", "live")


def test_format_status_preview_uses_local_time() -> None:
    text, abstract = _format_status(_competition("pre", date="2026-08-23T23:35:00Z"))
    assert abstract == "preview"
    assert ":" in text
    hh, mm = text.split(":")
    assert 0 <= int(hh) <= 23
    assert 0 <= int(mm) <= 59


def test_format_status_preview_bad_date_falls_back_to_sched() -> None:
    text, abstract = _format_status(_competition("pre", date="not-a-date"))
    assert abstract == "preview"
    assert text == "Sched"


# ---------------------------------------------------------------------------
# _parse_games
# ---------------------------------------------------------------------------


def _event(away_id, home_id, away_score, home_score, state="post", period=4,
           away_winner=False, home_winner=False, clock="", date=""):
    return {
        "competitions": [{
            "status": {"type": {"state": state}, "period": period, "displayClock": clock},
            "date": date,
            "competitors": [
                {"homeAway": "away", "team": {"id": away_id}, "score": away_score, "winner": away_winner},
                {"homeAway": "home", "team": {"id": home_id}, "score": home_score, "winner": home_winner},
            ],
        }],
    }


def test_parse_games_returns_expected_games() -> None:
    payload = {"events": [_event(12, 6, 27, 24, away_winner=True)]}
    games = _parse_games(payload)
    assert len(games) == 1
    g = games[0]
    assert g.league == "nfl"
    assert (g.away_tri, g.home_tri) == ("KC", "DAL")
    assert (g.away_score, g.home_score) == (27, 24)
    assert g.away_winner is True and g.home_winner is False


def test_parse_games_handles_empty_events() -> None:
    assert _parse_games({"events": []}) == []
    assert _parse_games({}) == []


def test_parse_games_skips_events_missing_a_side() -> None:
    payload = {"events": [{
        "competitions": [{
            "status": {"type": {"state": "pre"}, "period": 0, "displayClock": ""},
            "date": "",
            "competitors": [
                {"homeAway": "away", "team": {"id": 12}, "score": 0, "winner": False},
            ],
        }],
    }]}
    assert _parse_games(payload) == []


def test_parse_games_uses_placeholder_for_unknown_team_id() -> None:
    payload = {"events": [_event(99999, 6, 0, 0, state="pre", period=0)]}
    games = _parse_games(payload)
    assert games[0].away_tri == "---"
    assert games[0].home_tri == "DAL"


def test_parse_games_handles_float_scores_gracefully() -> None:
    """ESPN occasionally serializes scores as numeric strings; make sure
    the int(float(...)) coercion path doesn't blow up on garbage."""
    payload = {"events": [{
        "competitions": [{
            "status": {"type": {"state": "post"}, "period": 4, "displayClock": ""},
            "date": "",
            "competitors": [
                {"homeAway": "away", "team": {"id": 12}, "score": "not-a-number", "winner": False},
                {"homeAway": "home", "team": {"id": 6}, "score": "24", "winner": True},
            ],
        }],
    }]}
    games = _parse_games(payload)
    assert games[0].away_score == 0
    assert games[0].home_score == 0  # both fall back to 0 on the shared except branch


# ---------------------------------------------------------------------------
# NFLMode wiring
# ---------------------------------------------------------------------------


def _isolated_config() -> Config:
    return dataclasses.replace(
        Config(),
        state_dir=Path(tempfile.mkdtemp(prefix="ticker-nfl-test-")),
    )


def test_nfl_mode_no_games_message() -> None:
    mode = NFLMode(_isolated_config())
    assert mode.NO_GAMES_MESSAGE == "No NFL today"
    assert mode.LEAGUE == "nfl"


def test_nfl_mode_favorite_config_method_round_trips() -> None:
    config = _isolated_config()
    config.set_favorite_team_nfl("KC")
    assert config.current_favorite_team_nfl() == "KC"
    mode = NFLMode(config)
    assert mode._favorite_team() == "KC"


def test_nfl_mode_rejects_invalid_tri_code() -> None:
    config = _isolated_config()
    try:
        config.set_favorite_team_nfl("ZZZ")
        assert False, "invalid tri-code should raise"
    except ValueError:
        pass


def test_nfl_logo_url_uses_espn_cdn_with_lowercase_tri() -> None:
    """NFL is the one league with a real logo CDN wired up (unlike the
    NHL/NBA SVG-only stubs)."""
    from ticker.modes.nfl import _logo_url_for_factory

    logo_url_for = _logo_url_for_factory({12: "KC"})
    assert logo_url_for(12) == "https://a.espncdn.com/i/teamlogos/nfl/500/kc.png"
    assert logo_url_for(99999) == ""
