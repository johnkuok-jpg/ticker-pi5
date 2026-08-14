# MIT License — Copyright (c) 2026 John Kuok
"""Who's That Pokémon — a passive silhouette/reveal slideshow.

A random Kanto Pokémon appears on the left half of the panel, first as a black
silhouette over the panel-native black, then dissolves into its full colored
sprite. The right half of the panel scrolls the name — ``???`` while it is
still a silhouette, the real name once revealed. After the reveal has been up
long enough to read, the mode dissolves back out and picks another.

There is no auto-rotation and no scoring. The whole point of picking Gen 1 is
that the silhouettes are the ones people actually recognize; the mode is here
to be looked at, not to demand attention.

Sprites are the Gen 5 style animated-into-static PNGs from the PokéAPI sprite
repo, downloaded once to :attr:`Config.state_dir` / ``pokemon`` / ``NNN.png``
and never re-fetched. The first-run download is best-effort: if the network
call fails, the mode logs and skips to the next candidate, rather than blocking
the panel on a silhouette that will never reveal.
"""

from __future__ import annotations

import io
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from ..canvas import Canvas, MEDIUM
from ..config import Config
from ..modes.base import Mode
from .pokemon_names import GEN1_NAMES  # 151-entry list of display names, indexed by dex# - 1

_SCROLL_PX_PER_TICK = 1.0  # matches nametag / spotify feel

LOGGER = logging.getLogger(__name__)

# The Bulbapedia sprites people picture in their head are the 96×96 Gen 5 stills
# in the PokéAPI sprites repo, released under MIT. The repo pins ``master`` and
# has been stable for years — no versioned releases to track.
_SPRITE_URL = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{dex}.png"
_USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"
_FETCH_TIMEOUT = 6.0

# Panel geometry. The sprite area is a 32×32 block on the left, the label
# scrolls in the remaining 96 columns with a two-pixel gutter.
_SPRITE_BOX = 32
_TEXT_LEFT = 34
_TEXT_WIDTH = 128 - _TEXT_LEFT  # 94 px for the label
_TEXT_Y_TOP = 10       # matches spotify's text_y for MEDIUM-in-16-px zone visual
_TEXT_ZONE_H = 12      # Spleen MEDIUM cell height

# Frame counts, in "ticks" — the renderer runs at Config.fps. These are set as
# multiples of fps so they read as seconds regardless of frame rate.
_SILHOUETTE_SECS = 4.0    # shape only, ??? scrolling
_DISSOLVE_SECS = 1.2      # cross-fade from silhouette to full color
_REVEAL_SECS = 3.5        # colored, name scrolling
_FADE_OUT_SECS = 0.6      # gentle exit so the panel isn't jarring between rounds

# Colors. The TV bumper this is imitating puts a smooth horizontal gradient
# behind the silhouette — sky blue on the left, warm red on the right — not a
# hard split. LED matrices exaggerate mid-tones because the pixels are
# emissive, so both endpoint colors sit a shade darker than they would on a
# screen. Values picked by eye against a 128×32 preview at typical brightness.
_BG_BLUE = (24, 60, 128)      # left endpoint of the horizontal gradient
_BG_RED = (150, 22, 22)       # right endpoint
_SILHOUETTE_INK = (0, 0, 0)   # pure black; the colored BG makes it read now
_LABEL = (255, 240, 210)      # warm off-white on the red, matches the TV bumper
_MASK_QUESTION = (255, 230, 90)  # ??? in yellow, echoing the reference's mask


def _make_gradient_bg(width: int, height: int) -> Image.Image:
    """Blue-→-red horizontal gradient the width of the panel.

    A tiny vertical falloff darkens the top and bottom row very slightly so
    the panel doesn't look like a flat block of color — the reference bumper
    has a subtle radial gradient from the dot pattern, and this approximates
    that effect without introducing a whole dot-shading pass.
    """
    bg = Image.new("RGB", (width, height), (0, 0, 0))
    px = bg.load()
    for x in range(width):
        t = x / max(1, width - 1)
        r = int(round(_BG_BLUE[0] + (_BG_RED[0] - _BG_BLUE[0]) * t))
        g = int(round(_BG_BLUE[1] + (_BG_RED[1] - _BG_BLUE[1]) * t))
        b = int(round(_BG_BLUE[2] + (_BG_RED[2] - _BG_BLUE[2]) * t))
        for y in range(height):
            # Slight vertical darkening at the very top and bottom rows so the
            # panel doesn't look like a solid block. Falloff peaks at the edges.
            dy = abs(y - (height - 1) / 2) / ((height - 1) / 2)  # 0 center, 1 edge
            k = 1.0 - 0.12 * (dy ** 2)
            px[x, y] = (int(r * k), int(g * k), int(b * k))
    return bg


