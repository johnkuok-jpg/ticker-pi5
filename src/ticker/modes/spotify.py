# MIT License — Copyright (c) 2026 John Kuok
"""Local raspotify now-playing mode using MPRIS with a log fallback."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode


@dataclass(slots=True)
class Track:
    title: str
    artist: str


class SpotifyMode(Mode):
    """Read local MPRIS metadata; this avoids requiring a Spotify Web API key."""

    CACHE_SECONDS = 5
    MPRIS_SERVICE = "org.mpris.MediaPlayer2.spotifyd"
    MPRIS_PATH = "/org/mpris/MediaPlayer2"

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.track: Track | None = None
        self._last_refresh = 0.0

    def _from_mpris(self) -> Track | None:
        try:
            import pydbus

            for bus_factory in (pydbus.SessionBus, pydbus.SystemBus):
                try:
                    player = bus_factory().get(self.MPRIS_SERVICE, self.MPRIS_PATH)
                    if str(player.PlaybackStatus) != "Playing":
                        continue
                    metadata = player.Metadata
                    title = str(metadata.get("xesam:title", ""))
                    artists = metadata.get("xesam:artist", [])
                    artist = ", ".join(str(item) for item in artists) if artists else "Unknown artist"
                    if title:
                        return Track(title, artist)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _from_log(self) -> Track | None:
        try:
            lines = self.config.raspotify_log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-250:]
        except OSError:
            return None
        # The fallback intentionally accepts common "track - artist" log wording.
        pattern = re.compile(r"(?:playing|track).*?[:=-]\s*(?P<title>.+?)\s+-\s+(?P<artist>.+)$", re.I)
        for line in reversed(lines):
            match = pattern.search(line)
            if match:
                return Track(match.group("title").strip(), match.group("artist").strip())
        return None

    def _refresh(self) -> None:
        self.track = self._from_mpris() or self._from_log()
        self._last_refresh = time.monotonic()

    def render(self, canvas: Canvas, tick: int) -> None:
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()
        canvas.clear()
        if not self.track:
            canvas.text_centered(7, "NO MUSIC", (150, 150, 165), SMALL)
            canvas.text_centered(18, "START RASPOTIFY", (70, 80, 95), SMALL)
            return
        # TODO: add album-art retrieval when a stable local source is selected.
        canvas.scroll_text(6, self.track.title, (80, 240, 130), tick * 2, SMALL)
        canvas.scroll_text(18, self.track.artist, (150, 170, 195), tick, SMALL)
