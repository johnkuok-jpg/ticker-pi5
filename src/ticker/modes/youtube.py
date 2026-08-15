# MIT License — Copyright (c) 2026 John Kuok
"""YouTube trending mode — actually plays video (at 57x32 pixels).

The LED matrix is 128x32. YouTube videos are 16:9, so a downsampled frame at
32 tall is 57 wide. We render the video in the LEFT 57 columns and use the
remaining 71 columns for scrolling metadata (title, channel, view count).

Pipeline:
  1. Piped API returns the top trending videos (no API key needed).
  2. yt-dlp downloads a low-resolution stream to /tmp/ticker-yt-cache/.
  3. imageio-ffmpeg extracts every Nth frame at 32 rows tall, 12 fps.
  4. Frames are kept in memory as a numpy array; render() blits the current
     one to the canvas each tick.
  5. When the video ends (or download fails), advance to the next trending
     video. The mode never blocks the LED loop: downloads happen in a
     background thread and playback shows a placeholder until frames arrive.

Requirements:
  - yt-dlp (Python package; pulls video URLs and streams)
  - imageio-ffmpeg (bundles static ffmpeg binary; no system apt install)

Fails gracefully to a "loading" screen if downloads are blocked or the Pi
is offline. Never crashes the renderer.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode

# Piped instances rotate; try a couple in order so we survive one going down.
PIPED_INSTANCES = (
    "https://pipedapi.kavin.rocks",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.adminforge.de",
)

# Frame geometry for the tiny video window.
VIDEO_W = 57       # 16:9 aspect fitted to 32 tall = 56.9, rounded up
VIDEO_H = 32
VIDEO_X = 0        # left-aligned on the matrix
META_X = VIDEO_W + 3   # small gap between video and text

# Playback pacing.
TARGET_FPS = 12    # LED matrix runs at 30 fps; 12 fps video = every ~2.5 ticks
CACHE_DIR = Path(tempfile.gettempdir()) / "ticker-yt-cache"

# Trending list refresh interval (10 min).
TRENDING_CACHE_SECONDS = 600


def _format_views(views: int) -> str:
    if views >= 1_000_000_000:
        return f"{views / 1_000_000_000:.1f}B"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}M"
    if views >= 1_000:
        return f"{views / 1_000:.0f}K"
    return str(views)


@dataclass
class VideoInfo:
    id: str
    title: str
    channel: str
    views: int


class YouTubeMode(Mode):
    """Play tiny 57x32 YouTube videos with scrolling metadata."""

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Trending list state.
        self.videos: list[VideoInfo] = []
        self._trending_refreshed = 0.0

        # Current playback state.
        self._current_index = 0
        self._frames: np.ndarray | None = None  # shape (N, 32, VIDEO_W, 3), uint8
        self._current_video: VideoInfo | None = None
        self._playback_started = 0.0

        # Download worker.
        self._download_thread: threading.Thread | None = None
        self._download_target: str | None = None   # video id currently being fetched

        # Kick off the first trending fetch immediately in a thread so the
        # render loop doesn't block on the first frame.
        self._refresh_trending_async()

    # ------------------------------------------------------------------ trending

    def _refresh_trending_async(self) -> None:
        def _work():
            region = (getattr(self.config, "youtube_region", "US") or "US").upper()
            for base in PIPED_INSTANCES:
                try:
                    r = requests.get(f"{base}/trending", params={"region": region}, timeout=5)
                    r.raise_for_status()
                    data = r.json()
                    vids = []
                    for item in data[:15]:
                        # Piped video URLs look like "/watch?v=<id>"
                        url = str(item.get("url", ""))
                        vid = url.split("=", 1)[-1] if "=" in url else ""
                        title = str(item.get("title", "")).strip()
                        channel = str(item.get("uploaderName", "")).strip()
                        views = int(item.get("views") or 0)
                        if vid and title:
                            vids.append(VideoInfo(vid, title, channel, views))
                    if vids:
                        self.videos = vids
                        self._trending_refreshed = time.monotonic()
                        return
                except Exception:
                    continue
            self._trending_refreshed = time.monotonic()

        t = threading.Thread(target=_work, daemon=True, name="yt-trending")
        t.start()

    # ------------------------------------------------------------------ download

    def _download_and_decode_async(self, video: VideoInfo) -> None:
        """Fetch this video, decode to a numpy frame stack. Runs in a thread."""
        if self._download_thread and self._download_thread.is_alive():
            return   # a download is already in flight
        self._download_target = video.id

        def _work():
            try:
                frames = _fetch_and_decode(video.id)
                # Only apply if we're still supposed to be showing this video.
                if self._download_target == video.id:
                    self._frames = frames
                    self._current_video = video
                    self._playback_started = time.monotonic()
            except Exception:
                # Swallow any download/decode error. The renderer will keep
                # showing a placeholder and eventually advance.
                pass

        self._download_thread = threading.Thread(target=_work, daemon=True, name="yt-decode")
        self._download_thread.start()

    def _advance(self) -> None:
        """Move to the next trending video and start its download."""
        if not self.videos:
            return
        self._current_index = (self._current_index + 1) % len(self.videos)
        self._frames = None   # clear current frames so the placeholder shows
        self._download_and_decode_async(self.videos[self._current_index])

    # ------------------------------------------------------------------ render

    def render(self, canvas: Canvas, tick: int) -> None:
        # Refresh trending list every 10 minutes (async, non-blocking).
        if time.monotonic() - self._trending_refreshed >= TRENDING_CACHE_SECONDS:
            self._refresh_trending_async()

        # If we haven't started any download yet and we have videos, kick one off.
        if self._frames is None and self._current_video is None and self.videos:
            self._download_and_decode_async(self.videos[self._current_index])

        canvas.clear()

        # Metadata panel is always the same layout — top row wordmark, then
        # channel + views, then title scrolling on the bottom line.
        self._render_metadata(canvas, tick)

        # Video panel: current frame if we have one, otherwise placeholder.
        if self._frames is not None and len(self._frames) > 0:
            frame_idx = int((time.monotonic() - self._playback_started) * TARGET_FPS)
            if frame_idx >= len(self._frames):
                # Video ended — advance to the next trending video.
                self._advance()
                self._render_placeholder(canvas)
                return
            frame = self._frames[frame_idx]
            self._blit_frame(canvas, frame)
        else:
            self._render_placeholder(canvas)

    def _blit_frame(self, canvas: Canvas, frame: np.ndarray) -> None:
        """Copy one (32, VIDEO_W, 3) RGB frame into the left of the canvas."""
        # Use PIL image blit — the canvas has an .image_buffer we can paste onto.
        from PIL import Image
        img = Image.fromarray(frame, mode="RGB")
        canvas.image(VIDEO_X, 0, img)

    def _render_metadata(self, canvas: Canvas, tick: int) -> None:
        # Top-right: YouTube wordmark + view count on the same row.
        # We have 71 px (X=60..127) for both.
        vid = self._current_video or (self.videos[self._current_index] if self.videos else None)
        if vid is None:
            canvas.text(META_X, 12, "YouTube", (240, 40, 40), SMALL)
            canvas.text(META_X, 22, "loading", (150, 150, 150), SMALL)
            return

        # Line 1: red "YT" tag on the left of the metadata area, views on the right.
        canvas.text(META_X, 1, "YT", (240, 40, 40), SMALL)
        views_str = _format_views(vid.views)
        views_w = canvas.text_width(views_str, SMALL)
        canvas.text(128 - views_w - 1, 1, views_str, (200, 200, 210), SMALL)

        # Line 2: channel name, truncated to fit the metadata area.
        # The metadata region is 71 px wide starting at META_X. text_width tells
        # us if we need to truncate manually — canvas.fit handles it.
        channel_str = canvas.fit(vid.channel, 128 - META_X)
        canvas.text(META_X, 11, channel_str, (150, 150, 160), SMALL)

        # Line 3: title scrolls if it overflows.
        # We use a manual scroll within the metadata bounds rather than
        # canvas.scroll_text (which uses full canvas width).
        _scroll_within(canvas, META_X, 128 - META_X, 22, vid.title, (240, 240, 245), tick)

    def _render_placeholder(self, canvas: Canvas) -> None:
        # Draw a subtle "loading" frame in the video area — a red YouTube-style
        # rounded rect with a play triangle.
        canvas.fill_rect(VIDEO_X + 8, 8, VIDEO_W - 16, 16, (80, 20, 24))
        # Play triangle in the middle
        cx, cy = VIDEO_X + VIDEO_W // 2, 16
        for dx in range(4):
            for dy in range(-3 + dx, 4 - dx):
                canvas.pixel(cx - 2 + dx, cy + dy, (240, 240, 240))


# --------------------------------------------------------------------- helpers


def _scroll_within(
    canvas: Canvas,
    x: int,
    width: int,
    y: int,
    text: str,
    color,
    tick: int,
) -> None:
    """Scroll `text` inside a bounded x..x+width strip on row y.

    Similar to Canvas.scroll_text but confined to a horizontal window instead
    of the full canvas width. We compute the text width and shift by tick.
    """
    from PIL import Image, ImageDraw
    text_w = canvas.text_width(text, SMALL)
    if text_w <= width:
        canvas.text(x, y, text, color, SMALL)
        return
    # Total scroll distance = text width + a small gap before wraparound.
    gap = 12
    total = text_w + gap
    offset = tick % total
    # Draw a temporary strip 2x the width and blit the visible portion.
    strip = Image.new("RGB", (total + width, 10), (0, 0, 0))
    draw = ImageDraw.Draw(strip)
    from ticker.canvas import load_font
    font = load_font(SMALL)
    draw.fontmode = "1"
    draw.text((0, 0), text, fill=color, font=font)
    # If the offset would show empty tail, wrap the beginning of the text on the right
    draw.text((total, 0), text, fill=color, font=font)
    visible = strip.crop((offset, 0, offset + width, 10))
    canvas.image(x, y, visible)


def _fetch_and_decode(video_id: str) -> np.ndarray:
    """Download `video_id` at low quality and return a (N, 32, VIDEO_W, 3) uint8 array.

    Uses yt-dlp to fetch a small stream URL, then ffmpeg (via imageio-ffmpeg's
    bundled binary) to pipe raw RGB frames at 12 fps, 57x32.
    """
    # Import here so a missing yt-dlp doesn't stop the ticker from booting.
    import yt_dlp
    import imageio_ffmpeg

    # Download to a temp file first, then decode from disk. Streaming directly
    # from the HTTPS URL segfaults ffmpeg on some builds, and buffering to
    # disk lets us cap length by yt-dlp's match_filter before ffmpeg runs.
    cache_file = CACHE_DIR / f"{video_id}.mp4"

    # Cleanup: keep only the 5 newest cache files. A trending run cycles through
    # ~15 videos, so we'll re-download after a full lap. This keeps disk usage
    # under ~60 MB for typical 144p mp4 sizes.
    try:
        cached = sorted(CACHE_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in cached[5:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass

    if not cache_file.exists():
        ydl_opts = {
            "format": "worst[ext=mp4]/worst",
            "outtmpl": str(cache_file),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Cap length: skip anything over 10 min (they blow up the frame array).
            "match_filter": lambda info, incomplete: (
                None if (info.get("duration") or 0) <= 600
                else "video is longer than 10 min, skipping"
            ),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        if not cache_file.exists():
            raise RuntimeError("download did not produce a file")

    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    # Decode straight to raw RGB frames at 32 rows tall, VIDEO_W wide, TARGET_FPS.
    cmd = [
        ffmpeg_bin,
        "-loglevel", "error",
        "-i", str(cache_file),
        "-vf", f"fps={TARGET_FPS},scale={VIDEO_W}:{VIDEO_H}",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    raw = proc.stdout
    frame_size = VIDEO_W * VIDEO_H * 3
    n_frames = len(raw) // frame_size
    if n_frames == 0:
        raise RuntimeError("ffmpeg produced no frames")
    arr = np.frombuffer(raw[:n_frames * frame_size], dtype=np.uint8)
    arr = arr.reshape(n_frames, VIDEO_H, VIDEO_W, 3)
    # Copy to a writeable array (frombuffer returns read-only).
    return arr.copy()
