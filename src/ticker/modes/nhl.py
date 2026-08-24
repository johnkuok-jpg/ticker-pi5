# MIT License — Copyright (c) 2026 John Kuok
"""NHL scoreboard.

Driven by the NHL's public web API (``api-web.nhle.com``) -- no API key.
Same tri-code + colour + card-rendering pattern as ``mlb.py``; only the
schedule fetch and status formatting are league-specific.

Team logos: the NHL serves SVGs (``assets.nhle.com/logos/nhl/svg/...``),
which Pillow can't rasterize, so this module falls back to the solid
colour-block card treatment for every team rather than attempting a
logo fetch. If a PNG mirror shows up later this can grow a
``_logo_url_for`` the way MLB/NFL/NBA have.
"""

from __future__ import annotations

from ticker.canvas import Canvas
from ticker.modes.base import Mode
from ticker.modes.sports_common import (
    Game,
    LeagueMode,
    LogoCache,
    http_get_json,
)

_NHL_SCORE_URL = "https://api-web.nhle.com/v1/score/now"
_NHL_TIMEOUT = 6.0

# 32 NHL clubs. team_id -> (tri, primary hex, secondary hex). Colours
# copied from official style guides at a saturation level that survives
# the LED gamma curve, same convention as ``_MLB_TEAMS``.
_NHL_TEAMS: dict[int, tuple[str, tuple[int, int, int], tuple[int, int, int]]] = {
    1:  ("NJD", ( 30,  50, 120), (200,  30,  40)),  # Devils
    2:  ("NYI", ( 30,  70, 160), (240, 130,  30)),  # Islanders
    3:  ("NYR", ( 30,  70, 160), (200,  30,  40)),  # Rangers
    4:  ("PHI", (200,  30,  50), (  0,   0,   0)),  # Flyers
    5:  ("PIT", (240, 200,  50), (  0,   0,   0)),  # Penguins
    6:  ("BOS", (240, 200,  50), (  0,   0,   0)),  # Bruins
    7:  ("BUF", ( 30,  60, 130), (240, 200,  50)),  # Sabres
    8:  ("MTL", (200,  30,  50), (  0,   0,   0)),  # Canadiens
    9:  ("OTT", (200,  30,  50), (  0,   0,   0)),  # Senators
    10: ("TOR", ( 30, 110, 200), (  0,   0,   0)),  # Maple Leafs
    12: ("CAR", (200,  30,  50), (  0,   0,   0)),  # Hurricanes
    13: ("FLA", (200,  30,  50), ( 30,  50, 120)),  # Panthers
    14: ("TBL", ( 30, 110, 200), (  0,   0,   0)),  # Lightning
    15: ("WSH", (200,  30,  50), ( 30,  50, 120)),  # Capitals
    16: ("CHI", (200,  30,  50), (  0,   0,   0)),  # Blackhawks
    17: ("DET", (200,  30,  50), (  0,   0,   0)),  # Red Wings
    18: ("NSH", (240, 200,  50), (  0,   0,   0)),  # Predators
    19: ("STL", (30,   70, 160), (240, 200,  50)),  # Blues
    20: ("CGY", (200,  30,  50), (240, 200,  50)),  # Flames
    21: ("COL", ( 60,  30, 120), (200,  30,  50)),  # Avalanche
    22: ("EDM", ( 30,  50, 120), (240, 130,  30)),  # Oilers
    23: ("VAN", ( 30,  70, 160), (  0,   0,   0)),  # Canucks
    24: ("ANA", (240, 130,  30), (  0,   0,   0)),  # Ducks
    25: ("DAL", ( 30,  70, 160), (200, 200, 200)),  # Stars
    26: ("LAK", (  0,   0,   0), (200, 200, 200)),  # Kings
    28: ("SJS", ( 30, 110, 100), (  0,   0,   0)),  # Sharks
    29: ("CBJ", ( 30,  50, 120), (200,  30,  50)),  # Blue Jackets
    30: ("MIN", ( 30,  60, 120), (200,  30,  50)),  # Wild
    52: ("WPG", ( 30,  50, 120), (200, 200, 210)),  # Jets
    53: ("ARI", (200,  50,  70), (240, 200,  90)),  # Coyotes/Utah lineage
    54: ("VGK", (200, 170,  90), (  0,   0,   0)),  # Golden Knights
    55: ("SEA", ( 30, 110, 150), (  0,   0,   0)),  # Kraken
    59: ("UTA", ( 30,  70, 160), (200, 200, 200)),  # Utah Mammoth
}


