# MIT License — Copyright (c) 2026 John Kuok
"""YouTube popular-videos mode — actually plays video (at 57x32 pixels).

The LED matrix is 128x32. YouTube videos are 16:9, so a downsampled frame at
32 tall is 57 wide. We render the video in the LEFT 57 columns and use the
remaining 71 columns for scrolling metadata (title, channel, view count).

Data source: YouTube removed the /feed/trending page in 2026 and every
community Piped instance is either dead or has shut down. So we use their
public global music chart playlist (Top 100) via yt-dlp's flat-extract mode.
No API key or middleman needed.

Pipeline:
  1. yt-dlp fetches the chart playlist (flat extract, ~1s).
  2. yt-dlp downloads the current video's lowest-quality stream to disk.
  3. imageio-ffmpeg extracts every Nth frame at 32 rows tall, 12 fps.
  4. Frames are kept in memory as a numpy array; render() blits the current
     one to the canvas each tick.
  5. When the video ends (or download fails), advance to the next video.

Everything network-touching happens on background threads guarded by a
single-flight flag, so the LED render loop never blocks and never spawns
a thundering herd of retries.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode

# YouTube's official global Top 100 music chart used to work, but nearly all
# entries there are Vevo/label music videos that return HTTP 403 (DRM). Switch
# to a channel-uploads playlist (Veritasium is the default — no DRM, universally
# interesting, uploads about weekly). Override via YOUTUBE_PLAYLIST env var
# with a full playlist URL if you want a different channel or a custom list.
#
# YouTube channel-uploads playlist IDs are always the channel ID with the
# leading "UC" replaced by "UU". Some popular choices:
#   Veritasium:  UUHnyfMqiRRG1u-2MsSQLbXA
#   Kurzgesagt:  UUsXVk37bltHxD1rDPwtNM8Q
#   MrBeast:     UUX6OQ3DkcsbYNE6H8uQQuVA
#   TED-Ed:      UUsooa4yRKGN_zEE8iknghZA
#   MKBHD:       UUBJycsmduvYEL83R_U4JriQ
DEFAULT_PLAYLIST_URL = (
    "https://www.youtube.com/playlist?list=UUHnyfMqiRRG1u-2MsSQLbXA"
)

# Frame geometry for the tiny video window.
VIDEO_W = 57       # 16:9 aspect fitted to 32 tall = 56.9, rounded up
VIDEO_H = 32
VIDEO_X = 0        # left-aligned on the matrix
META_X = VIDEO_W + 3   # small gap between video and text

# Playback pacing.
TARGET_FPS = 12    # LED matrix runs at 30 fps; 12 fps video = every ~2.5 ticks
CACHE_DIR = Path(tempfile.gettempdir()) / "ticker-yt-cache"

# Trending list refresh interval (1 hour is plenty for a music chart).
TRENDING_CACHE_SECONDS = 3600

# Backoff after a trending fetch fails, so we don't hammer network on every tick.
TRENDING_RETRY_AFTER_FAILURE = 60.0


def _safe_log(msg: str) -> None:
    """Print an ASCII-only version of the message.

    systemd's journal runs services under C locale by default, which means
    print() with non-ASCII chars raises UnicodeEncodeError. That would blow
    away the real error we were trying to log, so we replace any offending
    character with '?'. Any log line is better than none.
    """
    try:
        print(msg.encode("ascii", errors="replace").decode("ascii"), flush=True)
    except Exception:
        pass


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
        # Playlist URL: env var wins, then config attr, else default.
        import os
        self._playlist_url = (
            os.getenv("YOUTUBE_PLAYLIST")
            or getattr(config, "youtube_playlist", None)
            or DEFAULT_PLAYLIST_URL
        )

        # Trending list state.
        self.videos: list[VideoInfo] = []
        # Deferred first fetch: don't hammer network in __init__ (that runs on
        # every mode switch). Set to 0 so the FIRST render tick triggers a fetch.
        self._next_trending_fetch_at = 0.0
        self._trending_in_flight = False

        # Current playback state.
        self._current_index = 0
        self._frames: np.ndarray | None = None  # shape (N, 32, VIDEO_W, 3), uint8
        self._current_video: VideoInfo | None = None
        self._playback_started = 0.0

        # Download worker: single-flight, target id = "this download is for this video".
        self._download_in_flight = False
        self._download_target: str | None = None

    # ------------------------------------------------------------------ trending

    def _maybe_refresh_trending(self) -> None:
        """Kick off a trending fetch if it's time and none is running."""
        now = time.monotonic()
        if self._trending_in_flight:
            return
        if now < self._next_trending_fetch_at:
            return
        self._trending_in_flight = True

        def _work():
            try:
                import yt_dlp
                opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "extract_flat": True,
                    "playlistend": 20,
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(self._playlist_url, download=False)
                entries = [e for e in (info.get("entries") or []) if e]
                vids = []
                for e in entries[:20]:
                    vid = e.get("id") or ""
                    title = str(e.get("title") or "").strip()
                    channel = str(e.get("uploader") or e.get("channel") or "").strip()
                    views = int(e.get("view_count") or 0)
                    if vid and title:
                        vids.append(VideoInfo(vid, title, channel, views))
                if vids:
                    self.videos = vids
                    self._next_trending_fetch_at = time.monotonic() + TRENDING_CACHE_SECONDS
                    _safe_log(f"[youtube] fetched {len(vids)} videos from playlist")
                else:
                    self._next_trending_fetch_at = time.monotonic() + TRENDING_RETRY_AFTER_FAILURE
                    _safe_log("[youtube] playlist returned zero videos")
            except Exception as e:
                self._next_trending_fetch_at = time.monotonic() + TRENDING_RETRY_AFTER_FAILURE
                _safe_log(f"[youtube] trending fetch failed: {type(e).__name__}: {e}")
            finally:
                self._trending_in_flight = False

        threading.Thread(target=_work, daemon=True, name="yt-trending").start()

    # ------------------------------------------------------------------ download

    def _maybe_start_download(self) -> None:
        """Kick off a video download if we have a list, no video is loaded,
        and no download is currently running."""
        if self._download_in_flight:
            return
        if self._frames is not None:
            return
        if not self.videos:
            return
        video = self.videos[self._current_index]
        if self._download_target == video.id:
            # A recent download for this video already finished (or failed) —
            # don't retry the same one immediately, advance to the next instead.
            return

        self._download_in_flight = True
        self._download_target = video.id

        def _work():
            try:
                _safe_log(f"[youtube] downloading {video.id}: {video.title[:50]}")
                frames = _fetch_and_decode(video.id)
                self._frames = frames
                self._current_video = video
                self._playback_started = time.monotonic()
                _safe_log(f"[youtube] ready: {video.id} - {len(frames)} frames")
            except Exception as e:
                _safe_log(f"[youtube] download failed for {video.id}: {type(e).__name__}: {e}")
                # Advance to the next video on failure so we don't get stuck.
                if self.videos:
                    self._current_index = (self._current_index + 1) % len(self.videos)
            finally:
                self._download_in_flight = False

        threading.Thread(target=_work, daemon=True, name="yt-decode").start()

    def _advance(self) -> None:
        """Move to the next video and clear frames so the download kicks off."""
        if not self.videos:
            return
        self._current_index = (self._current_index + 1) % len(self.videos)
        self._frames = None
        self._download_target = None   # allow the new video to download

    # ------------------------------------------------------------------ render

    def render(self, canvas: Canvas, tick: int) -> None:
        # Non-blocking: kick off list refresh + video download if it's time and
        # nothing else is in flight. All actual work happens on threads.
        self._maybe_refresh_trending()
        self._maybe_start_download()

        canvas.clear()

        # Metadata panel is always the same layout.
        self._render_metadata(canvas, tick)

        # Video panel: current frame if we have one, otherwise placeholder.
        if self._frames is not None and len(self._frames) > 0:
            frame_idx = int((time.monotonic() - self._playback_started) * TARGET_FPS)
            if frame_idx >= len(self._frames):
                # Video ended — advance to the next.
                self._advance()
                self._render_placeholder(canvas)
                return
            frame = self._frames[frame_idx]
            self._blit_frame(canvas, frame)
        else:
            self._render_placeholder(canvas)

    def _blit_frame(self, canvas: Canvas, frame: np.ndarray) -> None:
        """Copy one (32, VIDEO_W, 3) RGB frame into the left of the canvas."""
        from PIL import Image
        img = Image.fromarray(frame, mode="RGB")
        canvas.image(VIDEO_X, 0, img)

    def _render_metadata(self, canvas: Canvas, tick: int) -> None:
        # Pick the "showing right now" video: what's actually playing, or the
        # next-up if nothing has started yet.
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
        channel_str = canvas.fit(vid.channel, 128 - META_X)
        canvas.text(META_X, 11, channel_str, (150, 150, 160), SMALL)

        # Line 3: title scrolls if it overflows the metadata strip.
        _scroll_within(canvas, META_X, 128 - META_X, 22, vid.title, (240, 240, 245), tick)

    def _render_placeholder(self, canvas: Canvas) -> None:
        # Red YouTube-style rounded rect with a play triangle centered on the
        # video area.
        canvas.fill_rect(VIDEO_X + 8, 8, VIDEO_W - 16, 16, (80, 20, 24))
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
    """Scroll `text` inside a bounded x..x+width strip on row y."""
    from PIL import Image, ImageDraw
    text_w = canvas.text_width(text, SMALL)
    if text_w <= width:
        canvas.text(x, y, text, color, SMALL)
        return
    gap = 12
    total = text_w + gap
    offset = tick % total
    strip = Image.new("RGB", (total + width, 10), (0, 0, 0))
    draw = ImageDraw.Draw(strip)
    from ticker.canvas import load_font
    font = load_font(SMALL)
    draw.fontmode = "1"
    draw.text((0, 0), text, fill=color, font=font)
    draw.text((total, 0), text, fill=color, font=font)
    visible = strip.crop((offset, 0, offset + width, 10))
    canvas.image(x, y, visible)


def _fetch_and_decode(video_id: str) -> np.ndarray:
    """Download `video_id` at low quality and return a (N, 32, VIDEO_W, 3) uint8 array."""
    import yt_dlp
    import imageio_ffmpeg

    # Cache cleanup: keep only the 5 newest files.
    try:
        cached = sorted(CACHE_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in cached[5:]:
            if old.is_file():
                old.unlink(missing_ok=True)
    except Exception:
        pass

    existing = list(CACHE_DIR.glob(f"{video_id}.*"))
    if existing:
        cache_file = existing[0]
    else:
        # Let yt-dlp pick the extension via %(ext)s. "worst" (no ext filter)
        # because YouTube's SABR streaming can drop mp4 formats.
        outtmpl = str(CACHE_DIR / f"{video_id}.%(ext)s")
        ydl_opts = {
            "format": "worst",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "match_filter": lambda info, incomplete: (
                None if (info.get("duration") or 0) <= 600
                else "video is longer than 10 min, skipping"
            ),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        found = list(CACHE_DIR.glob(f"{video_id}.*"))
        if not found:
            raise RuntimeError("download did not produce a file")
        cache_file = found[0]

    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
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
    return arr.copy()
