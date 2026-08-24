# MIT License — Copyright (c) 2026 John Kuok
"""Sports umbrella mode.

Composes the four league modes (``mlb.py``, ``nhl.py``, ``nfl.py``,
``nba.py``) into the single ``sports`` slot in ``VALID_MODES`` /
``MODE_TYPES``. There is one top-level "Sports" entry in the mode
list -- not four -- and this class is what makes that one entry show
whichever leagues actually have games today.

Rotation: each tick, ask every league mode for today's games (each
mode does its own polling/caching per ``LeagueMode.games_for_today``).
Leagues with no games today are skipped entirely -- e.g. in the NFL
off-season, the panel just cycles MLB/NHL/NBA without an awkward
"No NFL today" card wasting rotation time. If literally no league has
a game today, the placeholder from whichever league sorts first
(alphabetically, for determinism) is shown so the panel still says
something rather than going blank.

Each league keeps its own favorite-team filter and card-rotation
cadence (``CARD_SECONDS``) -- this class only decides which league's
turn it is; once a league is "on", ``LeagueMode.render`` handles the
within-league card cycling exactly as it does when driven standalone.
"""

from __future__ import annotations

import time

from ticker.canvas import Canvas
from ticker.modes.base import Mode
from ticker.modes.mlb import MLBMode
from ticker.modes.nba import NBAMode
from ticker.modes.nfl import NFLMode
from ticker.modes.nhl import NHLMode
from ticker.modes.sports_common import draw_placeholder

# One rotation slot per league that has games today. This is
# independent of each league's own ``CARD_SECONDS`` (which governs how
# long a single game's card is shown within that league's turn) --
# this is how long a *league* holds the whole panel before handing off
# to the next league with games today.
LEAGUE_SECONDS = 6.0


class SportsMode(Mode):
    """Rotate across MLB/NHL/NFL/NBA, showing only leagues with games today.

    Kept intentionally thin: all of the per-league fetch/cache/render
    logic lives in the league modes themselves (``LeagueMode`` in
    ``sports_common.py`` and each league's subclass). This class's only
    job is picking which league is "on" for the current rotation slot.
    """

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        # Order matters only for the deterministic empty-slate fallback
        # (alphabetical: mlb, nba, nfl, nhl) -- the rotation itself
        # walks whichever subset has games, in this same fixed order,
        # so the sequence is stable frame to frame.
        self._leagues: list[Mode] = [
            MLBMode(config),
            NBAMode(config),
            NFLMode(config),
            NHLMode(config),
        ]

    def _leagues_with_games(self) -> list[Mode]:
        active = []
        for league in self._leagues:
            try:
                if league.games_for_today():  # type: ignore[attr-defined]
                    active.append(league)
            except Exception:
                # A single league's fetch blowing up shouldn't take the
                # whole umbrella mode down with it -- just skip it this
                # tick, exactly like a per-league fetch failure does
                # internally via LeagueMode.games_for_today's own
                # try/except (this guards the call itself, e.g. bad
                # league wiring turning into an AttributeError).
                continue
        return active

    def render(self, canvas: Canvas, tick: int) -> None:
        active = self._leagues_with_games()

        if not active:
            # Nobody's playing anywhere today -- show the first league's
            # (MLB's) placeholder so the panel still reads as "Sports"
            # rather than rendering nothing.
            draw_placeholder(canvas, self._leagues[0].NO_GAMES_MESSAGE)  # type: ignore[attr-defined]
            return

        idx = int(time.monotonic() // LEAGUE_SECONDS) % len(active)
        active[idx].render(canvas, tick)
