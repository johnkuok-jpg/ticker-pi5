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

import json
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode

# Curated playlists that survive downscaling to 57×32. These are all
# channel-uploads playlists (a channel ID with the leading "UC" replaced by
# "UU") because those don't require auth and are stable over time. Content
# picked to look good at very low resolution — slow scenic footage, big
# recognisable subjects, wide colour, minimal fine detail or text.
#
# The category key is what the webapp posts back and what lives on disk.
CATEGORIES: dict[str, dict[str, str]] = {
    "nature":     {"label": "Nature",     "desc": "Nature Relaxation Films", "list": "UU4lp9Emg1ci8eo2eDkB-Tag"},
    "bbc_earth":  {"label": "BBC Earth",  "desc": "Wildlife, big landscapes",  "list": "UUwmZiChSryoWQCZMIQezgTg"},
    "aquarium":   {"label": "Aquarium",   "desc": "Monterey Bay Aquarium",     "list": "UUnM5iMGiKsZg-iOlIO2ZkdQ"},
    "animals":    {"label": "Animals",    "desc": "Animal Planet",             "list": "UUkEBDbzLyH-LbB2FgMoSMaQ"},
    "space":      {"label": "Space",      "desc": "NASA",                      "list": "UULA_DiR1FfKNvjuUpBHmylQ"},
    "jpl":        {"label": "JPL",        "desc": "NASA Jet Propulsion Lab",   "list": "UUryGec9PdUCLjpJW2mgCuLw"},
    "earth":      {"label": "Earth",      "desc": "Aerials from above",        "list": "UUU1wj4omlek5tQ3GDJNZuWQ"},
    "gopro":      {"label": "GoPro",      "desc": "Action + landscapes",       "list": "UUqhnX4jA0A5paNd1v-zEysw"},
    "aviation":   {"label": "Aviation",   "desc": "Cargospotter: 747s, A380s",  "list": "UUA6aJAT9rH8vRwWGiGr2iqQ"},
    "ambient":    {"label": "Ambient",    "desc": "4K screensavers",           "list": "UUg72Hd6UZAgPBAUZplnmPMQ"},
    "lofi":       {"label": "Lofi",       "desc": "Lofi Girl music videos",    "list": "UUSJ4gkVC6NrvII8umztf0Ow"},
    "veritasium": {"label": "Science",    "desc": "Veritasium",                "list": "UUHnyfMqiRRG1u-2MsSQLbXA"},
}
DEFAULT_CATEGORY = "nature"


def resolve_playlist(value: str) -> str:
    """Turn a stored value into a full playlist URL.

    ``value`` may be a category key (e.g. ``"nature"``), a full URL (starts
    with ``http``), or empty. Unknown keys and blanks fall back to the
    default category so the mode is never stuck without a source.
    """
    v = (value or "").strip()
    if v.startswith("http"):
        return v
    cat = CATEGORIES.get(v) or CATEGORIES[DEFAULT_CATEGORY]
    return f"https://www.youtube.com/playlist?list={cat['list']}"


DEFAULT_PLAYLIST_URL = resolve_playlist(DEFAULT_CATEGORY)

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

# How long a repeatedly-failing video stays on the blocklist before we try it
# again. Long enough that we don't hammer the same 403 within a session, short
# enough that a temporary geo-restriction / age-gate / DRM tick unblocks itself
# after a few hours instead of poisoning the queue permanently.
BLOCKLIST_TTL_SECONDS = 6 * 3600  # 6 hours


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


def _is_playable_entry(entry: dict) -> bool:
    """Return False for livestreams, upcoming premieres, and unplayable items.

    yt-dlp's flat playlist extraction returns a ``live_status`` string on
    each entry that tells us whether it's a real VOD. Anything that isn't
    ``was_live`` (a livestream that ended and is now watchable) or ``not_live``
    is either currently airing, scheduled for the future, a members-only
    stream, or otherwise not something we can decode to frames right now.

    Duration is a second signal: a video with a known duration is a real VOD;
    a duration of None on a flat entry usually means it's live or upcoming.
    We take the union of both signals so that if yt-dlp changes its field
    reporting we still filter the obvious cases out.
    """
    status = str(entry.get("live_status") or "").lower()
    if status in {"is_live", "is_upcoming", "post_live"}:
        return False
    if entry.get("availability") in {"needs_auth", "subscriber_only", "premium_only"}:
        return False
    return True


SHORT_MAX_DURATION_SECONDS = 90


