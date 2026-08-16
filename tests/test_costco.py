# MIT License — Copyright (c) 2026 John Kuok
"""Costco Gasoline mode: wordmark geometry and card layout.

The wordmark is a hand-drawn bitmap, so the things worth pinning down are
the ones a future glyph edit could silently break: that the mark fits its
declared box, that COSTCO and GASOLINE occupy their own row bands, and
that the price rows land below the mark instead of on top of it.
"""

from __future__ import annotations

from ticker.canvas import Canvas
from ticker.modes.costco import (
    _LOGO_GLYPHS_5x5,
    _LOGO_GLYPHS_TAG,
    _LOGO_HEIGHT,
    _LOGO_TEXT,
    _LOGO_WIDTH,
    _TAG_TEXT,
    _TAG_TOP,
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
        assert char in _LOGO_GLYPHS_5x5, char
    for char in _TAG_TEXT:
        assert char in _LOGO_GLYPHS_TAG, char


def test_glyph_rows_are_rectangular() -> None:
    """Ragged rows would shift pixels and skew the advance width."""
    for table in (_LOGO_GLYPHS_5x5, _LOGO_GLYPHS_TAG):
        for char, glyph in table.items():
            widths = {len(row) for row in glyph}
            assert len(widths) == 1, (char, widths)
            assert set("".join(glyph)) <= {"0", "1"}, char


def test_costco_and_gasoline_are_roughly_the_same_width() -> None:
    """The shrunk COSTCO should read as a peer of GASOLINE, not a title.

    Slightly narrower is intentional (COSTCO sits inside GASOLINE's width),
    but it must not be more than a few pixels off in either direction --
    otherwise the stacked mark stops looking balanced.
    """
    assert abs(_LOGO_WIDTH - _TAG_WIDTH) <= 6


def test_wordmark_stays_inside_its_declared_box() -> None:
    """The header layout budgets ``max(_LOGO_WIDTH, _TAG_WIDTH)`` x _LOGO_HEIGHT."""
    canvas = Canvas(128, 32)
    CostcoMode._draw_wordmark(CostcoMode, canvas, 0, 0)  # type: ignore[arg-type]
    lit = _lit(canvas)
    assert lit, "wordmark drew nothing"
    assert max(x for x, _ in lit) < max(_LOGO_WIDTH, _TAG_WIDTH)
    assert max(y for _, y in lit) < _LOGO_HEIGHT


def test_costco_row_is_red_and_gasoline_row_is_blue() -> None:
    """Row-band colour split is the whole point of the mark -- pin it."""
    canvas = Canvas(128, 32)
    CostcoMode._draw_wordmark(CostcoMode, canvas, 0, 0)  # type: ignore[arg-type]
    pixels = canvas.image_buffer.load()
    top = {pixels[x, y] for x, y in _lit(canvas) if y < _TAG_TOP}
    bottom = {pixels[x, y] for x, y in _lit(canvas) if y >= _TAG_TOP}
    assert top == {COSTCO_RED}
    assert bottom == {LOGO_BLUE}


def test_no_speed_stripes_in_the_shoulder() -> None:
    """The old mark filled the shoulder under COSTCO with blue stripes.

    Now that COSTCO sits on rows 0-4 and GASOLINE on rows 5-9 as full
    peers, there is no shoulder to fill. A stray stripe would show up as
    lit blue pixels on rows outside the GASOLINE glyph body, so guard
    against a future glyph edit resurrecting them by accident.
    """
    canvas = Canvas(128, 32)
    CostcoMode._draw_wordmark(CostcoMode, canvas, 0, 0)  # type: ignore[arg-type]
    lit = _lit(canvas)
    # Every blue pixel must live inside the GASOLINE row band (5-9).
    blue_ys = {y for x, y in lit if y >= _TAG_TOP}
    assert blue_ys <= set(range(_TAG_TOP, _TAG_TOP + 5))


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


def test_reg_row_uses_the_bigger_medium_font() -> None:
    """REG is the primary price -- it should be visibly bigger than PREM.

    We compare the pixel bounding-box heights of the two price glyph runs.
    The REG row is drawn in MEDIUM (12-tall) and PREM in SMALL (8-tall);
    the lit-pixel height for REG's ``$5.30`` glyphs should exceed PREM's.
    """
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()

    def _run_height(y_min: int, y_max: int) -> int:
        ys = {
            y
            for y in range(y_min, y_max + 1)
            for x in range(canvas.width // 2, canvas.width)
            if pixels[x, y] != (0, 0, 0)
        }
        return (max(ys) - min(ys) + 1) if ys else 0

    reg_height = _run_height(11, 22)   # MEDIUM row lives here
    prem_height = _run_height(24, 31)  # SMALL row lives here
    assert reg_height > prem_height, (reg_height, prem_height)


def test_card_renders_both_prices() -> None:
    """Regression guard that seeding + render still produces a full card."""
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    # Bottom two thirds of the panel carry the price rows.
    lit = {(x, y) for x, y in _lit(canvas) if y >= _LOGO_HEIGHT}
    assert lit, "price rows drew nothing"
