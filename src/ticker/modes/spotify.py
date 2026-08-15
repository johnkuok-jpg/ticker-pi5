# MIT License — Copyright (c) 2026 John Kuok
"""Spotify 'now playing' mode.

Fixed 128x32 layout, locked after mocks in the design pass:

    +--- 32x32 art ---+ | +---- 95px text zone --------+
    |                 | | |  title  (rows 0..14)        |
    |                 | | |         (row  15 = gap)     |
    |    album cover  | | |  artist (rows 16..29)       |
    |                 | | |         (row  30 = gap)     |
    +-----------------+ | +---- row 31: progress bar ---+
    cols 0..31            cols 33..127

The album art is a full 32x32 square with no crop; the 1px progress bar sits
under the text only so the artwork is never overpainted.

Both text lines are drawn bold with Spleen MEDIUM (6x12). When the rendered
text is wider than the 95px text zone, it scrolls left-to-right at one pixel
per frame with a 20px gap between the tail and the repeat, matching the
nametag mode's scroll aesthetic.

The mode never blocks: :class:`ticker.spotify.SpotifyClient` polls Spotify on
a background thread and this renderer only reads the cached snapshot. On any
non-connected / non-playing state, a terse placeholder is drawn instead.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from ticker import spotify as spotify_client
from ticker.canvas import Canvas, MEDIUM
from ticker.config import Config
from ticker.modes.base import Mode

# --- Layout constants (see module docstring) ------------------------------

ART_SIZE = 32
GUTTER_X = 32   # 1-pixel gap between art and text (art occupies 0..31)
TEXT_LEFT = ART_SIZE + 1        # 33
TEXT_WIDTH = 128 - TEXT_LEFT    # 95

TITLE_TOP = 0
TITLE_HEIGHT = 15
GAP_ROW = 15
ARTIST_TOP = 16
ARTIST_HEIGHT = 14
BAR_ROW = 31

# --- Colours ---------------------------------------------------------------

# Spotify green. Kept as the branded colour on the progress bar; text stays
# neutral so the panel does not turn into a wall of green.
SPOTIFY_GREEN = (30, 215, 96)
BAR_BG = (40, 40, 40)
TITLE_COLOR = (255, 255, 255)
ARTIST_COLOR = (180, 180, 180)
# When paused, the progress bar dims to gray so 'playing' vs 'paused' is
# visible at a glance without adding a pause icon that would eat pixels.
PAUSED_FG = (110, 110, 110)
PLACEHOLDER_COLOR = (140, 140, 140)

# --- Scroll behaviour ------------------------------------------------------

SCROLL_GAP_PX = 20
# Pixels of travel per tick. Fractional on purpose: whole-pixel steps at this
# speed look like a stutter because the text sits still and then jumps. See
# _draw_line, which blends adjacent pixel offsets to render the fraction.
SCROLL_PX_PER_TICK = 0.5
# Ticks to hold still at the start of each loop. Motion that never stops is
# tiring to read; a short pause gives the eye somewhere to latch on before the
# line starts moving, and marks where the text begins.
SCROLL_DWELL_TICKS = 45


# --- Spotify wordmark ------------------------------------------------------
#
# A small, unmistakable Spotify glyph used wherever album art is missing
# (idle placeholder, unconfigured client, connect prompt, and no-art frames).
# We used to draw arcs with PIL's ``ImageDraw.arc``, but at this pixel scale
# (a 17×17 disc) PIL's anti-aliased arc stubs read as random speckles — the
# arcs came out as a couple of gray marks in the top-right rather than the
# three parallel sound-wave arcs that give the wordmark its silhouette.
#
# Instead we plot the arcs pixel-by-pixel as three ``dome-down`` curves:
# each arc's middle sits a row higher than its ends, so it curves gently
# downward on both sides, mirroring the real logo.


def _draw_spotify_mark(image: Image.Image, cx: int, cy: int) -> None:
    """Draw the Spotify green-disc-with-three-arcs mark centred on (cx, cy).

    Sized for a 32×32 slot: a 17-pixel disc with three arcs spanning the
    middle 12 pixels. Safe to call on any RGB image — all drawing is inside
    a bounded box around the centre.
    """
    draw = ImageDraw.Draw(image)
    # Green disc. Radius 8 gives a 17-pixel-wide disc, which is the largest
    # disc that still leaves a one-pixel margin inside a 32×32 art slot.
    draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=SPOTIFY_GREEN)

    # Three arcs: top / middle / bottom, each a wide-shallow ``dome-down``
    # curve. ``half_width`` shrinks toward the bottom so the arcs stay inside
    # the disc as it narrows below the equator.
    for y_middle, half_width in ((cy - 5, 6), (cy - 1, 5), (cy + 3, 4)):
        for dx in range(-half_width, half_width + 1):
            # Ends of each arc drop by one pixel; middle stays put. This is
            # cheaper than a full parabola and reads the same at 1x.
            drop = 1 if abs(dx) >= half_width - 1 else 0
            draw.point((cx + dx, y_middle + drop), fill=(0, 0, 0))


class SpotifyMode(Mode):
    """Renders the current Spotify track (or a placeholder when idle)."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        auth = spotify_client.SpotifyAuth(
            client_id=config.spotify_client_id,
            client_secret=config.spotify_client_secret,
            redirect_uri=config.spotify_redirect_uri,
            token_file=config.spotify_token_file,
        )
        # One client per Mode instance. The renderer builds a Mode once and
        # reuses it across frames, so the underlying HTTP session and art
        # cache live for the process's lifetime.
        self._client = spotify_client.SpotifyClient(auth)
        self._auth = auth

    # Public accessors used by the webapp (via _renderer_status equivalents)
    # so 'connected' shows in the UI without the webapp having to know the
    # token-file format.
    @property
    def auth(self) -> spotify_client.SpotifyAuth:
        return self._auth

    @property
    def client(self) -> spotify_client.SpotifyClient:
        return self._client

    def render(self, canvas: Canvas, tick: int) -> None:
        snapshot = self._client.snapshot()

        if not self._auth.configured:
            self._draw_placeholder(canvas, "SET SPOTIFY CLIENT ID", tick)
            return
        if not self._auth.connected:
            self._draw_placeholder(canvas, "CONNECT IN SETTINGS", tick)
            return
        if snapshot is None or not snapshot.title:
            # Connected but /currently-playing returned 204, or first poll not yet complete.
            self._draw_placeholder(canvas, "NOT PLAYING", tick)
            return

        self._draw_art(canvas, snapshot.album_art)
        self._draw_line(canvas, snapshot.title, TITLE_TOP, TITLE_HEIGHT, TITLE_COLOR, tick)
        self._draw_line(canvas, snapshot.artist, ARTIST_TOP, ARTIST_HEIGHT, ARTIST_COLOR, tick)
        self._draw_progress(canvas, snapshot)

    # --- drawing helpers ---------------------------------------------------

    def _draw_art(self, canvas: Canvas, art: Image.Image | None) -> None:
        """Blit the 32x32 album cover, or a subtle placeholder square."""
        if art is not None:
            canvas.image_buffer.paste(art, (0, 0))
            return
        # No art yet — dim square background, and the Spotify wordmark disc on top.
        draw = ImageDraw.Draw(canvas.image_buffer)
        draw.rectangle((0, 0, ART_SIZE - 1, ART_SIZE - 1), fill=(28, 28, 30))
        _draw_spotify_mark(canvas.image_buffer, cx=15, cy=15)

    def _draw_line(
        self,
        canvas: Canvas,
        text: str,
        y_top: int,
        zone_h: int,
        color: tuple[int, int, int],
        tick: int,
    ) -> None:
        """Draw one text line, scrolling if it overflows the text zone.

        Spleen MEDIUM is 6x12; centred vertically in a 14- or 15-pixel zone
        that leaves a 1-2 pixel breathing gap top/bottom.
        """
        if not text:
            return
        text_h = 12  # Spleen MEDIUM
        y = y_top + max(0, (zone_h - text_h) // 2)
        text_w = canvas.text_bold_width(text, MEDIUM, weight=1)

        if text_w <= TEXT_WIDTH:
            canvas.text_bold(TEXT_LEFT, y, text, color, MEDIUM, weight=1)
            return

        # Scroll: render the whole string plus one repeat into a scratch strip,
        # then crop a text-zone-wide slice at the current offset.
        period = text_w + SCROLL_GAP_PX

        # Where are we in the dwell-then-scroll cycle?
        scroll_ticks = int(period / SCROLL_PX_PER_TICK)
        cycle_ticks = scroll_ticks + SCROLL_DWELL_TICKS
        phase = tick % cycle_ticks
        offset_f = 0.0 if phase < SCROLL_DWELL_TICKS else (phase - SCROLL_DWELL_TICKS) * SCROLL_PX_PER_TICK

        strip_w = period + TEXT_WIDTH + 24  # margin so the +1 crop never runs off the end
        scratch = Canvas(strip_w, text_h + 4)
        scratch.text_bold(0, 0, text, color, MEDIUM, weight=1)
        scratch.text_bold(period, 0, text, color, MEDIUM, weight=1)

        # Sub-pixel step: crop at both neighbouring whole-pixel offsets and
        # cross-fade by the fractional part. The panel's pixels stay on a grid,
        # but weighting the two positions makes the *apparent* position land
        # between them, which reads as smooth motion instead of a 1px jump.
        base = int(offset_f)
        frac = offset_f - base
        near = scratch.image_buffer.crop((base, 0, base + TEXT_WIDTH, text_h + 4))
        if frac > 0.0:
            far = scratch.image_buffer.crop((base + 1, 0, base + 1 + TEXT_WIDTH, text_h + 4))
            near = Image.blend(near, far, frac)
        canvas.image_buffer.paste(near, (TEXT_LEFT, y))

    def _draw_progress(self, canvas: Canvas, snapshot: spotify_client.NowPlaying) -> None:
        """Draw the 1-pixel progress bar under the text zone (cols 33..127)."""
        draw = ImageDraw.Draw(canvas.image_buffer)
        # Background rail
        for x in range(TEXT_LEFT, 128):
            draw.point((x, BAR_ROW), fill=BAR_BG)
        if snapshot.duration_ms <= 0:
            return
        fraction = max(0.0, min(1.0, snapshot.progress_ms / snapshot.duration_ms))
        fill_end = TEXT_LEFT + int(fraction * TEXT_WIDTH)
        color = SPOTIFY_GREEN if snapshot.is_playing else PAUSED_FG
        for x in range(TEXT_LEFT, fill_end):
            draw.point((x, BAR_ROW), fill=color)

    def _draw_placeholder(self, canvas: Canvas, message: str, tick: int) -> None:
        """Idle screen: Spotify mark on the left, message on the right.

        Kept intentionally quiet so the panel does not look like an error state
        when the user simply is not playing anything.
        """
        _draw_spotify_mark(canvas.image_buffer, cx=15, cy=15)

        # Message on the right, centred vertically. Scrolls if too long so a
        # long .env error message is still readable.
        text_h = 12
        y = (32 - text_h) // 2
        self._draw_line(canvas, message, y_top=y, zone_h=text_h, color=PLACEHOLDER_COLOR, tick=tick)
