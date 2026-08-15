# MIT License — Copyright (c) 2026 John Kuok
"""YouTube trending videos mode.

Pulls the current YouTube trending list from Piped (a free community proxy,
no API key required) and cycles through the top videos. Each card shows:

  • YouTube wordmark tag ("YouTube" in red)
  • Channel name (small, dim)
  • Video title (scrolls if it doesn't fit)
  • View count formatted compactly (e.g. "2.4M views")

Falls back gracefully if the network is down or Piped is unreachable.
"""

from __future__ import annotations

import time

import requests

from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode

# Piped instances rotate; try a couple in order so we survive the primary
# going offline. Any instance exposes /trending?region=US that returns a JSON
# list of {title, uploaderName, views, ...} objects.
PIPED_INSTANCES = (
    "https://pipedapi.kavin.rocks",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.adminforge.de",
)

# How long each trending card stays on-screen before advancing.
SECONDS_PER_CARD = 6.0


def _format_views(views: int) -> str:
    """Format a view count into a compact string like '2.4M views'."""
    if views >= 1_000_000_000:
        return f"{views / 1_000_000_000:.1f}B views"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}M views"
    if views >= 1_000:
        return f"{views / 1_000:.0f}K views"
    return f"{views} views"


class YouTubeMode(Mode):
    """Cycle through YouTube trending videos, one card at a time."""

    CACHE_SECONDS = 600  # refresh trending list every 10 minutes

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        # Each item is {"title": str, "channel": str, "views": int}
        self.videos: list[dict] = []
        self._last_refresh = 0.0
        self._card_started = time.monotonic()
        self._card_index = 0

    def _refresh(self) -> None:
        """Query Piped for trending. Skip silently on any failure."""
        region = (getattr(self.config, "youtube_region", "US") or "US").upper()
        for base in PIPED_INSTANCES:
            try:
                r = requests.get(
                    f"{base}/trending",
                    params={"region": region},
                    timeout=5,
                )
                r.raise_for_status()
                data = r.json()
                # Piped returns a list of stream objects. We take the top 15 and
                # keep the fields we render.
                videos = []
                for item in data[:15]:
                    title = str(item.get("title", "")).strip()
                    channel = str(item.get("uploaderName", "")).strip()
                    views = int(item.get("views") or 0)
                    if title:
                        videos.append({"title": title, "channel": channel, "views": views})
                if videos:
                    self.videos = videos
                    self._last_refresh = time.monotonic()
                    return
            except Exception:
                continue  # try next mirror
        # If every mirror failed, keep the stale list and try again after CACHE_SECONDS.
        self._last_refresh = time.monotonic()

    def render(self, canvas: Canvas, tick: int) -> None:
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()

        canvas.clear()

        # Header: red "YouTube" wordmark tag on the left
        canvas.text(1, 1, "YouTube", (240, 40, 40), SMALL)
        canvas.hline(11, (60, 20, 24))  # dim red divider mirrors the wordmark

        if not self.videos:
            # Show a friendly waiting state instead of a blank matrix.
            canvas.scroll_text(15, "LOADING YOUTUBE TRENDING", (200, 200, 200), tick * 2, SMALL)
            return

        # Advance card every SECONDS_PER_CARD.
        now = time.monotonic()
        if now - self._card_started >= SECONDS_PER_CARD:
            self._card_index = (self._card_index + 1) % len(self.videos)
            self._card_started = now

        video = self.videos[self._card_index]

        # Row 1 (Y=13): channel name — small, dim white, truncated to fit.
        canvas.text(1, 13, canvas.fit(video["channel"]), (150, 150, 160), SMALL)

        # Row 2 (Y=23): title — scrolls if it overflows the 128 px width. We
        # bias the scroll speed lower than news so titles are easier to read.
        canvas.scroll_text(23, video["title"], (240, 240, 245), tick, SMALL)

        # Row 3 wraps back into the header — instead put views into the
        # top-right corner of the header row so we don't need extra vertical
        # space. Right-aligned by measuring text width first.
        views_str = _format_views(video["views"])
        views_w = canvas.text_width(views_str, SMALL)
        canvas.text(128 - views_w - 1, 1, views_str, (200, 200, 210), SMALL)
