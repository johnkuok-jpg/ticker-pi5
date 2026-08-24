# MIT License — Copyright (c) 2026 John Kuok
"""NBA scoreboard.

Driven by the NBA's public live-data CDN (``cdn.nba.com``) -- the same
static JSON feed the nba.com scoreboard polls, no API key. Same
tri-code + colour + card-rendering pattern as ``mlb.py``/``nhl.py``;
only the schedule fetch and status formatting are league-specific.

``gameStatus`` is an int enum: ``1`` = upcoming, ``2`` = live, ``3`` =
final. ``gameStatusText`` is a human string ("7:00 pm ET", "Q3 8:41",
"Final") but its formatting is inconsistent across seasons, so this
module derives its own short status line from the structured fields
(``period``, ``gameClock``) instead of trusting the free-text version.
"""

from __future__ import annotations

from datetime import datetime

from ticker.canvas import Canvas
from ticker.modes.base import Mode
from ticker.modes.sports_common import (
    Game,
    LeagueMode,
    LogoCache,
    http_get_json,
)

_NBA_SCOREBOARD_URL = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
_NBA_TIMEOUT = 6.0

# 30 NBA clubs. team_id -> (tri, primary hex, secondary hex). Colours
# copied from official style guides at a saturation level that survives
# the LED gamma curve, same convention as ``_MLB_TEAMS``.
_NBA_TEAMS: dict[int, tuple[str, tuple[int, int, int], tuple[int, int, int]]] = {
    1610612737: ("ATL", (200,  30,  50), (  0,   0,   0)),  # Hawks
    1610612738: ("BOS", (  0,  80,  60), (200, 200, 200)),  # Celtics
    1610612739: ("CLE", (200,  30,  50), (  0,   0,   0)),  # Cavaliers
    1610612740: ("NOP", ( 20,  40, 120), (200, 170,  90)),  # Pelicans
    1610612741: ("CHI", (200,  30,  50), (  0,   0,   0)),  # Bulls
    1610612742: ("DAL", ( 30,  70, 160), (200, 200, 210)),  # Mavericks
    1610612743: ("DEN", ( 30,  50, 120), (240, 200,  50)),  # Nuggets
    1610612744: ("GSW", (240, 200,  50), ( 30,  70, 160)),  # Warriors
    1610612745: ("HOU", (200,  30,  50), (  0,   0,   0)),  # Rockets
    1610612746: ("LAC", (200,  30,  50), ( 30,  70, 160)),  # Clippers
    1610612747: ("LAL", (240, 200,  50), ( 60,  30, 120)),  # Lakers
    1610612748: ("MIA", (200,  30,  50), (  0,   0,   0)),  # Heat
    1610612749: ("MIL", ( 30,  90,  70), (200, 170,  90)),  # Bucks
    1610612750: ("MIN", ( 30,  50, 120), (200,  30,  50)),  # Timberwolves
    1610612751: ("BKN", (  0,   0,   0), (200, 200, 200)),  # Nets
    1610612752: ("NYK", ( 30,  70, 160), (240, 130,  30)),  # Knicks
    1610612753: ("ORL", ( 30,  90, 100), (  0,   0,   0)),  # Magic
    1610612754: ("IND", ( 30,  50, 120), (240, 200,  50)),  # Pacers
    1610612755: ("PHI", (  0,  50, 130), (200,  30,  50)),  # 76ers
    1610612756: ("PHX", (240, 110,  30), ( 60,  30, 120)),  # Suns
    1610612757: ("POR", (200,  30,  50), (  0,   0,   0)),  # Trail Blazers
    1610612758: ("SAC", (100,  50, 130), (200, 170,  90)),  # Kings
    1610612759: ("SAS", (  0,   0,   0), (200, 200, 210)),  # Spurs
    1610612760: ("OKC", ( 30,  70, 160), (240, 110,  30)),  # Thunder
    1610612761: ("TOR", (200,  30,  50), (  0,   0,   0)),  # Raptors
    1610612762: ("UTA", ( 30,  50, 120), (240, 200,  50)),  # Jazz
    1610612763: ("MEM", ( 30, 110, 150), (  0,   0,   0)),  # Grizzlies
    1610612764: ("WAS", (  0,  50, 130), (200,  30,  50)),  # Wizards
    1610612765: ("DET", (200,  30,  50), (  0,   0,   0)),  # Pistons
    1610612766: ("CHA", ( 30,  70, 160), (200,  30,  50)),  # Hornets
}


