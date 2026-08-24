# MIT License — Copyright (c) 2026 John Kuok
"""MLB scoreboard.

Driven by the public MLB Stats API (``statsapi.mlb.com``) -- no API key,
no rate-limit surprises, and it's the same feed the mlb.com scoreboard
runs on so scores update within seconds of the play.

Card rendering (logos, layout, colour cues) lives in ``sports_common.py``
and is shared with the NHL/NFL/NBA sibling modes; this file only fetches
MLB's schedule and turns it into ``sports_common.Game`` records.

Was ``sports.py`` / ``SportsMode`` before the sports umbrella grew NHL,
NFL, and NBA siblings. The MLB team/colour table and favorite-team
tri-code validation used to live here and are unchanged in shape --
``config.py`` still imports ``_MLB_TEAMS`` from this module, just under
its new filename.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from ticker.canvas import Canvas
from ticker.modes.base import Mode
from ticker.modes.sports_common import (
    Game,
    LeagueMode,
    LogoCache,
    http_get_json,
)

_MLB_SCHEDULE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&hydrate=linescore&date={date}"
)
_MLB_LOGO_URL = "https://midfield.mlbstatic.com/v1/team/{team_id}/spots/72"
_MLB_TIMEOUT = 6.0

# 30 MLB clubs plus historical aliases. team_id -> (tri, primary hex,
# secondary hex). The primary is used for the tri-code text so a
# glance at colour still hints which side is which even before the
# logo fetches. Colours are copied from official style guides at a
# saturation level that survives the LED gamma curve -- the panel
# lifts blacks and crushes reds, so anything darker than about
# (60, 60, 60) reads as black.
_MLB_TEAMS: dict[int, tuple[str, tuple[int, int, int], tuple[int, int, int]]] = {
    108: ("LAA", (200,  40,  50), (200, 170, 100)),  # Angels
    109: ("ARI", (200,  50,  70), (220, 180,  90)),  # D-backs
    110: ("BAL", (240, 110,  30), (  0,   0,   0)),  # Orioles
    111: ("BOS", (200,  30,  40), (  0,  50, 130)),  # Red Sox
    112: ("CHC", ( 20,  50, 140), (200,  40,  60)),  # Cubs
    113: ("CIN", (200,  30,  40), (  0,   0,   0)),  # Reds
    114: ("CLE", ( 20,  40, 120), (200,  30,  40)),  # Guardians
    115: ("COL", ( 60,  30, 120), (150, 130, 160)),  # Rockies
    116: ("DET", ( 30,  50, 120), (240, 150,  30)),  # Tigers
    117: ("HOU", (240, 120,  30), ( 30,  50, 100)),  # Astros
    118: ("KC",  ( 30,  60, 130), (240, 200,  90)),  # Royals
    119: ("LAD", ( 20,  60, 150), (255, 255, 255)),  # Dodgers
    120: ("WSH", (200,  30,  50), ( 30,  30,  70)),  # Nationals
    121: ("NYM", ( 30,  70, 160), (240, 130,  30)),  # Mets
    133: ("OAK", ( 40, 130,  70), (240, 200,  90)),  # Athletics
    134: ("PIT", (240, 200,  50), (  0,   0,   0)),  # Pirates
    135: ("SD",  (210, 180,  90), ( 60,  40,  20)),  # Padres
    136: ("SEA", ( 30, 110, 100), (200, 210, 220)),  # Mariners
    137: ("SF",  (240, 110,  30), (  0,   0,   0)),  # Giants
    138: ("STL", (200,  40,  60), (240, 200,  90)),  # Cardinals
    139: ("TB",  ( 30, 100, 190), (240, 200,  50)),  # Rays
    140: ("TEX", ( 30,  60, 150), (200,  30,  40)),  # Rangers
    141: ("TOR", ( 30, 110, 200), (200, 200, 210)),  # Blue Jays
    142: ("MIN", ( 30,  70, 140), (200,  30,  40)),  # Twins
    143: ("PHI", (200,  30,  50), ( 30,  60, 130)),  # Phillies
    144: ("ATL", (200,  30,  50), ( 30,  60, 130)),  # Braves
    145: ("CWS", (200, 200, 200), (  0,   0,   0)),  # White Sox
    146: ("MIA", ( 40, 170, 200), (240, 130,  30)),  # Marlins
    147: ("NYY", ( 30,  70, 160), (200, 200, 200)),  # Yankees
    158: ("MIL", ( 30,  60, 120), (200, 170, 100)),  # Brewers
}


def _format_status(game_json: dict) -> tuple[str, str]:
    """Translate an MLB schedule game record to ``(status_line, abstract)``.

    Preview games show the local start time (server-side we get UTC,
    so we convert to the process' local zone -- which is what the Pi
    is set to). Live games show ``Top 7`` / ``Bot 3`` / ``Mid 9``.
    Final games show ``F/9`` for a nine-inning finish and ``F/10`` etc.
    for extras.
    """
    status = game_json.get("status", {}) or {}
    abstract = (status.get("abstractGameState") or "").lower()
    detailed = status.get("detailedState") or ""

    linescore = game_json.get("linescore", {}) or {}
    inning = linescore.get("currentInning")
    inning_state = linescore.get("inningState") or linescore.get("inningHalf") or ""

    if abstract == "final" or detailed in ("Game Over", "Completed Early"):
        # ``scheduledInnings`` is 9 for regular season, 7 for some
        # doubleheaders; ``currentInning`` overshoots for extras.
        scheduled = int(game_json.get("scheduledInnings") or 9)
        played = int(inning or scheduled)
        if played == scheduled:
            return "Final", "final"
        return f"F/{played}", "final"

    if abstract == "live":
        # ``inningState`` is more consistent than ``inningHalf``: it
        # emits Top / Bottom / Middle / End with the same casing every
        # time. Middle/End are the between-half-innings pauses.
        prefix = {
            "Top": "Top",
            "Bottom": "Bot",
            "Middle": "Mid",
            "End": "End",
        }.get(inning_state, inning_state[:3] or "Live")
        try:
            return f"{prefix} {int(inning)}", "live"
        except (TypeError, ValueError):
            return "Live", "live"

    # Preview / scheduled / warmup. Fall back to the game's local
    # start time -- convert UTC to the process' local timezone so
    # a user in San Francisco sees Pacific time.
    date_str = game_json.get("gameDate") or ""
    try:
        # MLB uses trailing Z which Python's ISO parser rejects
        # before 3.11; strip it and treat as UTC.
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%-I:%M"), "preview"
    except ValueError:
        return detailed[:6] or "Sched", "preview"


def _parse_games(payload: dict) -> list[Game]:
    """Extract ``Game`` records from an MLB schedule response.

    Doubleheaders show up as multiple game entries with the same
    teams and different ``gameNumber``; we keep them as separate
    cards so both scores are visible. Suspended and postponed games
    are dropped so we don't render stale zeros.
    """
    out: list[Game] = []
    for date_block in payload.get("dates", []):
        for g in date_block.get("games", []):
            status = g.get("status", {}) or {}
            detailed = status.get("detailedState") or ""
            if detailed in ("Postponed", "Cancelled", "Suspended"):
                continue
            teams = g.get("teams", {}) or {}
            away = teams.get("away", {}) or {}
            home = teams.get("home", {}) or {}
            away_team = away.get("team", {}) or {}
            home_team = home.get("team", {}) or {}
            away_id = int(away_team.get("id") or 0)
            home_id = int(home_team.get("id") or 0)
            if not away_id or not home_id:
                continue
            away_meta = _MLB_TEAMS.get(away_id, ("---", (200, 200, 200), (0, 0, 0)))
            home_meta = _MLB_TEAMS.get(home_id, ("---", (200, 200, 200), (0, 0, 0)))
            status_line, abstract = _format_status(g)
            out.append(
                Game(
                    league="mlb",
                    away_id=away_id,
                    home_id=home_id,
                    away_score=int(away.get("score") or 0),
                    home_score=int(home.get("score") or 0),
                    status_line=status_line,
                    abstract=abstract,
                    away_winner=bool(away.get("isWinner")),
                    home_winner=bool(home.get("isWinner")),
                    away_tri=away_meta[0],
                    home_tri=home_meta[0],
                    away_color=away_meta[1],
                    home_color=home_meta[1],
                )
            )
    return out


def _logo_url_for(team_id: int) -> str:
    return _MLB_LOGO_URL.format(team_id=team_id)


class MLBMode(LeagueMode, Mode):
    """Render live MLB scoreboards, one game per card.

    Refresh cadence is generous: MLB updates its schedule endpoint on
    every play but the LED panel only needs to notice changes at card-
    rotation speed. 30 s of drift on a stale score is fine.

    Card rotation walks through today's games; when there are none
    (winter, off day), the mode renders a compact "No games today"
    placeholder.
    """

    LEAGUE = "mlb"
    NO_GAMES_MESSAGE = "No MLB today"
    CACHE_SECONDS = 30.0
    CARD_SECONDS = 6.0
    FAVORITE_CONFIG_METHOD = "current_favorite_team_mlb"

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        Mode.__init__(self, config)
        LeagueMode.__init__(self, config)

    def _build_logo_cache(self) -> LogoCache:
        cache_dir = self.config.state_dir / "sports_logos" / "mlb"
        return LogoCache(cache_dir, _logo_url_for)

    def _refresh_games(self) -> list[Game]:
        """Poll today's MLB schedule.

        Uses the process' local date, which is what a viewer means by
        "today". A game that starts at 7pm Pacific on Aug 23 is listed
        as Aug 23 in the API too because MLB backfills their
        ``officialDate`` with the local date of the venue, but that
        edge only bites for very-early morning games in Hawaii and
        Guam -- irrelevant for a Bay Area viewer.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        url = _MLB_SCHEDULE_URL.format(date=today)
        payload = http_get_json(url, timeout=_MLB_TIMEOUT)
        return _parse_games(payload)

    def render(self, canvas: Canvas, tick: int) -> None:
        LeagueMode.render(self, canvas, tick)
