# MIT License — Copyright (c) 2026 John Kuok
"""Desk name tag — a single name in bold, in a color of the wearer's choice.

Meant to be printed to a coworker's ticker so the panel becomes a permanent
desk plate rather than a live data feed. The name, its color, and its font
all live on the config and are settable from the web control panel.

Three font families are available, each optimized for a different look:

* ``spleen`` (default) — Spleen 8x16 bold auto-shrinks through a ladder
  (LARGE bold → LARGE plain → MEDIUM bold → MEDIUM plain) so short names
  land big and longer names step down gracefully.
* ``terminus`` — Terminus 14 bold. Utility monospace with a real bold cut;
  the "official conference badge" look.
* ``scientifica`` — Scientifica 11. Tall, condensed, distinctive; good for
  names long enough that Spleen would step down.

If the chosen family still overflows the available width (or if a name is
too long for even the Spleen ladder's smallest tier), the text scrolls
seamlessly right-to-left instead of being truncated.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..canvas import Canvas, LARGE, MEDIUM, load_font as load_spleen, sanitize
from ..config import Config

# Perplexity mark, pre-rasterized to a 19x24 1-bit bitmap that is mirror-
# symmetric around its own center column. Shipped as a static asset so the Pi
# does not need cairosvg at runtime; the bitmap is recolored per-render to
# match the wearer's chosen text color.
_MARK_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "logos" / "perplexity_24.png"

# The mark sits in a fixed left zone; the name centers in whatever's left.
# 19 (mark width) + 1 (left margin) + 3 (gap) = 23 px of left zone.
MARK_LEFT_MARGIN = 1
MARK_TEXT_GAP = 3

# 128-pixel panel, minus one pixel of edge margin on the right, minus the
# left zone reserved for the mark.
MAX_TEXT_WIDTH = 128 - 1 - 19 - MARK_LEFT_MARGIN - MARK_TEXT_GAP

# Fallback name shown before the wearer sets their own, so a brand-new panel
# still looks like a name tag instead of a mystery blank.
DEFAULT_NAME = "HELLO"

# Perplexity brand teal. The mark always ships in brand color regardless of
# what the wearer picks for the name, so the plate still reads as Perplexity
# even when the name is amber or pink.
MARK_COLOR = (32, 128, 141)  # #20808D

# Non-Spleen bitmap fonts, bundled as PIL font pairs (.pil + .pbm).
_EXTRA_FONTS_DIR = Path(__file__).resolve().parents[1] / "fonts"

# Font family registry. Each entry describes how a family is rendered:
#   * path: PIL font file, None means "use the Spleen size ladder"
#   * cap_height: pixel height of a capital letter, for vertical centering
#     (measured empirically, not the same as the full cell height because
#     PIL BDF fonts include leading above and below the cell)
FAMILY_SPLEEN = "spleen"
FAMILY_TERMINUS = "terminus"
FAMILY_SCIENTIFICA = "scientifica"

VALID_FAMILIES = (FAMILY_SPLEEN, FAMILY_TERMINUS, FAMILY_SCIENTIFICA)
DEFAULT_FAMILY = FAMILY_SPLEEN

# For fixed-size families, we need to know where to draw so the text visually
# centers on the 32-tall panel. PIL draws relative to the font's own baseline
# metrics, which include ascender/descender space we don't want. We use the
# actual pixel bounding box of a probe string to pin the text.
_PROBE = "HgjyJK"

# Scroll behavior when the name is too long for the chosen family.
SCROLL_GAP_PX = 20  # gap between the tail of the name and the head of the repeat
SCROLL_PX_PER_TICK = 1  # 1 px per render tick; renderer ticks at ~30 fps


def _load_mark_bitmap() -> Image.Image | None:
    """Load the mark bitmap once and cache; returns None if the asset is missing."""
    if not _MARK_PATH.exists():
        return None
    return Image.open(_MARK_PATH).convert("1")


@lru_cache(maxsize=4)
def _load_family_font(family: str) -> ImageFont.ImageFont | None:
    """Load and cache the PIL font for a non-Spleen family. Returns None if missing."""
    if family == FAMILY_TERMINUS:
        path = _EXTRA_FONTS_DIR / "terminus-14-bold.pil"
    elif family == FAMILY_SCIENTIFICA:
        path = _EXTRA_FONTS_DIR / "scientifica-11.pil"
    else:
        return None
    if not path.exists():
        return None
    return ImageFont.load(str(path))


def _measure(font: ImageFont.ImageFont, text: str) -> tuple[int, int, int, int]:
    """Return (width, height, top_offset, left_offset) for *text* drawn with *font*.

    top_offset/left_offset are the coordinates of the ink's top-left corner
    relative to the (0, 0) draw origin, so a caller can translate a desired
    on-panel position into the correct draw() call.
    """
    probe = Image.new("1", (max(400, 8 * len(text) + 8), 60), 0)
    d = ImageDraw.Draw(probe)
    d.text((0, 0), text, fill=1, font=font)
    bbox = probe.getbbox()
    if bbox is None:
        return 0, 0, 0, 0
    return bbox[2] - bbox[0], bbox[3] - bbox[1], bbox[1], bbox[0]


class NametagMode:
    """A name-tag renderer that supports fixed and Spleen font families,
    scrolling when the name is too wide for the chosen family."""

    def __init__(self, config: Config) -> None:
        self._config = config
        # Cache the mark bitmap on init; missing asset falls back to text-only.
        self._mark = _load_mark_bitmap()

    def render(self, canvas: Canvas, tick: int) -> None:
        name = self._config.current_nametag_name() or DEFAULT_NAME
        color = self._config.current_nametag_color()
        family = self._resolve_family()

        # Paint the mark in Perplexity teal (fixed), then place the name in
        # the wearer's chosen color in the remaining right zone.
        mark_width = self._paint_mark(canvas, MARK_COLOR)
        text_zone_left = MARK_LEFT_MARGIN + mark_width + MARK_TEXT_GAP
        text_zone_width = canvas.width - text_zone_left - 1

        if family == FAMILY_SPLEEN:
            self._render_spleen(canvas, name, color, text_zone_left, text_zone_width, tick)
        else:
            self._render_fixed(canvas, name, color, family, text_zone_left, text_zone_width, tick)

    def _resolve_family(self) -> str:
        """Return the current family from config, defaulting on missing/unknown values."""
        raw = self._config.current_nametag_font()
        if raw in VALID_FAMILIES:
            return raw
        return DEFAULT_FAMILY

    # ---------- Spleen family (the original auto-ladder) ---------------------

    def _render_spleen(
        self,
        canvas: Canvas,
        name: str,
        color: tuple[int, int, int],
        text_zone_left: int,
        text_zone_width: int,
        tick: int,
    ) -> None:
        """Auto-pick size in the Spleen ladder; scroll if even the smallest tier overflows."""
        size, use_bold, width = _fit_spleen(canvas, name, text_zone_width)

        # Vertical centering per font tier. LARGE is 16 tall on a 32-tall
        # panel, MEDIUM is 12 tall; both split the empty space evenly.
        line_height = 16 if size == LARGE else 12
        y = (canvas.height - line_height) // 2

        if width > text_zone_width:
            # Even MEDIUM plain overflows: scroll. Truncate is what the old
            # code did; scrolling is much more useful when a name genuinely
            # exceeds the panel (e.g. "Alexandra Constantinescu-Weinberg").
            self._scroll_spleen(canvas, name, color, size, use_bold,
                                text_zone_left, text_zone_width, y, tick)
            return

        x = text_zone_left + (text_zone_width - width) // 2
        if use_bold:
            canvas.text_bold(x, y, name, color, size, weight=1)
        else:
            canvas.text(x, y, name, color, size)

    def _scroll_spleen(
        self,
        canvas: Canvas,
        name: str,
        color: tuple[int, int, int],
        size: int,
        use_bold: bool,
        text_zone_left: int,
        text_zone_width: int,
        y: int,
        tick: int,
    ) -> None:
        """Render a scrolling Spleen name inside a clipped text zone."""
        text_w = (
            canvas.text_bold_width(name, size, weight=1) if use_bold
            else canvas.text_width(name, size)
        )
        period = text_w + SCROLL_GAP_PX
        offset = (tick * SCROLL_PX_PER_TICK) % period
        start_x = text_zone_left - offset

        # Prepare a temp image containing the full text, then paste twice
        # (seamless loop) with clipping to the text zone.
        cell_h = 16 if size == LARGE else 12
        strip = Image.new("RGB", (text_w, cell_h), (0, 0, 0))
        strip_canvas = Canvas(text_w, cell_h)
        # Overlay a black rect first so we start clean.
        if use_bold:
            strip_canvas.text_bold(0, 0, name, color, size, weight=1)
        else:
            strip_canvas.text(0, 0, name, color, size)
        strip = strip_canvas.image_buffer

        _blit_clipped(canvas, strip, start_x, y, text_zone_left, text_zone_width)
        _blit_clipped(canvas, strip, start_x + period, y, text_zone_left, text_zone_width)

    # ---------- Fixed-size families (Terminus, Scientifica) ------------------

    def _render_fixed(
        self,
        canvas: Canvas,
        name: str,
        color: tuple[int, int, int],
        family: str,
        text_zone_left: int,
        text_zone_width: int,
        tick: int,
    ) -> None:
        """Render with a fixed non-Spleen font; scroll if it overflows."""
        font = _load_family_font(family)
        if font is None:
            # Missing font file: fall back to Spleen behavior. This should
            # only trigger if the packaged fonts were deleted at runtime.
            self._render_spleen(canvas, name, color, text_zone_left, text_zone_width, tick)
            return

        cleaned = sanitize(name)
        text_w, text_h, top, left = _measure(font, cleaned)

        # Use the probe to compute a stable baseline: measure cap height in
        # the current font and center that vertical span on the panel.
        _, probe_h, probe_top, _ = _measure(font, _PROBE)
        y = (canvas.height - probe_h) // 2 - probe_top

        if text_w <= text_zone_width:
            x = text_zone_left + (text_zone_width - text_w) // 2 - left
            self._draw_pil_text(canvas, font, cleaned, x, y, color)
            return

        # Overflow: scroll seamlessly.
        period = text_w + SCROLL_GAP_PX
        offset = (tick * SCROLL_PX_PER_TICK) % period
        start_x = text_zone_left - offset - left

        # Render the whole name to a strip, then clip-blit twice.
        strip = Image.new("RGB", (text_w, canvas.height), (0, 0, 0))
        d = ImageDraw.Draw(strip)
        d.text((-left, y), cleaned, fill=color, font=font)

        _blit_clipped(canvas, strip, start_x + left, 0, text_zone_left, text_zone_width)
        _blit_clipped(canvas, strip, start_x + left + period, 0, text_zone_left, text_zone_width)

    def _draw_pil_text(
        self,
        canvas: Canvas,
        font: ImageFont.ImageFont,
        text: str,
        x: int,
        y: int,
        color: tuple[int, int, int],
    ) -> None:
        """Draw a PIL bitmap font glyph run onto the canvas at (x, y)."""
        # Canvas exposes _draw for internal use; nametag is the only client
        # that draws with a non-Spleen font, so a light reach-in here beats
        # widening the canvas API for one caller.
        canvas._draw.text((x, y), text, font=font, fill=color)  # noqa: SLF001

    def _paint_mark(self, canvas: Canvas, color: tuple[int, int, int]) -> int:
        """Draw the recolored mark into the canvas; return its width (0 if missing)."""
        if self._mark is None:
            return 0
        mw, mh = self._mark.size
        # Vertically center the mark on the 32-tall panel.
        top = (canvas.height - mh) // 2
        left = MARK_LEFT_MARGIN
        # 1-bit bitmap: walk every lit pixel and set it to the chosen color.
        # The bitmap is small (19x24), so this is well under a millisecond.
        pixels = self._mark.load()
        for y in range(mh):
            for x in range(mw):
                if pixels[x, y]:
                    canvas.pixel(left + x, top + y, color)
        return mw


def _blit_clipped(
    canvas: Canvas,
    src: Image.Image,
    dst_x: int,
    dst_y: int,
    clip_x: int,
    clip_w: int,
) -> None:
    """Paste *src* onto the canvas at (dst_x, dst_y), clipping to [clip_x, clip_x+clip_w).

    Used for scrolling text so the moving strip is masked to the text zone
    and does not spill onto the mark or past the right edge of the panel.
    """
    sw, sh = src.size
    src_left = max(0, clip_x - dst_x)
    src_right = min(sw, clip_x + clip_w - dst_x)
    if src_right <= src_left:
        return
    piece = src.crop((src_left, 0, src_right, sh))
    canvas.image_buffer.paste(piece, (dst_x + src_left, dst_y))


def _fit_spleen(canvas: Canvas, name: str, max_width: int = MAX_TEXT_WIDTH) -> tuple[int, bool, int]:
    """Pick the largest Spleen tier the name fits into, and return (size, bold, width).

    ``max_width`` shrinks when the logo takes up part of the panel, so short
    names still land at LARGE bold and longer names step down to MEDIUM.

    If nothing in the ladder fits, returns the MEDIUM-plain measurement of
    the full (untruncated) name; the caller uses that to decide whether to
    scroll rather than truncate.
    """
    for size, use_bold in ((LARGE, True), (LARGE, False), (MEDIUM, True), (MEDIUM, False)):
        width = (
            canvas.text_bold_width(name, size, weight=1)
            if use_bold
            else canvas.text_width(name, size)
        )
        if width <= max_width:
            return size, use_bold, width

    # Nothing fits: return the MEDIUM-plain width so the caller can scroll.
    width = canvas.text_width(name, MEDIUM)
    return MEDIUM, False, width
