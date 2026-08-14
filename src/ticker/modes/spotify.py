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
non-connected / non-playing state, a friendly placeholder is drawn instead.
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
SCROLL_PX_PER_TICK = 1


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
            self._draw_placeholder(canvas, "Set SPOTIFY_CLIENT_ID", tick)
            return
        if not self._auth.connected:
            self._draw_placeholder(canvas, "Connect Spotify in webapp", tick)
            return
        if snapshot is None or not snapshot.title:
            # Connected but /currently-playing returned 204, or first poll not yet complete.
            self._draw_placeholder(canvas, "Not playing", tick)
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
        # No art yet — draw a dim square so the layout does not look broken.
        draw = ImageDraw.Draw(canvas.image_buffer)
        draw.rectangle((0, 0, ART_SIZE - 1, ART_SIZE - 1), fill=(28, 28, 30))
        # A tiny Spotify-green dot as a hint that it is the Spotify mode.
        draw.ellipse((12, 12, 19, 19), fill=SPOTIFY_GREEN)

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
        offset = (tick * SCROLL_PX_PER_TICK) % period
        strip_w = period + TEXT_WIDTH + 20  # generous margin for the crop
        scratch = Canvas(strip_w, text_h + 4)
        scratch.text_bold(0, 0, text, color, MEDIUM, weight=1)
        scratch.text_bold(period, 0, text, color, MEDIUM, weight=1)
        piece = scratch.image_buffer.crop((offset, 0, offset + TEXT_WIDTH, text_h + 4))
        canvas.image_buffer.paste(piece, (TEXT_LEFT, y))

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
        """Idle screen: Spotify green dot on the left, message on the right.

        Kept intentionally quiet so the panel does not look like an error state
        when the user simply is not playing anything.
        """
        # Small green disc where the album art would be.
        draw = ImageDraw.Draw(canvas.image_buffer)
        cx, cy = 15, 15
        draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=SPOTIFY_GREEN)
        # Three Spotify "sound wave" arcs inside the disc.
        for radius in (3, 5, 7):
            draw.arc(
                (cx - radius, cy - radius - 1, cx + radius, cy + radius - 1),
                start=290,
                end=340,
                fill=(0, 0, 0),
            )

        # Message on the right, centred vertically. Scrolls if too long so a
        # long .env error message is still readable.
        text_h = 12
        y = (32 - text_h) // 2
        self._draw_line(canvas, message, y_top=y, zone_h=text_h, color=PLACEHOLDER_COLOR, tick=tick)
