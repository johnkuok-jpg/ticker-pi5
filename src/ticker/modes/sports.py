# MIT License — Copyright (c) 2026 John Kuok
"""Sports scoreboards.

The umbrella mode for live sports. First league in is MLB, driven by
the public MLB Stats API (``statsapi.mlb.com``) -- no API key, no
rate-limit surprises, and it's the same feed the mlb.com scoreboard
runs on so scores update within seconds of the play.

The module is designed so a second league (NHL, NBA, ...) can be
grafted on later by writing a new ``_ProviderX.fetch_games()`` that
returns a list of ``Game`` records. The renderer only cares about
the ``Game`` shape.

Layout on a 128x32 panel, one game per card:

    +----+-----------+-----------+----------+
    | AL |  TB  3    |    F/9    |          |
    | HL |  BAL 1    |           |          |
    +----+-----------+-----------+----------+

    * left column: 16x16 team logo, away above home
    * middle: team tri-code and score in MEDIUM (6x12) type
    * right: game status (Final, inning, or start time) in SMALL

Logo lookup mirrors ``stocks.py``: bundled/cached PNG first, then a
one-shot background HTTP fetch against ``midfield.mlbstatic.com`` on
first sighting. The renderer never blocks on the network -- a missing
logo just leaves an empty 16x16 slot until the next tick.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

LOGGER = logging.getLogger("ticker.sports")

# ---------------------------------------------------------------------------
# MLB
# ---------------------------------------------------------------------------

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


@dataclass(slots=True, frozen=True)
class Game:
    """One scheduled or in-progress MLB game.

    ``status_line`` is the short right-column status: "F/9", "Bot 8",
    "7:35" (upcoming pitch time). ``abstract`` collapses the many
    detailed states into three buckets ("preview" / "live" / "final")
    the renderer uses for colour cues.
    """
    away_id: int
    home_id: int
    away_score: int
    home_score: int
    status_line: str
    abstract: str  # "preview" | "live" | "final"
    away_winner: bool
    home_winner: bool
    away_tri: str
    home_tri: str
    away_color: tuple[int, int, int]
    home_color: tuple[int, int, int]


def _http_get_json(url: str, timeout: float = _MLB_TIMEOUT) -> dict:
    """One JSON GET, exception on any non-200 or parse failure.

    Kept tiny so the mode's refresh loop can compose calls easily.
    A UA header is set because MLB's WAF sometimes returns 403 to
    the default Python UA.
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": "ticker-pi5/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


# ---------------------------------------------------------------------------
# Logo lookup
# ---------------------------------------------------------------------------

_LOGO_SIZE = 16   # rendered size on the panel (px)


def _fetch_and_prep_logo(team_id: int) -> Image.Image | None:
    """Fetch a team spot logo and downscale to 16x16 RGBA.

    Returns ``None`` on any network failure so the caller can fall
    back to the tri-code + colour block. MLB serves the spot logo
    as an indexed PNG with a transparent background -- perfect for
    compositing onto the black panel with alpha preserved.
    """
    url = _MLB_LOGO_URL.format(team_id=team_id)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "ticker-pi5/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_MLB_TIMEOUT) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        LOGGER.debug("mlb logo fetch failed for %s: %s", team_id, exc)
        return None
    try:
        raw = Image.open(BytesIO(data)).convert("RGBA")
    except OSError as exc:
        LOGGER.debug("mlb logo decode failed for %s: %s", team_id, exc)
        return None
    return raw.resize((_LOGO_SIZE, _LOGO_SIZE), Image.LANCZOS)


