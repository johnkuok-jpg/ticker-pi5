# MIT License — Copyright (c) 2026 John Kuok
"""Shared plumbing for the per-league sports scoreboard modes.

Split out of the original ``sports.py`` (MLB-only) when NHL, NFL, and NBA
were added as siblings. Every league module (``mlb.py``, ``nhl.py``,
``nfl.py``, ``nba.py``) fetches its own league's data in its own shape and
converts it into the ``Game`` record defined here -- the renderer and card
layout only ever touch ``Game``, never a league-specific payload.

Card layout on a 128x32 panel, one game per card (unchanged from the
original MLB-only design):

    +----+-----------+-----------+----------+
    | AL |  TB  3    |    F/9    |          |
    | HL |  BAL 1    |           |          |
    +----+-----------+-----------+----------+

    * left column: 16x16 team logo, away above home
    * middle: team tri-code and score in MEDIUM (6x12) type
    * right: game status (Final, period/inning, or start time) in SMALL

Logo lookup is on-disk cache first, then a one-shot background HTTP fetch
on first sighting, keyed by ``(league, team_id)`` so MLB's team 137 and
NHL's team 137 never collide in the same cache directory. The renderer
never blocks on the network -- a missing logo just leaves an empty 16x16
slot until the next tick.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from ticker.canvas import MEDIUM, SMALL, Canvas

LOGGER = logging.getLogger("ticker.sports")

_LOGO_SIZE = 16   # rendered size on the panel (px)
_DEFAULT_TIMEOUT = 6.0


@dataclass(slots=True, frozen=True)
class Game:
    """One scheduled or in-progress game, in any of the four leagues.

    ``status_line`` is the short right-column status: "F/9", "Bot 8",
    "7:35" (upcoming start time), "F", "3rd 4:12", "Q3 8:41". ``abstract``
    collapses the many league-specific detailed states into three buckets
    ("preview" / "live" / "final") the renderer uses for colour cues.

    ``league`` and the numeric team ids are carried through so the logo
    cache can key on ``(league, team_id)`` -- team ids are only unique
    within a single league's numbering scheme.
    """
    league: str  # "mlb" | "nhl" | "nfl" | "nba"
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


def http_get_json(url: str, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """One JSON GET, exception on any non-200 or parse failure.

    A UA header is set because several of these APIs (MLB's WAF, ESPN's
    edge) sometimes return 403 to the default Python UA.
    """
    import json

    req = urllib.request.Request(
        url, headers={"User-Agent": "ticker-pi5/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_and_prep_logo(url: str, timeout: float = _DEFAULT_TIMEOUT) -> Image.Image | None:
    """Fetch a team logo from *url* and downscale to 16x16 RGBA.

    Returns ``None`` on any network failure so the caller can fall back
    to the tri-code + colour block. Some leagues (NHL) serve SVG logos;
    Pillow can't rasterize those, so callers pass a PNG/JPEG URL and this
    function only ever gets bitmap data.
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "ticker-pi5/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        LOGGER.debug("logo fetch failed for %s: %s", url, exc)
        return None
    try:
        raw = Image.open(BytesIO(data)).convert("RGBA")
    except OSError as exc:
        LOGGER.debug("logo decode failed for %s: %s", url, exc)
        return None
    return raw.resize((_LOGO_SIZE, _LOGO_SIZE), Image.LANCZOS)