def _is_short(entry: dict) -> bool:
    """Return True for YouTube Shorts (vertical clips).

    Detection is duration-based because the flat-extract entry doesn't
    contain the signals we'd prefer:

    - The URL for Shorts is often ``/watch?v=...`` in flat mode, not
      ``/shorts/...`` -- observed live on GoPro's channel feed.
    - ``ie_key`` is always ``Youtube`` regardless of format.
    - ``width`` / ``height`` / ``aspect_ratio`` are ``None`` in flat mode.

    That leaves ``duration`` as the only reliable flat-mode signal. Real
    Shorts cap at 60s but we allow a small margin -- some channels' Shorts
    report 61-88s after re-encoding. Above 90s virtually every YouTube video
    is landscape.

    The tradeoff: we'll occasionally drop a legitimate ultra-short landscape
    clip (a JPL teaser, a BBC Earth cutdown). That's acceptable for a video
    ticker where the alternative is showing a vertical letterbox sliver on
    the 57x32 panel. Channels that publish real landscape VODs (Nature
    Relaxation Films, BBC Earth, NASA JPL) upload minute-plus content
    routinely, so filling the 20-slot queue is not a problem in practice.

    The ``/shorts/`` URL check stays as a belt-and-braces short-circuit for
    any yt-dlp version that does surface it.
    """
    for key in ("webpage_url", "url", "original_url"):
        val = entry.get(key)
        if isinstance(val, str) and "/shorts/" in val.lower():
            return True
    duration = entry.get("duration")
    if isinstance(duration, (int, float)) and 0 < duration <= SHORT_MAX_DURATION_SECONDS:
        return True
    return False