def _format_status(g: dict) -> tuple[str, str]:
    """Translate an NHL score-endpoint game record to ``(status_line, abstract)``.

    ``gameState`` is the top-level bucket: ``FUT``/``PRE`` (upcoming),
    ``LIVE``/``CRIT`` (in progress, CRIT = late/close), ``OFF``/``FINAL``
    (over). Live games show ``3rd 4:12`` style; final games show ``F``
    or ``F/OT`` / ``F/SO`` when the deciding period wasn't regulation.
    """
    state = g.get("gameState") or ""

    if state in ("OFF", "FINAL"):
        outcome = g.get("gameOutcome", {}) or {}
        last_period = outcome.get("lastPeriodType") or (g.get("periodDescriptor") or {}).get("periodType")
        if last_period == "OT":
            return "F/OT", "final"
        if last_period == "SO":
            return "F/SO", "final"
        return "F", "final"

    if state in ("LIVE", "CRIT"):
        period_desc = g.get("periodDescriptor", {}) or {}
        number = period_desc.get("number")
        period_type = period_desc.get("periodType") or "REG"
        clock = g.get("clock", {}) or {}
        remaining = clock.get("timeRemaining") or ""
        if clock.get("inIntermission"):
            label = "Int"
        elif period_type == "OT":
            label = "OT"
        elif period_type == "SO":
            return "Shootout", "live"
        else:
            ordinal = {1: "1st", 2: "2nd", 3: "3rd"}.get(number, f"P{number}")
            label = ordinal
        if remaining:
            return f"{label} {remaining}", "live"
        return label, "live"

    # FUT / PRE: upcoming. Convert UTC start time to local.
    from datetime import datetime

    start = g.get("startTimeUTC") or ""
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%-I:%M"), "preview"
    except ValueError:
        return "Sched", "preview"


def _parse_games(payload: dict) -> list[Game]:
    """Extract ``Game`` records from an NHL ``/v1/score/now`` response."""
    out: list[Game] = []
    for g in payload.get("games", []):
        away = g.get("awayTeam", {}) or {}
        home = g.get("homeTeam", {}) or {}
        away_id = int(away.get("id") or 0)
        home_id = int(home.get("id") or 0)
        if not away_id or not home_id:
            continue
        away_meta = _NHL_TEAMS.get(away_id, ("---", (200, 200, 200), (0, 0, 0)))
        home_meta = _NHL_TEAMS.get(home_id, ("---", (200, 200, 200), (0, 0, 0)))
        status_line, abstract = _format_status(g)
        away_score = int(away.get("score") or 0)
        home_score = int(home.get("score") or 0)
        out.append(
            Game(
                league="nhl",
                away_id=away_id,
                home_id=home_id,
                away_score=away_score,
                home_score=home_score,
                status_line=status_line,
                abstract=abstract,
                away_winner=abstract == "final" and away_score > home_score,
                home_winner=abstract == "final" and home_score > away_score,
                away_tri=away_meta[0],
                home_tri=home_meta[0],
                away_color=away_meta[1],
                home_color=home_meta[1],
            )
        )
    return out


def _logo_url_for(team_id: int) -> str:
    # No PNG mirror wired up yet -- see module docstring. Returning an
    # empty string makes ``fetch_and_prep_logo`` fail fast (bad URL)
    # rather than hang, so cards always fall back to the colour block.
    return ""


class NHLMode(LeagueMode, Mode):
    """Render live NHL scoreboards, one game per card."""

    LEAGUE = "nhl"
    NO_GAMES_MESSAGE = "No NHL today"
    CACHE_SECONDS = 30.0
    CARD_SECONDS = 6.0
    FAVORITE_CONFIG_METHOD = "current_favorite_team_nhl"

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        Mode.__init__(self, config)
        LeagueMode.__init__(self, config)

    def _build_logo_cache(self) -> LogoCache:
        cache_dir = self.config.state_dir / "sports_logos" / "nhl"
        return LogoCache(cache_dir, _logo_url_for)

    def _refresh_games(self) -> list[Game]:
        """Poll the NHL's "score now" endpoint, which always returns the
        current slate (today, or the most recent day with games)."""
        payload = http_get_json(_NHL_SCORE_URL, timeout=_NHL_TIMEOUT)
        return _parse_games(payload)

    def render(self, canvas: Canvas, tick: int) -> None:
        LeagueMode.render(self, canvas, tick)
