# MIT License — Copyright (c) 2026 John Kuok
"""Costco Gasoline mode: wordmark geometry and card layout.

The wordmark is a hand-drawn bitmap, so the things worth pinning down are
the ones a future glyph edit could silently break: that the mark fits its
declared box, that the tagline stays inside the logo's width, and that
the price rows land below the mark instead of on top of it.
"""

from __future__ import annotations

from ticker.canvas import Canvas
from ticker.modes.costco import (
    _LOGO_GLYPHS_6x8,
    _LOGO_GLYPHS_TAG,
    _LOGO_HEIGHT,
    _LOGO_TEXT,
    _LOGO_WIDTH,
    _STRIPE_ROWS,
    _TAG_TEXT,
    _TAG_WIDTH,
    COSTCO_RED,
    LOGO_BLUE,
    CostcoMode,
    WarehousePrices,
)


def _lit(canvas: Canvas) -> set[tuple[int, int]]:
    """Coordinates of every non-black pixel on the canvas."""
    pixels = canvas.image_buffer.load()
    return {
        (x, y)
        for y in range(canvas.height)
        for x in range(canvas.width)
        if pixels[x, y] != (0, 0, 0)
    }


def test_every_letter_has_a_glyph() -> None:
    """A missing glyph is skipped at render time, so catch it here."""
    for char in _LOGO_TEXT:
        assert char in _LOGO_GLYPHS_6x8, char
    for char in _TAG_TEXT:
        assert char in _LOGO_GLYPHS_TAG, char


def test_glyph_rows_are_rectangular() -> None:
    """Ragged rows would shift pixels and skew the advance width."""
    for table in (_LOGO_GLYPHS_6x8, _LOGO_GLYPHS_TAG):
        for char, glyph in table.items():
            widths = {len(row) for row in glyph}
            assert len(widths) == 1, (char, widths)
            assert set("".join(glyph)) <= {"0", "1"}, char


def test_tagline_fits_under_the_logo() -> None:
    """``GASOLINE`` is right-aligned under ``COSTCO``; it must be narrower.

    If the tagline ever grew wider than the logo it would hang off the
    left edge of the mark and collide with the stripes.
    """
    assert _TAG_WIDTH < _LOGO_WIDTH


def test_wordmark_stays_inside_its_declared_box() -> None:
    """The header layout budgets exactly ``_LOGO_WIDTH`` x ``_LOGO_HEIGHT``."""
    canvas = Canvas(128, 32)
    CostcoMode._draw_wordmark(CostcoMode, canvas, 0, 0)  # type: ignore[arg-type]
    lit = _lit(canvas)
    assert lit, "wordmark drew nothing"
    assert max(x for x, _ in lit) < _LOGO_WIDTH
    assert max(y for _, y in lit) < _LOGO_HEIGHT


def test_wordmark_uses_red_logo_and_blue_tagline() -> None:
    """Colour split is the whole point of the mark -- pin it."""
    canvas = Canvas(128, 32)
    CostcoMode._draw_wordmark(CostcoMode, canvas, 0, 0)  # type: ignore[arg-type]
    pixels = canvas.image_buffer.load()
    top = {pixels[x, y] for x, y in _lit(canvas) if y < 8}
    bottom = {pixels[x, y] for x, y in _lit(canvas) if y >= 9}
    assert top == {COSTCO_RED}
    assert bottom == {LOGO_BLUE}


def test_stripes_are_straight_and_left_aligned() -> None:
    """Each stripe is a flat run starting at the mark's left edge.

    The real sign slants these; at 32px tall a diagonal turns into a
    staircase, so they were deliberately flattened. This guards that.
    """
    canvas = Canvas(128, 32)
    CostcoMode._draw_wordmark(CostcoMode, canvas, 0, 0)  # type: ignore[arg-type]
    lit = _lit(canvas)
    tag_start = _LOGO_WIDTH - _TAG_WIDTH
    for row in _STRIPE_ROWS:
        run = sorted(x for x, y in lit if y == row and x < tag_start)
        assert run, f"row {row} had no stripe"
        assert run[0] == 0, f"stripe on row {row} does not start at x=0"
        # Contiguous: a flat stripe has no gaps.
        assert run == list(range(run[0], run[-1] + 1))


def _seeded_mode() -> CostcoMode:
    class _Config:
        fps = 30

        def current_costco_warehouses(self) -> list[str]:
            return ["475"]

    mode = CostcoMode(_Config())  # type: ignore[arg-type]
    mode._prices = {
        "475": WarehousePrices(
            "475", "SOUTH SAN Francisco", "1600 El Camino Real",
            "5.30", "5.74", "", short_name="El Camino",
        )
    }
    mode._last_ids = ("475",)
    mode._last_refresh = float("inf")
    return mode


def test_price_rows_clear_the_wordmark() -> None:
    """Prices must start below the mark, not overlap its bottom row."""
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # The row directly under the mark is the gutter -- it should be empty
    # on the left half, where GASOLINE and the REG label would collide.
    gutter = [pixels[x, _LOGO_HEIGHT] for x in range(_LOGO_WIDTH)]
    assert set(gutter) == {(0, 0, 0)}, "no clear gutter under the wordmark"


def test_card_renders_both_prices() -> None:
    """Regression guard that seeding + render still produces a full card."""
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    # Bottom two thirds of the panel carry the price rows.
    lit = {(x, y) for x, y in _lit(canvas) if y >= _LOGO_HEIGHT}
    assert lit, "price rows drew nothing"