def _format_status(g: dict) -> tuple[str, str]:
    """Translate an NBA scoreboard game record to ``(status_line, abstract)``.

    ``gameStatus``: 1 = upcoming, 2 = live, 3 = final.
    """
    status = int(g.get("gameStatus") or 1)

    if status == 3:
        # No overtime marker in the structured fields, but a period > 4
        # means it went to OT -- ``period`` counts overtime periods too.
        period = int(g.get("period") or 4)
        if period > 4:
            return f"F/OT{period - 4}" if period > 5 else "F/OT", "final"
        return "Final", "final"

    if status == 2:
        period = int(g.get("period") or 1)
        clock = (g.get("gameClock") or "").strip()
        # ``gameClock`` comes back ISO-8601 duration style (PT08M41.00S)
        # on some responses and plain "8:41" on others; normalize both.
        if clock.startswith("PT"):
            clock = _iso8601_duration_to_clock(clock)
        label = f"OT{period - 4}" if period > 4 else f"Q{period}"
        if clock:
            return f"{label} {clock}", "live"
        return label, "live"

    # Upcoming: convert UTC tip-off time to local.
    start = g.get("gameTimeUTC") or ""
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%-I:%M"), "preview"
    except ValueError:
        return "Sched", "preview"


def _iso8601_duration_to_clock(duration: str) -> str:
    """Convert ``PT08M41.00S`` to ``8:41``. Best-effort; falls back to
    stripping the ``PT``/``S`` wrapper if the minutes marker is missing."""
    import re

    match = re.match(r"PT(?:(\d+)M)?(?:([\d.]+)S)?", duration)
    if not match:
        return duration
    minutes = int(match.group(1) or 0)
    seconds = int(float(match.group(2) or 0))
    return f"{minutes}:{seconds:02d}"


def _parse_games(payload: dict) -> list[Game]:
    """Extract ``Game`` records from an NBA ``todaysScoreboard`` response."""
    out: list[Game] = []
    scoreboard = payload.get("scoreboard", {}) or {}
    for g in scoreboard.get("games", []):
        away = g.get("awayTeam", {}) or {}
        home = g.get("homeTeam", {}) or {}
        away_id = int(away.get("teamId") or 0)
        home_id = int(home.get("teamId") or 0)
        if not away_id or not home_id:
            continue
        away_meta = _NBA_TEAMS.get(away_id, ("---", (200, 200, 200), (0, 0, 0)))
        home_meta = _NBA_TEAMS.get(home_id, ("---", (200, 200, 200), (0, 0, 0)))
        status_line, abstract = _format_status(g)
        away_score = int(away.get("score") or 0)
        home_score = int(home.get("score") or 0)
        out.append(
            Game(
                league="nba",
                away_id=away_id,
                home_id=home_id,
                away_score=away_score,
                home_score=home_score,
                status_line=status_line,
                abstract=abstract,
                away_winner=abstract == "final" and away_score > home_score,
                home_winner=abstract == "final" and home_score > away_score,
                away_tri=away.get("teamTricode") or away_meta[0],
                home_tri=home.get("teamTricode") or home_meta[0],
                away_color=away_meta[1],
                home_color=home_meta[1],
            )
        )
    return out


def _logo_url_for(team_id: int) -> str:
    return f"https://cdn.nba.com/logos/nba/{team_id}/global/L/logo.svg"


class NBAMode(LeagueMode, Mode):
    """Render live NBA scoreboards, one game per card."""

    LEAGUE = "nba"
    NO_GAMES_MESSAGE = "No NBA today"
    CACHE_SECONDS = 20.0  # NBA's CDN feed already caches ~ this cadence
    CARD_SECONDS = 6.0
    FAVORITE_CONFIG_METHOD = "current_favorite_team_nba"

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        Mode.__init__(self, config)
        LeagueMode.__init__(self, config)

    def _build_logo_cache(self) -> LogoCache:
        cache_dir = self.config.state_dir / "sports_logos" / "nba"
        # NBA's CDN logo is SVG too -- same limitation as NHL, see
        # ``nhl.py``. Kept as a stub URL fn so a future PNG source is a
        # one-line change instead of a new cache wiring.
        return LogoCache(cache_dir, lambda team_id: "")

    def _refresh_games(self) -> list[Game]:
        payload = http_get_json(_NBA_SCOREBOARD_URL, timeout=_NBA_TIMEOUT)
        return _parse_games(payload)

    def render(self, canvas: Canvas, tick: int) -> None:
        LeagueMode.render(self, canvas, tick)
