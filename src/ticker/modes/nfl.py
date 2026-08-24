# MIT License — Copyright (c) 2026 John Kuok
"""NFL scoreboard.

Driven by ESPN's public site API (``site.api.espn.com``) -- no API key.
Same tri-code + colour + card-rendering pattern as ``mlb.py``; only the
schedule fetch and status formatting are league-specific.

Team logos: ESPN serves per-team PNG logos from a predictable CDN path
(``a.espncdn.com/i/teamlogos/nfl/500/<abbr-lowercase>.png``), so unlike
NHL/NBA this module gets real logo art instead of falling back to
colour blocks.
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

_NFL_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
_NFL_TIMEOUT = 6.0

# 32 NFL clubs. ESPN team_id -> (tri, primary hex, secondary hex).
# Colours copied from official style guides at a saturation level that
# survives the LED gamma curve, same convention as ``_MLB_TEAMS``.
# ESPN's numeric team ids (stable, used across all of their APIs).
_NFL_TEAMS: dict[int, tuple[str, tuple[int, int, int], tuple[int, int, int]]] = {
    22: ("ARI", (150,  30,  40), (  0,   0,   0)),   # Cardinals
    1:  ("ATL", (160,  30,  40), (  0,   0,   0)),   # Falcons
    33: ("BAL", ( 30,  40,  90), (200, 160,  40)),   # Ravens
    2:  ("BUF", ( 30,  60, 150), (200,  30,  40)),   # Bills
    29: ("CAR", (  0, 130, 160), (  0,   0,   0)),   # Panthers
    3:  ("CHI", ( 20,  40,  90), (240, 130,  30)),   # Bears
    4:  ("CIN", (240, 130,  30), (  0,   0,   0)),   # Bengals
    5:  ("CLE", (140,  60,  30), (  0,   0,   0)),   # Browns
    6:  ("DAL", ( 30,  60, 130), (150, 150, 150)),   # Cowboys
    7:  ("DEN", ( 30,  50, 100), (240, 130,  30)),   # Broncos
    8:  ("DET", ( 30,  90, 160), (150, 150, 150)),   # Lions
    9:  ("GB",  ( 30,  60,  40), (240, 200,  50)),   # Packers
    34: ("HOU", ( 20,  30,  60), (240, 130,  30)),   # Texans
    11: ("IND", ( 30,  50, 100), (200, 200, 200)),   # Colts
    30: ("JAX", ( 10,  70,  70), (200, 170,  90)),   # Jaguars
    12: ("KC",  (200,  30,  40), (240, 200,  50)),   # Chiefs
    13: ("LV",  (  0,   0,   0), (150, 150, 150)),   # Raiders
    24: ("LAC", ( 30,  70, 160), (240, 200,  50)),   # Chargers
    14: ("LAR", ( 30,  50, 100), (240, 200,  50)),   # Rams
    15: ("MIA", ( 20, 140, 140), (240, 130,  30)),   # Dolphins
    16: ("MIN", ( 60,  30, 120), (240, 200,  50)),   # Vikings
    17: ("NE",  ( 20,  30,  70), (200,  30,  40)),   # Patriots
    18: ("NO",  (150, 120,  70), (  0,   0,   0)),   # Saints
    19: ("NYG", ( 30,  50, 100), (200,  30,  40)),   # Giants
    20: ("NYJ", ( 20,  60,  50), (  0,   0,   0)),   # Jets
    21: ("PHI", ( 20,  70,  60), (150, 150, 150)),   # Eagles
    23: ("PIT", (  0,   0,   0), (240, 200,  50)),   # Steelers
    25: ("SF",  (170,   0,  30), (200, 170,  90)),   # 49ers
    26: ("SEA", ( 20,  30,  60), (100, 190,  60)),   # Seahawks
    27: ("TB",  (200,  30,  40), (  0,   0,   0)),   # Buccaneers
    10: ("TEN", ( 20,  50, 100), (200, 170,  90)),   # Titans
    28: ("WSH", (140,  30,  40), (240, 200,  50)),   # Commanders
}

_NFL_LOGO_URL = "https://a.espncdn.com/i/teamlogos/nfl/500/{tri}.png"


def _format_status(competition: dict) -> tuple[str, str]:
    """Translate an ESPN competition record to ``(status_line, abstract)``.

    ``status.type.state`` is the top-level bucket: ``pre`` (upcoming),
    ``in`` (live), ``post`` (over). Live games show ``Q3 8:41`` style
    from ``status.period``/``status.displayClock``; final games show
    ``Final`` or ``F/OT`` when ``period`` went past regulation's 4.
    """
    status = competition.get("status", {}) or {}
    status_type = status.get("type", {}) or {}
    state = status_type.get("state") or ""
    period = int(status.get("period") or 0)

    if state == "post":
        if period > 4:
            return "F/OT", "final"
        return "Final", "final"

    if state == "in":
        clock = status.get("displayClock") or ""
        label = f"OT" if period > 4 else f"Q{period}"
        if clock:
            return f"{label} {clock}", "live"
        return label, "live"

    # pre: upcoming. Convert UTC kickoff time to local.
    date_str = competition.get("date") or ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%-I:%M"), "preview"
    except ValueError:
        return "Sched", "preview"


def _parse_games(payload: dict) -> list[Game]:
    """Extract ``Game`` records from an ESPN NFL scoreboard response."""
    out: list[Game] = []
    for event in payload.get("events", []):
        for competition in event.get("competitions", []):
            competitors = competition.get("competitors", []) or []
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            if away is None or home is None:
                continue
            away_team = away.get("team", {}) or {}
            home_team = home.get("team", {}) or {}
            try:
                away_id = int(away_team.get("id") or 0)
                home_id = int(home_team.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if not away_id or not home_id:
                continue
            away_meta = _NFL_TEAMS.get(away_id, ("---", (200, 200, 200), (0, 0, 0)))
            home_meta = _NFL_TEAMS.get(home_id, ("---", (200, 200, 200), (0, 0, 0)))
            status_line, abstract = _format_status(competition)
            try:
                away_score = int(float(away.get("score") or 0))
                home_score = int(float(home.get("score") or 0))
            except (TypeError, ValueError):
                away_score = home_score = 0
            out.append(
                Game(
                    league="nfl",
                    away_id=away_id,
                    home_id=home_id,
                    away_score=away_score,
                    home_score=home_score,
                    status_line=status_line,
                    abstract=abstract,
                    away_winner=bool(away.get("winner")),
                    home_winner=bool(home.get("winner")),
                    away_tri=away_meta[0],
                    home_tri=home_meta[0],
                    away_color=away_meta[1],
                    home_color=home_meta[1],
                )
            )
    return out


def _logo_url_for_factory(tri_by_id: dict[int, str]):
    def _logo_url_for(team_id: int) -> str:
        tri = tri_by_id.get(team_id, "")
        if not tri:
            return ""
        return _NFL_LOGO_URL.format(tri=tri.lower())

    return _logo_url_for


class NFLMode(LeagueMode, Mode):
    """Render live NFL scoreboards, one game per card."""

    LEAGUE = "nfl"
    NO_GAMES_MESSAGE = "No NFL today"
    CACHE_SECONDS = 30.0
    CARD_SECONDS = 6.0
    FAVORITE_CONFIG_METHOD = "current_favorite_team_nfl"

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        Mode.__init__(self, config)
        LeagueMode.__init__(self, config)

    def _build_logo_cache(self) -> LogoCache:
        cache_dir = self.config.state_dir / "sports_logos" / "nfl"
        tri_by_id = {tid: meta[0] for tid, meta in _NFL_TEAMS.items()}
        return LogoCache(cache_dir, _logo_url_for_factory(tri_by_id))

    def _refresh_games(self) -> list[Game]:
        """Poll ESPN's scoreboard endpoint, which always returns the
        current slate (today's games, or the most recent gameday)."""
        payload = http_get_json(_NFL_SCOREBOARD_URL, timeout=_NFL_TIMEOUT)
        return _parse_games(payload)

    def render(self, canvas: Canvas, tick: int) -> None:
        LeagueMode.render(self, canvas, tick)