@dataclass
class _Round:
    dex: int
    name: str
    sprite: Image.Image        # 32×32 RGBA, cropped and centered
    start_tick: int
    silhouette: Image.Image = field(init=False)  # 32×32 RGBA, alpha copied from sprite but RGB=(0,0,0)

    def __post_init__(self) -> None:
        r, g, b, a = self.sprite.split()
        # Pure black silhouette — the colored panel background provides the
        # contrast the TV bumper gets from its bright blue frame.
        tinted = Image.new("RGBA", self.sprite.size, (*_SILHOUETTE_INK, 255))
        tinted.putalpha(a)
        self.silhouette = tinted


class PokemonMode(Mode):
    """Silhouette → dissolve → reveal → dissolve loop over Kanto Pokémon."""

    _bg_cache: Image.Image | None = None

    def _gradient_bg(self) -> Image.Image:
        if self._bg_cache is None:
            self._bg_cache = _make_gradient_bg(128, 32)
        return self._bg_cache

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._cache_dir = config.state_dir / "pokemon"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # ``_pool`` is the order we'll show them in — a shuffled copy of the dex
        # numbers we've successfully loaded a sprite for. When it empties we
        # reshuffle. Doing it this way instead of a raw ``random.choice`` on
        # every round means the same Pokémon can't repeat immediately.
        self._pool: list[int] = []
        self._round: _Round | None = None
        # Cache Pillow images in memory once loaded from disk — reading a small
        # PNG 151 times a session is wasteful, and the whole set is under 200 KB.
        self._loaded: dict[int, Image.Image] = {}
        # A random offset shifts the scrolling name so it doesn't always start
        # in exactly the same place.
        self._rng = random.Random()

    # ------------------------------------------------------------------ sprites

    def _sprite_path(self, dex: int) -> Path:
        return self._cache_dir / f"{dex:03d}.png"

    def _load_sprite(self, dex: int) -> Image.Image | None:
        """Return a 32×32 RGBA sprite, downloading if necessary.

        Returns ``None`` on any failure, so callers can skip to the next dex#.
        """
        cached = self._loaded.get(dex)
        if cached is not None:
            return cached
        path = self._sprite_path(dex)
        if not path.exists():
            if not self._download_sprite(dex, path):
                return None
        try:
            raw = Image.open(path).convert("RGBA")
        except (OSError, ValueError) as err:
            LOGGER.warning("Failed to open Pokémon sprite %s: %s", path, err)
            # Corrupt cached file — remove so a later run can re-download.
            try:
                path.unlink()
            except OSError:
                pass
            return None
        sprite = _fit_to_box(raw, _SPRITE_BOX)
        self._loaded[dex] = sprite
        return sprite

    def _download_sprite(self, dex: int, path: Path) -> bool:
        url = _SPRITE_URL.format(dex=dex)
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response:
                data = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            LOGGER.info("Pokémon sprite fetch failed for #%d: %s", dex, err)
            return False
        # Write via a temp file so a killed process can't leave a half-file
        # behind that the next boot would try to open.
        tmp = path.with_suffix(".png.tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError as err:
            LOGGER.warning("Cannot write Pokémon sprite cache %s: %s", path, err)
            return False
        return True

    # ------------------------------------------------------------------ rounds

    def _next_dex(self) -> int | None:
        """Pick the next Pokémon whose sprite loads. Returns None if none do."""
        # First time (or after we've cycled through all 151) reshuffle.
        if not self._pool:
            self._pool = list(range(1, len(GEN1_NAMES) + 1))
            self._rng.shuffle(self._pool)
        # Try candidates until one loads. This handles the first-run case
        # where sprites still need to download: we may burn through several
        # dex#s the first time the panel enters this mode.
        while self._pool:
            candidate = self._pool.pop()
            if self._load_sprite(candidate) is not None:
                return candidate
        return None

    def _start_round(self, tick: int) -> None:
        dex = self._next_dex()
        if dex is None:
            # No sprites available — give up quietly. The render loop will keep
            # painting blank frames, which the caller can detect and switch off.
            self._round = None
            return
        sprite = self._loaded[dex]
        self._round = _Round(
            dex=dex,
            name=GEN1_NAMES[dex - 1],
            sprite=sprite,
            start_tick=tick,
        )

    # ------------------------------------------------------------------ render

    def render(self, canvas: Canvas, tick: int) -> None:
        fps = max(1, self.config.fps)
        if self._round is None:
            self._start_round(tick)
        if self._round is None:
            # Sprite pool empty (no cached sprites and no network). Show a
            # muted "..." rather than a blank panel; the panel is still alive.
            canvas.text_bold(2, _TEXT_Y_TOP, "...", _MASK_QUESTION, MEDIUM)
            return

        round_ = self._round
        elapsed = (tick - round_.start_tick) / fps

        # Phase boundaries. Everything is fractions of a second so the tempo
        # is the same at 30fps as it is at 60fps.
        silhouette_end = _SILHOUETTE_SECS
        dissolve_end = silhouette_end + _DISSOLVE_SECS
        reveal_end = dissolve_end + _REVEAL_SECS
        fade_out_end = reveal_end + _FADE_OUT_SECS

        if elapsed >= fade_out_end:
            # Round done — pick another. Reset the clock rather than folding
            # start_tick forward so tiny timing errors can't accumulate.
            self._start_round(tick)
            if self._round is None:
                canvas.text_bold(2, _TEXT_Y_TOP, "...", _MASK_QUESTION, MEDIUM)
                return
            round_ = self._round
            elapsed = 0.0

        # Compute how much of the colored sprite to show, in [0.0, 1.0].
        # Silhouette-only phase is 0.0; dissolve ramps to 1.0; reveal holds
        # at 1.0; fade-out ramps back to 0.0.
        if elapsed < silhouette_end:
            reveal_alpha = 0.0
        elif elapsed < dissolve_end:
            reveal_alpha = (elapsed - silhouette_end) / _DISSOLVE_SECS
        elif elapsed < reveal_end:
            reveal_alpha = 1.0
        else:
            reveal_alpha = 1.0 - (elapsed - reveal_end) / _FADE_OUT_SECS
        reveal_alpha = max(0.0, min(1.0, reveal_alpha))

        # Paint the horizontal blue→red gradient across the whole panel first.
        # Cache the gradient row on the mode instance since neither the panel
        # width nor the endpoint colors change at runtime; regenerating it
        # every frame is cheap but wasteful.
        canvas.image_buffer.paste(self._gradient_bg(), (0, 0))

        # Silhouette underneath so it stays visible through the fade, colored
        # sprite on top with alpha scaled by reveal_alpha.
        composite = round_.silhouette.copy()
        color = round_.sprite.copy()
        r, g, b, a = color.split()
        scaled_alpha = a.point(lambda v, s=reveal_alpha: int(v * s))
        color.putalpha(scaled_alpha)
        composite = Image.alpha_composite(composite, color)
        canvas.image(0, 0, composite)

        # Right-hand label. Before we're mostly revealed the crowd is still
        # guessing, so scroll ??? in a dimmer color. After the reveal, scroll
        # the actual name in the standard label white.
        show_name = reveal_alpha >= 0.5
        label = round_.name.upper() if show_name else "??? ??? ???"
        label_color = _LABEL if show_name else _MASK_QUESTION
        # scroll_text on the shared canvas would paint across the sprite area,
        # so render into a scratch canvas the width of the label zone and blit
        # that back — same pattern spotify's now-playing scroller uses. The
        # scratch is pre-filled from the same gradient row as the underlying
        # panel so the paste blends into the backdrop instead of stamping a
        # black (or flat-red) rectangle across it.
        _draw_scrolling_label(canvas, label, label_color, tick, backdrop_strip=self._gradient_bg())


def _draw_scrolling_label(
    canvas: Canvas,
    text: str,
    color: tuple[int, int, int],
    tick: int,
    backdrop_strip: Image.Image | None = None,
) -> None:
    """Scroll *text* left-to-right in the label zone only, leaving the sprite alone.

    ``Canvas.scroll_text`` draws across the whole panel width, which would
    happily paint on top of the sprite. Instead, render one full period of the
    text into a scratch canvas the label zone's width, then paste that back at
    the label zone's origin so the sprite side stays untouched.

    ``backdrop_strip``, if provided, is a same-size-as-panel image whose label
    region is copied into the scratch canvas first so the text renders on top
    of the real backdrop instead of black.
    """
    period = canvas.text_width(text, MEDIUM) + 12  # 12px gap matches scroll_text's default
    if period <= 12:
        return
    scratch = Canvas(_TEXT_WIDTH, _TEXT_ZONE_H)
    if backdrop_strip is not None:
        region = backdrop_strip.crop(
            (_TEXT_LEFT, _TEXT_Y_TOP, _TEXT_LEFT + _TEXT_WIDTH, _TEXT_Y_TOP + _TEXT_ZONE_H)
        )
        scratch.image_buffer.paste(region, (0, 0))
    scratch.scroll_text(0, text, color, tick, MEDIUM, gap=12)
    canvas.image_buffer.paste(scratch.image_buffer, (_TEXT_LEFT, _TEXT_Y_TOP))


def _fit_to_box(sprite: Image.Image, box: int) -> Image.Image:
    """Trim transparent padding and center in a ``box``×``box`` RGBA image.

    PokéAPI sprites are 96×96 with roughly a 30-pixel border of transparency,
    so scaling the raw square would waste a third of our LED real estate on
    empty space. Cropping to the visible bbox first lets us use the full
    32×32 for the actual creature.
    """
    bbox = sprite.getbbox()
    if bbox is not None:
        sprite = sprite.crop(bbox)
    # Preserve aspect ratio; ``thumbnail`` won't upscale, which is what we want:
    # a Gen 5 sprite is 96×96 max and always shrinks down.
    sprite.thumbnail((box, box), Image.LANCZOS)
    out = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    out.paste(sprite, ((box - sprite.width) // 2, (box - sprite.height) // 2), sprite)
    return out
