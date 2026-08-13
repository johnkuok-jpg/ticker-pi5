# MIT License — Copyright (c) 2026 John Kuok
"""RSS headline marquee mode."""

from __future__ import annotations

import time

import feedparser

from ticker.canvas import Canvas
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
        prefix = f"{self.config.news_source_name}: "
        headline = "  •  ".join(self.headlines) if self.headlines else "Waiting for headlines"
        canvas.text(0, 1, prefix, (80, 100, 130), 7)
        canvas.scroll_text(15, headline, (220, 225, 235), tick * 2, 8)