class YouTubeMode(Mode):
    """Play tiny 57x32 YouTube videos with scrolling metadata."""

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Playlist source resolution, in priority order:
        #   1. YOUTUBE_PLAYLIST env var (for the power-user override)
        #   2. The webapp-selected value on disk (category key OR URL)
        #   3. The compiled-in default (nature)
        import os
        self._config = config
        self._playlist_state_file = config.youtube_playlist_file
        env = os.getenv("YOUTUBE_PLAYLIST", "").strip()
        self._env_override = env if env else None
        self._current_playlist_selection: str = config.current_youtube_playlist()
        self._playlist_url = self._env_override or resolve_playlist(
            self._current_playlist_selection
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

        # Webapp-driven skip. The webapp bumps a counter on disk on each
        # "next" tap; when we observe a higher value than we last saw we
        # advance. Seed with the current value so we don't skip the first
        # video every time the mode is entered.
        self._skip_counter_file = config.youtube_skip_file
        self._last_skip_seen: int = self._read_skip_counter()

        # Bad-video blocklist: {video_id: expiry_monotonic}. Persisted to
        # disk so the ticker survives restarts without re-attempting known-bad
        # IDs, which is where the 403 loops from earlier came from.
        self._blocklist_file: Path = config.state_dir / "youtube_blocklist.json"
        self._blocklist: dict[str, float] = self._load_blocklist()

    def _maybe_reload_playlist_selection(self) -> None:
        """Pick up webapp changes to the selected category without a restart.

        The env-var override always wins; it's expected to be permanent for a
        given deployment. If it isn't set, we live-poll the state file so
        picking a new category on the phone takes effect on the next tick.
        """
        if self._env_override:
            return
        try:
            selection = self._playlist_state_file.read_text(encoding="utf-8").strip()
        except OSError:
            selection = ""
        if selection == self._current_playlist_selection:
            return
        self._current_playlist_selection = selection
        self._playlist_url = resolve_playlist(selection)
        # Force a fresh trending fetch on the new source, and drop any queued
        # download for the old playlist so the switch feels immediate.
        self._next_trending_fetch_at = 0.0
        self.videos = []
        self._frames = None
        self._current_video = None
        self._download_target = None
        _safe_log(f"[youtube] switched to playlist: {self._playlist_url}")

    def _load_blocklist(self) -> dict[str, float]:
        """Load the persisted blocklist and drop expired entries.

        Stored on disk as ``{video_id: unix_expiry}`` so it survives across
        service restarts (which is when the same 403 loop was most annoying).
        We compare against wall-clock time on disk but track expiry using
        ``time.monotonic()`` in memory to stay robust against clock jumps.
        """
        try:
            data = json.loads(self._blocklist_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        now_wall = time.time()
        now_mono = time.monotonic()
        out: dict[str, float] = {}
        for vid, expiry_wall in data.items():
            try:
                remaining = float(expiry_wall) - now_wall
            except (TypeError, ValueError):
                continue
            if remaining > 0:
                out[vid] = now_mono + remaining
        return out

    def _save_blocklist(self) -> None:
        """Persist the blocklist as wall-clock expiries."""
        now_wall = time.time()
        now_mono = time.monotonic()
        data = {
            vid: now_wall + (expiry_mono - now_mono)
            for vid, expiry_mono in self._blocklist.items()
            if expiry_mono > now_mono
        }
        try:
            self._blocklist_file.parent.mkdir(parents=True, exist_ok=True)
            self._blocklist_file.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass  # best-effort; a lost blocklist just means we retry sooner

    def _is_blocklisted(self, video_id: str) -> bool:
        expiry = self._blocklist.get(video_id)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            self._blocklist.pop(video_id, None)
            return False
        return True

    def _blocklist_video(self, video_id: str) -> None:
        self._blocklist[video_id] = time.monotonic() + BLOCKLIST_TTL_SECONDS
        self._save_blocklist()

    def _prune_blocklist(self) -> None:
        now = time.monotonic()
        expired = [vid for vid, exp in self._blocklist.items() if exp <= now]
        for vid in expired:
            self._blocklist.pop(vid, None)
        if expired:
            self._save_blocklist()

    def _read_skip_counter(self) -> int:
        try:
            return int(self._skip_counter_file.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

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
                # Prune expired blocklist entries once per fetch cycle so a
                # long-running session doesn't accumulate stale IDs.
                self._prune_blocklist()
                vids = []
                skipped_live = 0
                skipped_blocked = 0
                skipped_short = 0
                for e in entries[:40]:  # widened from 20 to survive filtering
                    vid = e.get("id") or ""
                    title = str(e.get("title") or "").strip()
                    channel = str(e.get("uploader") or e.get("channel") or "").strip()
                    views = int(e.get("view_count") or 0)
                    if not (vid and title):
                        continue
                    if not _is_playable_entry(e):
                        skipped_live += 1
                        continue
                    if _is_short(e):
                        # Shorts are vertical and letterbox to a useless sliver
                        # on the 57x32 video panel. Drop them entirely.
                        skipped_short += 1
                        continue
                    if self._is_blocklisted(vid):
                        skipped_blocked += 1
                        continue
                    vids.append(VideoInfo(vid, title, channel, views))
                    if len(vids) >= 20:
                        break
                if skipped_live or skipped_blocked or skipped_short:
                    _safe_log(
                        f"[youtube] filtered {skipped_live} livestreams, "
                        f"{skipped_short} shorts, "
                        f"{skipped_blocked} blocklisted from playlist"
                    )
                if vids:
                    self.videos = vids
                    self._next_trending_fetch_at = time.monotonic() + TRENDING_CACHE_SECONDS
                    _safe_log(f"[youtube] fetched {len(vids)} videos from playlist")
                else:
                    self._next_trending_fetch_at = time.monotonic() + TRENDING_RETRY_AFTER_FAILURE
                    _safe_log("[youtube] playlist returned zero videos")
            except Exception as e:
                import traceback
                self._next_trending_fetch_at = time.monotonic() + TRENDING_RETRY_AFTER_FAILURE
                _safe_log(f"[youtube] trending fetch failed: {type(e).__name__}: {e}")
                for line in traceback.format_exc().splitlines():
                    _safe_log(f"[youtube]   {line}")
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
                # Log the full traceback so latin-1 header crashes (which come
                # from urllib deep inside yt-dlp) can actually be traced back
                # to their origin instead of just showing the leaf message.
                import traceback
                _safe_log(f"[youtube] download failed for {video.id}: {type(e).__name__}: {e}")
                for line in traceback.format_exc().splitlines():
                    _safe_log(f"[youtube]   {line}")
                # Blocklist the video for a few hours so we don't retry the
                # exact same 403 / premiere / missing-format each cycle. It
                # will fall out of the blocklist automatically at the next
                # trending refresh after BLOCKLIST_TTL_SECONDS.
                self._blocklist_video(video.id)
                # Also drop it from the current in-memory list so we don't
                # immediately re-attempt it before the next trending refresh.
                self.videos = [v for v in self.videos if v.id != video.id]
                if self.videos:
                    self._current_index %= len(self.videos)
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
        # Cheap file poll for webapp-selected category changes before anything
        # else — must happen before the trending fetch so the fetch targets
        # the newly selected playlist.
        self._maybe_reload_playlist_selection()

        # Non-blocking: kick off list refresh + video download if it's time and
        # nothing else is in flight. All actual work happens on threads.
        self._maybe_refresh_trending()

        # Webapp "next" button: cheap file poll, once per frame. Any bump
        # advances one video, no matter how many taps landed between frames
        # (rapid double-tap == single skip; deliberate).
        skip_now = self._read_skip_counter()
        if skip_now > self._last_skip_seen:
            self._last_skip_seen = skip_now
            self._advance()

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
    from ticker.canvas import load_font, sanitize
    # YouTube titles routinely include curly quotes, en-dashes, and emoji
    # ("Oregon's", "POV \u2013 Kayaking", etc.). The bundled Spleen bitmap font
    # is Latin-1 only, so drawing raw Unicode with PIL's ImageDraw raises
    # UnicodeEncodeError -- which surfaces on the LED matrix as "youtube error:
    # 'latin-1' codec can't en". canvas.text() calls sanitize() for us, but
    # this scroll branch draws straight to a PIL Draw and would bypass it.
    text = sanitize(text)
    text_w = canvas.text_width(text, SMALL)
    if text_w <= width:
        canvas.text(x, y, text, color, SMALL)
        return
    gap = 12
    total = text_w + gap
    offset = tick % total
    strip = Image.new("RGB", (total + width, 10), (0, 0, 0))
    draw = ImageDraw.Draw(strip)
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
        # Format selection: prefer a small progressive MP4 (single file, no
        # DASH merging), then fall back progressively. YouTube's SABR streaming
        # sometimes drops the classic `worst` format entirely, so we ladder:
        #   1. worst mp4 progressive (single file, decodes cleanly)
        #   2. worst progressive of any container
        #   3. absolute worst, allowing merged DASH audio+video
        # The final `/worst` catch-all keeps us alive when a video has no
        # progressive stream at all.
        outtmpl = str(CACHE_DIR / f"{video_id}.%(ext)s")
        ydl_opts = {
            "format": (
                "worst[ext=mp4][protocol!=m3u8]/"
                "worst[protocol!=m3u8]/"
                "worstvideo+worstaudio/worst"
            ),
            # `merge_output_format` kicks in only when the DASH-merge branch
            # of the ladder is taken; the two progressive branches produce a
            # single file and skip the merge entirely.
            "merge_output_format": "mp4",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Length cap: 60 minutes. At 12 fps decoded into a (N, 32, 57, 3)
            # uint8 array, 60 min lands at ~240 MB in RAM which is fine on the
            # Pi 5's 8 GB; above that the download time and cache pressure get
            # unfun. Cargospotter's aviation content is largely 60-minute
            # compilations so this cap is picked to fit it.
            "match_filter": lambda info, incomplete: (
                None if (info.get("duration") or 0) <= 3600
                else "video is longer than 60 min, skipping"
            ),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        found = list(CACHE_DIR.glob(f"{video_id}.*"))
        if not found:
            raise RuntimeError("download did not produce a file")
        cache_file = found[0]

    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    # Hard-cap the decoded portion of any video, regardless of source length.
    # This bounds:
    #   - ffmpeg CPU time (a 60-min Cargospotter comp would otherwise take
    #     several minutes to decode on a Pi 5 and blow past subprocess.run's
    #     old 120s timeout, causing the mode to blocklist and skip the video)
    #   - the raw pipe/RAM footprint (57*32*3 * TARGET_FPS * seconds)
    # A 5-minute cap is plenty: at 5 min * 60 s * TARGET_FPS = 3600 frames the
    # ticker plays for 5 minutes before advancing, and Cargospotter's opening
    # 5 minutes already contain multiple full landings/departures.
    max_decode_seconds = 5 * 60
    cmd = [
        ffmpeg_bin,
        "-loglevel", "error",
        # -t must precede -i to be honoured as an *input* time limit, so ffmpeg
        # stops reading the source instead of decoding the whole file. This is
        # what actually caps CPU time, not an output-side cap.
        "-t", str(max_decode_seconds),
        "-i", str(cache_file),
        # Keep source aspect ratio: scale to fit inside VIDEO_W x VIDEO_H, then
        # pad the remaining edges with black. Plain `scale=W:H` would stretch
        # a 4:3 or vertical clip out to the panel and look squashed. The
        # `decrease` flag means the scaled frame never exceeds the box on
        # either axis; pad centres it. Output stays exactly VIDEO_W x VIDEO_H
        # so the (N, H, W, 3) reshape below still works.
        "-vf",
        (
            f"fps={TARGET_FPS},"
            f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black"
        ),
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    # Timeout is a safety net well above the expected decode time. On a Pi 5
    # decoding 144p to 57x32 at 12 fps runs faster than realtime, so 5 minutes
    # of source takes well under 5 minutes to decode -- but a slow SD card or
    # a weirdly-encoded file could push past that, and we'd rather wait than
    # blocklist a legit video for missing a tight bound.
    proc = subprocess.run(cmd, capture_output=True, check=True, timeout=600)
    raw = proc.stdout
    frame_size = VIDEO_W * VIDEO_H * 3
    n_frames = len(raw) // frame_size
    if n_frames == 0:
        raise RuntimeError("ffmpeg produced no frames")
    arr = np.frombuffer(raw[:n_frames * frame_size], dtype=np.uint8)
    arr = arr.reshape(n_frames, VIDEO_H, VIDEO_W, 3)
    return arr.copy()