class SportsMode(Mode):
    """Render live MLB scoreboards, one game per card.

    Refresh cadence is generous: MLB updates its schedule endpoint on
    every play but the LED panel only needs to notice changes at card-
    rotation speed. 30 s of drift on a stale score is fine.

    Card rotation walks through today's games; when there are none
    (winter, off day), the mode renders a compact "No games today"
    placeholder that includes the next scheduled date if we can find
    one within a week.
    """

    CACHE_SECONDS = 30
    CARD_SECONDS = 6

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._games: list[Game] = []
        self._last_refresh = -1e9
        self._last_error_at = 0.0
        # Per-process logo bookkeeping.
        self._logo_fetches_in_flight: set[int] = set()
        self._logo_missing: set[int] = set()

    # -- data ----------------------------------------------------------------

    def _refresh(self) -> None:
        """Poll today's MLB schedule.

        Uses the process' local date, which is what a viewer means
        by "today". A game that starts at 7pm Pacific on Aug 23 is
        listed as Aug 23 in the API too because MLB backfills their
        ``officialDate`` with the local date of the venue, but that
        edge only bites for very-early morning games in Hawaii and
        Guam -- irrelevant for a Bay Area viewer.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        url = _MLB_SCHEDULE_URL.format(date=today)
        try:
            payload = _http_get_json(url)
        except Exception as exc:
            # Keep the last good games list so a brief outage doesn't
            # blank the panel. Log at warning so it shows up in
            # journalctl without spamming when the network flaps.
            self._last_error_at = time.monotonic()
            LOGGER.warning("mlb schedule fetch failed: %s", exc)
            return
        self._games = _parse_games(payload)
        self._last_refresh = time.monotonic()

    # -- logos ---------------------------------------------------------------

    @property
    def _logo_cache_dir(self) -> Path:
        d = self.config.state_dir / "sports_logos"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _logo_for(self, team_id: int) -> Image.Image | None:
        """Return a 16x16 RGBA team logo, or ``None`` if we don't have it yet.

        Lookup order:

        1. On-disk cache at ``state_dir/sports_logos/<team_id>.png``.
        2. Background fetch, kicked off once per team_id per process.

        Never blocks the render thread on the network.
        """
        cache_path = self._logo_cache_dir / f"{team_id}.png"
        if cache_path.exists():
            try:
                return Image.open(cache_path).convert("RGBA")
            except OSError:
                cache_path.unlink(missing_ok=True)
        if team_id in self._logo_missing:
            return None
        self._maybe_fetch_logo(team_id, cache_path)
        return None

    def _maybe_fetch_logo(self, team_id: int, cache_path: Path) -> None:
        """Kick off a single background download of ``team_id``'s logo.

        Single-flight per team so a card that revisits a team while
        its fetch is pending doesn't stack up threads. Runs off the
        render loop so the 30 fps schedule never stalls on the network.
        """
        if team_id in self._logo_fetches_in_flight:
            return
        self._logo_fetches_in_flight.add(team_id)

        def _work() -> None:
            try:
                logo = _fetch_and_prep_logo(team_id)
                if logo is None:
                    self._logo_missing.add(team_id)
                    return
                tmp = cache_path.with_suffix(".png.tmp")
                logo.save(tmp, format="PNG")
                tmp.replace(cache_path)
            except Exception as exc:
                LOGGER.debug("logo save failed for %s: %s", team_id, exc)
            finally:
                self._logo_fetches_in_flight.discard(team_id)

        threading.Thread(
            target=_work, daemon=True, name=f"sports-logo-{team_id}",
        ).start()

    # -- render --------------------------------------------------------------

    def _draw_placeholder(self, canvas: Canvas) -> None:
        """When there are no games, show a quiet placeholder.

        Kept intentionally minimal: a single line of amber text
        centred on the panel. Anything fancier competes with vibes
        for the "screen filler" role.
        """
        msg = "No MLB today"
        w = canvas.text_width(msg, font_size=MEDIUM)
        canvas.text(
            (128 - w) // 2, (32 - 12) // 2,
            msg, (240, 200,  90), font_size=MEDIUM,
        )

    def _draw_game(self, canvas: Canvas, game: Game) -> None:
        """Draw one game card.

        Left column: two 16x16 logos, away above home. If a logo
        hasn't downloaded yet, paint a solid team-colour block so
        the card doesn't look broken.

        Middle column: tri-code + score, MEDIUM (6x12) type. The
        winner's row (when Final) renders in white; the loser's in
        the muted amber used by the rest of the ticker for labels.
        Live games render both rows white so no premature "winner"
        colouring while the game is still on.

        Right column: game status (Final / inning / start time) in
        SMALL (5x8) type, centered vertically.
        """
        # -- logos or fallback blocks -------------------------------------
        for side, team_id, colour in (
            (0, game.away_id, game.away_color),
            (16, game.home_id, game.home_color),
        ):
            logo = self._logo_for(team_id)
            if logo is not None:
                canvas.image_buffer.paste(logo, (0, side), logo)
            else:
                # Colour-block fallback. Two-pixel-thick outline so
                # the block reads as a "team block" and not a bug.
                canvas.fill_rect(0, side, 16, 16, colour)

        # -- team tri-codes and scores ------------------------------------
        # Column 18 for the tri-code, column 44 for the score. That
        # leaves x >= 60 for the status. Scores in 3 characters cover
        # everything up to 999 (which will never happen in a baseball
        # game, but the layout is unphased by 2-digit scores).
        away_row_y = 2
        home_row_y = 18

        # Winner colouring only on final games.
        if game.abstract == "final":
            away_tri_colour  = (240, 240, 240) if game.away_winner else (180, 140,  60)
            home_tri_colour  = (240, 240, 240) if game.home_winner else (180, 140,  60)
            away_score_colour = (240, 240, 240) if game.away_winner else (180, 140,  60)
            home_score_colour = (240, 240, 240) if game.home_winner else (180, 140,  60)
        else:
            away_tri_colour = home_tri_colour = (240, 240, 240)
            away_score_colour = home_score_colour = (240, 240, 240)

        canvas.text(18, away_row_y, game.away_tri, away_tri_colour, font_size=MEDIUM)
        canvas.text(18, home_row_y, game.home_tri, home_tri_colour, font_size=MEDIUM)

        # Right-align the score to column ~62 so the numbers line up
        # regardless of one- vs two-digit scores.
        away_score_str = str(game.away_score)
        home_score_str = str(game.home_score)
        aw_w = canvas.text_width(away_score_str, font_size=MEDIUM)
        hw   = canvas.text_width(home_score_str, font_size=MEDIUM)
        canvas.text(66 - aw_w, away_row_y, away_score_str, away_score_colour, font_size=MEDIUM)
        canvas.text(66 - hw,   home_row_y, home_score_str, home_score_colour, font_size=MEDIUM)

        # -- status column (right side) -----------------------------------
        status_colour = {
            "live":    (100, 220, 120),  # green -- game is on
            "final":   (180, 180, 180),  # neutral grey
            "preview": (240, 200,  90),  # amber -- upcoming
        }.get(game.abstract, (240, 240, 240))
        # Center vertically. Small font is 8 tall, panel is 32 tall.
        canvas.text(72, 12, game.status_line, status_colour, font_size=SMALL)

    def render(self, canvas: Canvas, tick: int) -> None:
        # Refresh at most once per CACHE_SECONDS. The very first
        # call kicks off a fetch even if the timer says wait.
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()

        if not self._games:
            self._draw_placeholder(canvas)
            return

        # Favorite team filter: if the user has pinned a tri-code and it
        # actually plays today, dwell on just that game (or both halves of
        # a doubleheader). If it's an off-day, fall back to the full slate
        # rather than blanking the panel -- "MLB" on an off-day should still
        # show baseball, just not the wrong baseball.
        games = self._games
        favorite = self.config.current_favorite_team()
        if favorite:
            filtered = [g for g in games if favorite in (g.away_tri, g.home_tri)]
            if filtered:
                games = filtered

        # Card rotation: ``CARD_SECONDS`` per game, walk through
        # today's slate. Using wall-clock seconds (not tick count)
        # means the rotation cadence is stable regardless of the
        # renderer's frame rate.
        idx = int(time.monotonic() // self.CARD_SECONDS) % len(games)
        self._draw_game(canvas, games[idx])
