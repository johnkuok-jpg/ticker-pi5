# MIT License — Copyright (c) 2026 John Kuok
"""RSS headline marquee mode."""

from __future__ import annotations

import time

import feedparser

from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode


class NewsMode(Mode):
    """Refresh an RSS feed every five minutes and continuously scroll its headlines."""

    CACHE_SECONDS = 300

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.headlines: list[str] = []
        self._last_refresh = 0.0

    def _refresh(self) -> None:
        try:
            feed = feedparser.parse(self.config.news_feed_url)
            headlines = [str(entry.get("title", "")).strip() for entry in feed.entries[:15]]
            self.headlines = [headline for headline in headlines if headline]
        except Exception:
            pass
        finally:
            self._last_refresh = time.monotonic()

    def render(self, canvas: Canvas, tick: int) -> None:
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()
        canvas.clear()
        canvas.text(1, 1, canvas.fit(self.config.news_source_name), (95, 135, 195), SMALL)
        canvas.hline(11, (26, 36, 56))

        # Two scrolling rows. Odd-indexed headlines run on the top row, even on
        # the bottom row -- interleaving so neighboring stories don't sit on the
        # same line at the same time. Separator is spaced so a headline never
        # abuts the next without visible breathing room.
        if self.headlines:
            top = self.headlines[0::2]
            bottom = self.headlines[1::2] or self.headlines[0::2]
            top_str = "   +   ".join(top)
            bottom_str = "   +   ".join(bottom)
        else:
            top_str = bottom_str = "WAITING FOR HEADLINES"

        # Scroll speed: was `tick * 2` (2 px/tick = 60 px/s at 30 fps -- too
        # fast to read). Drop to 1 px every other tick = ~15 px/s, roughly the
        # comfortable reading speed for a stock ticker. Using integer division
        # keeps the offset an int (scroll_text does `offset % period`).
        scroll_offset = tick // 2

        # Two 8px rows in the 20px band below the hairline: row1 at y=13,
        # row2 at y=22, leaving 4px between and 1px bottom margin.
        canvas.scroll_text(13, top_str, (225, 230, 240), scroll_offset, SMALL)
        # Slight offset (+ different content phasing) so the two lines don't
        # visually stripe together as one wide marquee.
        canvas.scroll_text(22, bottom_str, (170, 190, 220), scroll_offset + 40, SMALL)