class LogoCache:
    """Per-league on-disk + in-flight logo cache, shared by every league mode.

    Keyed by ``team_id`` within one instance -- each league mode owns its
    own ``LogoCache`` pointed at its own cache subdirectory, so MLB team 137
    and NHL team 137 are never confused.
    """

    def __init__(self, cache_dir: Path, logo_url_for: Callable[[int], str]) -> None:
        self._cache_dir = cache_dir
        self._logo_url_for = logo_url_for
        self._fetches_in_flight: set[int] = set()
        self._missing: set[int] = set()

    def logo_for(self, team_id: int) -> Image.Image | None:
        """Return a 16x16 RGBA team logo, or ``None`` if we don't have it yet."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self._cache_dir / f"{team_id}.png"
        if cache_path.exists():
            try:
                return Image.open(cache_path).convert("RGBA")
            except OSError:
                cache_path.unlink(missing_ok=True)
        if team_id in self._missing:
            return None
        self._maybe_fetch(team_id, cache_path)
        return None

    def _maybe_fetch(self, team_id: int, cache_path: Path) -> None:
        """Kick off a single background download of *team_id*'s logo.

        Single-flight per team so a card that revisits a team while its
        fetch is pending doesn't stack up threads. Runs off the render
        loop so the frame rate never stalls on the network.
        """
        if team_id in self._fetches_in_flight:
            return
        self._fetches_in_flight.add(team_id)

        def _work() -> None:
            try:
                url = self._logo_url_for(team_id)
                logo = fetch_and_prep_logo(url)
                if logo is None:
                    self._missing.add(team_id)
                    return
                tmp = cache_path.with_suffix(".png.tmp")
                logo.save(tmp, format="PNG")
                tmp.replace(cache_path)
            except Exception as exc:
                LOGGER.debug("logo save failed for %s: %s", team_id, exc)
            finally:
                self._fetches_in_flight.discard(team_id)

        threading.Thread(
            target=_work, daemon=True, name=f"sports-logo-{team_id}",
        ).start()


def draw_placeholder(canvas: Canvas, message: str) -> None:
    """When a league has no games, show a quiet placeholder.

    Kept intentionally minimal: a single line of amber text centred on
    the panel. Anything fancier competes with vibes for the "screen
    filler" role.
    """
    w = canvas.text_width(message, font_size=MEDIUM)
    canvas.text(
        (128 - w) // 2, (32 - 12) // 2,
        message, (240, 200, 90), font_size=MEDIUM,
    )


def draw_game_card(canvas: Canvas, game: Game, logo_cache: LogoCache) -> None:
    """Draw one game card. Shared by all four leagues -- the layout has no
    league-specific logic, it only reads the ``Game`` record.

    Left column: two 16x16 logos, away above home. If a logo hasn't
    downloaded yet, paint a solid team-colour block so the card doesn't
    look broken.

    Middle column: tri-code + score, MEDIUM (6x12) type. The winner's
    row (when Final) renders in white; the loser's in the muted amber
    used by the rest of the ticker for labels. Live games render both
    rows white so no premature "winner" colouring while the game is
    still on.

    Right column: game status (Final / period-clock / start time) in
    SMALL (5x8) type, centered vertically.
    """
    # -- logos or fallback blocks -------------------------------------
    for side, team_id, colour in (
        (0, game.away_id, game.away_color),
        (16, game.home_id, game.home_color),
    ):
        logo = logo_cache.logo_for(team_id)
        if logo is not None:
            canvas.image_buffer.paste(logo, (0, side), logo)
        else:
            # Colour-block fallback. Two-pixel-thick outline so the
            # block reads as a "team block" and not a bug.
            canvas.fill_rect(0, side, 16, 16, colour)

    # -- team tri-codes and scores ------------------------------------
    # Column 18 for the tri-code, column 44 for the score. That leaves
    # x >= 60 for the status. Scores in 3 characters cover everything
    # any of these leagues will realistically produce.
    away_row_y = 2
    home_row_y = 18

    # Winner colouring only on final games.
    if game.abstract == "final":
        away_colour = (240, 240, 240) if game.away_winner else (180, 140, 60)
        home_colour = (240, 240, 240) if game.home_winner else (180, 140, 60)
    else:
        away_colour = home_colour = (240, 240, 240)

    canvas.text(18, away_row_y, game.away_tri, away_colour, font_size=MEDIUM)
    canvas.text(18, home_row_y, game.home_tri, home_colour, font_size=MEDIUM)

    # Right-align the score to column ~66 so the numbers line up
    # regardless of one- vs two-digit scores.
    away_score_str = str(game.away_score)
    home_score_str = str(game.home_score)
    aw_w = canvas.text_width(away_score_str, font_size=MEDIUM)
    hw = canvas.text_width(home_score_str, font_size=MEDIUM)
    canvas.text(66 - aw_w, away_row_y, away_score_str, away_colour, font_size=MEDIUM)
    canvas.text(66 - hw, home_row_y, home_score_str, home_colour, font_size=MEDIUM)

    # -- status column (right side) -----------------------------------
    status_colour = {
        "live": (100, 220, 120),   # green -- game is on
        "final": (180, 180, 180),  # neutral grey
        "preview": (240, 200, 90),  # amber -- upcoming
    }.get(game.abstract, (240, 240, 240))
    # Center vertically. Small font is 8 tall, panel is 32 tall.
    canvas.text(72, 12, game.status_line, status_colour, font_size=SMALL)


class LeagueMode:
    """Common render loop for a single-league scoreboard mode.

    Each league module (``mlb.py``, ``nhl.py``, ``nfl.py``, ``nba.py``)
    subclasses this and supplies ``LEAGUE``, ``NO_GAMES_MESSAGE``,
    ``CACHE_SECONDS``, and a ``_refresh_games() -> list[Game]`` fetcher
    plus a ``_logo_cache`` (a ``LogoCache`` bound to that league's own
    cache subdirectory and logo-URL function).

    ``current_favorite_team`` / ``FAVORITE_CONFIG_ATTR`` let each league
    read its own favorite-team pin from ``Config`` without one league's
    code importing another's team table.
    """

    LEAGUE: str = ""
    NO_GAMES_MESSAGE: str = "No games today"
    CACHE_SECONDS: float = 30.0
    CARD_SECONDS: float = 6.0
    FAVORITE_CONFIG_METHOD: str = ""  # e.g. "current_favorite_team_mlb"

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        self.config = config
        self._games: list[Game] = []
        self._last_refresh = -1e9
        self._last_error_at = 0.0
        self._logo_cache = self._build_logo_cache()

    def _build_logo_cache(self) -> LogoCache:  # pragma: no cover - overridden
        raise NotImplementedError

    def _refresh_games(self) -> list[Game]:  # pragma: no cover - overridden
        raise NotImplementedError

    def games_for_today(self) -> list[Game]:
        """Poll (respecting ``CACHE_SECONDS``) and return today's games.

        Keeps the last good games list on a fetch failure so a brief
        outage doesn't blank the panel.
        """
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            try:
                self._games = self._refresh_games()
                self._last_refresh = time.monotonic()
            except Exception as exc:
                self._last_error_at = time.monotonic()
                LOGGER.warning("%s schedule fetch failed: %s", self.LEAGUE, exc)
        return self._games

    def _favorite_team(self) -> str:
        if not self.FAVORITE_CONFIG_METHOD:
            return ""
        method = getattr(self.config, self.FAVORITE_CONFIG_METHOD, None)
        if method is None:
            return ""
        return method()

    def render(self, canvas: Canvas, tick: int) -> None:
        games = self.games_for_today()

        if not games:
            draw_placeholder(canvas, self.NO_GAMES_MESSAGE)
            return

        # Favorite team filter: if the user has pinned a tri-code and it
        # actually plays today, dwell on just that game (or both halves
        # of a doubleheader). If it's an off-day, fall back to the full
        # slate rather than blanking the panel.
        favorite = self._favorite_team()
        if favorite:
            filtered = [g for g in games if favorite in (g.away_tri, g.home_tri)]
            if filtered:
                games = filtered

        # Card rotation: CARD_SECONDS per game, walk through today's
        # slate. Using wall-clock seconds (not tick count) means the
        # rotation cadence is stable regardless of the renderer's frame
        # rate.
        idx = int(time.monotonic() // self.CARD_SECONDS) % len(games)
        draw_game_card(canvas, games[idx], self._logo_cache)
