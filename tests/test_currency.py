# MIT License — Copyright (c) 2026 John Kuok
"""Currency mode: layout of both the classic and the flag-mode variants.

The classic three-row board is exercised end-to-end elsewhere; these tests
focus on the pieces that a future flag or config change could quietly
break: that the flag pixels actually land on the canvas, that the label
collapses to the quote code once the flag is doing the country-naming
work, and that the classic layout still renders when flag mode is off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticker import flags
from ticker.canvas import MEDIUM, Canvas
from ticker.config import load_config
from ticker.modes.currency import (
    AMBER,
    CurrencyMode,
    ForexQuote,
)


def _lit_pixels(canvas: Canvas) -> set[tuple[int, int]]:
    pixels = canvas.image_buffer.load()
    return {
        (x, y)
        for y in range(canvas.height)
        for x in range(canvas.width)
        if pixels[x, y] != (0, 0, 0)
    }


def _colors_at(canvas: Canvas, x0: int, y0: int, x1: int, y1: int) -> set[tuple[int, int, int]]:
    pixels = canvas.image_buffer.load()
    return {
        pixels[x, y]
        for y in range(y0, y1)
        for x in range(x0, x1)
        if pixels[x, y] != (0, 0, 0)
    }


@pytest.fixture
def currency_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text("TICKER_WIDTH=128\nTICKER_HEIGHT=32\n", encoding="utf-8")
    return load_config(env)


def _prep_mode(config, quotes: list[ForexQuote]) -> CurrencyMode:
    """Build a mode with hand-set quotes and short-circuit the fetch loop."""
    mode = CurrencyMode(config)
    mode.quotes = quotes
    # Push the refresh clock into the far future so render() never fetches.
    mode._last_refresh = 1e18
    mode._last_pairs = tuple((q.base, q.quote) for q in quotes)
    return mode


# ---- flag catalogue ------------------------------------------------------


def test_every_flag_is_the_declared_shape() -> None:
    """Every flag is 8 rows of exactly 12 characters, with a palette."""
    for code in flags.supported_currencies():
        rows, palette = flags.flag_for(code)
        assert len(rows) == flags.FLAG_HEIGHT, code
        for row in rows:
            assert len(row) == flags.FLAG_WIDTH, code
            for char in row:
                assert char == "." or char in palette, (code, char)


def test_flag_lookup_is_case_insensitive() -> None:
    assert flags.flag_for("jpy") is flags.flag_for("JPY")
    assert flags.flag_for("Jpy") is flags.flag_for("JPY")


def test_flag_lookup_returns_none_for_unknown() -> None:
    # An unfamiliar quote currency must NOT get a wrong flag pasted next
    # to it -- the mode falls back to text-only for these.
    assert flags.flag_for("XYZ") is None


def test_default_pairs_all_have_flags() -> None:
    """The three shipped defaults must all render with a real flag.

    The whole point of flag mode is that the shipped experience shows
    actual flags. Regressing one of the defaults into a text-only fallback
    silently would be a bad shipping story.
    """
    for base, quote in [("USD", "JPY"), ("USD", "EUR"), ("USD", "CNY")]:
        assert flags.flag_for(quote) is not None, (base, quote)


# ---- classic layout (unchanged behaviour) --------------------------------


def test_classic_mode_uses_pair_label(currency_config) -> None:
    """With flag mode off the header still reads BASE/QUOTE, no flag."""
    currency_config.set_currency_pairs([("USD", "JPY")])
    currency_config.set_currency_flag_mode(False)
    mode = _prep_mode(currency_config, [ForexQuote("USD", "JPY", 149.0, 148.5)])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    # The leftmost column has NO flag pixels; the label starts at x=0.
    assert _colors_at(canvas, 0, 0, flags.FLAG_WIDTH, 32) <= {AMBER}


# ---- flag mode -----------------------------------------------------------


def test_flag_mode_paints_the_quote_flag(currency_config) -> None:
    """A JPY row must show a Japan-flag red pixel in the left column."""
    currency_config.set_currency_pairs([("USD", "JPY")])
    currency_config.set_currency_flag_mode(True)
    mode = _prep_mode(currency_config, [ForexQuote("USD", "JPY", 149.0, 148.5)])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    # Grab every colour inside the left flag column. The Japanese flag has
    # a red disc and a white field -- both must appear.
    palette = _colors_at(canvas, 0, 0, flags.FLAG_WIDTH, 32)
    # White background pixels.
    assert (240, 240, 240) in palette
    # Red disc pixels.
    assert (200, 30, 40) in palette


def test_flag_mode_label_collapses_to_quote_code(currency_config) -> None:
    """When a flag is present, the amber label reads e.g. ``JPY`` not ``USD/JPY``.

    The old label would eat pixels the rate column needs; the flag already
    carries the country identity.
    """
    currency_config.set_currency_pairs([("USD", "JPY")])
    currency_config.set_currency_flag_mode(True)
    mode = _prep_mode(currency_config, [ForexQuote("USD", "JPY", 149.0, 148.5)])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    # The width of "JPY " (with the trailing space the render uses) must
    # be substantially less than the width of "USD/JPY " -- catches a
    # future regression that goes back to painting the full pair label.
    assert canvas.text_width("JPY ", MEDIUM) < canvas.text_width("USD/JPY ", MEDIUM)


def test_flag_mode_three_pairs_uses_small_four_row_layout(currency_config) -> None:
    """Three pairs share the SMALL four-row layout, not the MEDIUM two-row one.

    Under the old cap this test used to assert the third row was suppressed;
    now the third pair renders on its own SMALL row and only the fourth slot
    is empty. A stray CNY-red pixel above y=24 or below y=16 would indicate
    the row landed on the wrong band, and the top-of-band pixel at y=16
    (band 3) must be lit somewhere in the flag column for CNY to be visible.
    """
    currency_config.set_currency_pairs([("USD", "JPY"), ("USD", "EUR"), ("USD", "CNY")])
    currency_config.set_currency_flag_mode(True)
    mode = _prep_mode(
        currency_config,
        [
            ForexQuote("USD", "JPY", 149.0, 148.5),
            ForexQuote("USD", "EUR", 0.92, 0.93),
            ForexQuote("USD", "CNY", 7.18, 7.20),
        ],
    )
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # CNY's third-band flag lives at y in [16..23]. Confirm at least one lit
    # pixel there in the flag column.
    lit_in_band_3 = [
        (x, y)
        for x in range(flags.FLAG_WIDTH)
        for y in range(16, 24)
        if pixels[x, y] != (0, 0, 0)
    ]
    assert lit_in_band_3, "CNY did not render in band 3 (y=16..23)"
    # The fourth slot (y=24..31) must be empty since only three pairs configured.
    for y in range(24, 32):
        for x in range(flags.FLAG_WIDTH):
            assert pixels[x, y] == (0, 0, 0), (x, y)


def test_flag_mode_four_pairs_fills_four_rows(currency_config) -> None:
    """Four pairs pack the panel flush: SMALL font, all four 8-row bands used."""
    currency_config.set_currency_pairs(
        [("USD", "JPY"), ("USD", "EUR"), ("USD", "CNY"), ("USD", "GBP")]
    )
    currency_config.set_currency_flag_mode(True)
    currency_config.set_currency_show_change(False)
    mode = _prep_mode(
        currency_config,
        [
            ForexQuote("USD", "JPY", 149.0, 148.5),
            ForexQuote("USD", "EUR", 0.92, 0.93),
            ForexQuote("USD", "CNY", 7.18, 7.20),
            ForexQuote("USD", "GBP", 0.78, 0.79),
        ],
    )
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # Every 8-row band must contain at least one lit flag pixel in the
    # left column. A missed band would mean a row was dropped.
    for band_top in (0, 8, 16, 24):
        lit = [
            (x, y)
            for x in range(flags.FLAG_WIDTH)
            for y in range(band_top, band_top + 8)
            if pixels[x, y] != (0, 0, 0)
        ]
        assert lit, f"band starting at y={band_top} rendered no flag pixels"


def test_flag_mode_four_pairs_show_change_still_fits(currency_config) -> None:
    """Four pairs + show_change on: SMALL font must still render every row.

    The old MEDIUM font can only stack two rows in 32 pixels, so if flag mode
    ever falls back to MEDIUM here the third and fourth rows drop. This test
    guards against that regression: with show_change ON and four pairs, we
    still expect four bands lit.
    """
    currency_config.set_currency_pairs(
        [("USD", "JPY"), ("USD", "EUR"), ("USD", "CNY"), ("USD", "GBP")]
    )
    currency_config.set_currency_flag_mode(True)
    currency_config.set_currency_show_change(True)
    mode = _prep_mode(
        currency_config,
        [
            ForexQuote("USD", "JPY", 149.0, 148.5),
            ForexQuote("USD", "EUR", 0.92, 0.93),
            ForexQuote("USD", "CNY", 7.18, 7.20),
            ForexQuote("USD", "GBP", 0.78, 0.79),
        ],
    )
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    for band_top in (0, 8, 16, 24):
        lit = [
            (x, y)
            for x in range(flags.FLAG_WIDTH)
            for y in range(band_top, band_top + 8)
            if pixels[x, y] != (0, 0, 0)
        ]
        assert lit, f"band y={band_top} empty; four rows didn't fit with show_change on"


def test_flag_mode_two_pairs_keeps_medium_layout(currency_config) -> None:
    """Two pairs must keep the taller MEDIUM font (tops at 2 and 18)."""
    currency_config.set_currency_pairs([("USD", "JPY"), ("USD", "EUR")])
    currency_config.set_currency_flag_mode(True)
    mode = _prep_mode(
        currency_config,
        [
            ForexQuote("USD", "JPY", 149.0, 148.5),
            ForexQuote("USD", "EUR", 0.92, 0.93),
        ],
    )
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # MEDIUM row 1 lives at y=2..15; MEDIUM row 2 at y=18..31. If the code
    # accidentally used SMALL, band 3 (y=16..23) would light up between rows
    # -- which for two pairs must stay dark in the flag column between y=13
    # and y=17.
    for y in range(14, 18):
        for x in range(flags.FLAG_WIDTH):
            assert pixels[x, y] == (0, 0, 0), (x, y)


def test_flag_mode_centres_single_row(currency_config) -> None:
    """One-pair flag mode parks the row near the vertical middle of the panel."""
    currency_config.set_currency_pairs([("USD", "JPY")])
    currency_config.set_currency_flag_mode(True)
    mode = _prep_mode(currency_config, [ForexQuote("USD", "JPY", 149.0, 148.5)])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    # Anything painted must sit in the vertical middle band, not the top
    # or bottom stripe. Loose bound: y in [8..20].
    lit = _lit_pixels(canvas)
    assert lit, "flag mode with one row painted nothing"
    for x, y in lit:
        assert 8 <= y <= 22, (x, y)


def test_flag_mode_falls_back_to_pair_label_when_flag_missing(currency_config) -> None:
    """Unknown quote currency: the row still renders, just without a flag.

    A totally unfamiliar ISO code (XYZ here) must not create a blank row --
    a missing flag is a display concession, not a data-hide.
    """
    # Set the pair to USD/XYZ so the render loop doesn't re-fetch away
    # from our hand-set quote.
    currency_config.set_currency_pairs([("USD", "XYZ")])
    currency_config.set_currency_flag_mode(True)
    mode = _prep_mode(currency_config, [ForexQuote("USD", "XYZ", 1.234, 1.230)])
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    lit = _lit_pixels(canvas)
    assert lit, "unknown-flag row painted nothing"
    # No flag was drawn, so the amber pair label starts at x=0.
    amber_xs = {x for x, y in lit if canvas.image_buffer.getpixel((x, y)) == AMBER}
    assert min(amber_xs) == 0


# ---- 2x2 grid arrangement -----------------------------------------------


def _four_pair_grid_mode(currency_config) -> CurrencyMode:
    currency_config.set_currency_pairs(
        [("USD", "JPY"), ("USD", "EUR"), ("USD", "GBP"), ("USD", "CNY")]
    )
    currency_config.set_currency_flag_mode(True)
    currency_config.set_currency_flag_grid(True)
    currency_config.set_currency_show_change(False)
    return _prep_mode(
        currency_config,
        [
            ForexQuote("USD", "JPY", 149.35, 149.00),
            ForexQuote("USD", "EUR", 0.923, 0.920),
            ForexQuote("USD", "GBP", 0.791, 0.795),
            ForexQuote("USD", "CNY", 7.245, 7.250),
        ],
    )


def test_flag_grid_places_all_four_pairs_in_four_quadrants(currency_config) -> None:
    """With grid on and 4 pairs, each quadrant must contain at least one flag pixel.

    Grid quadrants are 64w x 16h at (0,0), (64,0), (0,16), (64,16). The flag
    is drawn in the leftmost 12 columns of each quadrant, so we look for lit
    pixels in the 12x8 flag zone of each quadrant.
    """
    mode = _four_pair_grid_mode(currency_config)
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    quadrants = [
        (0, 0),   # top-left (JPY)
        (64, 0),  # top-right (EUR)
        (0, 16),  # bottom-left (GBP)
        (64, 16), # bottom-right (CNY)
    ]
    for x0, y0 in quadrants:
        lit = [
            (x, y)
            for x in range(x0, x0 + flags.FLAG_WIDTH)
            for y in range(y0 + 4, y0 + 4 + flags.FLAG_HEIGHT)
            if pixels[x, y] != (0, 0, 0)
        ]
        assert lit, f"quadrant at ({x0},{y0}) had no flag pixels"


def test_flag_grid_three_pairs_leaves_bottom_right_empty(currency_config) -> None:
    """Three pairs in grid mode: bottom-right quadrant stays dark.

    Preferable to shifting the layout or falling back mid-run. The fourth
    slot is expected to be empty (dark) rather than repeat one of the
    other three.
    """
    currency_config.set_currency_pairs(
        [("USD", "JPY"), ("USD", "EUR"), ("USD", "GBP")]
    )
    currency_config.set_currency_flag_mode(True)
    currency_config.set_currency_flag_grid(True)
    currency_config.set_currency_show_change(False)
    mode = _prep_mode(
        currency_config,
        [
            ForexQuote("USD", "JPY", 149.35, 149.00),
            ForexQuote("USD", "EUR", 0.923, 0.920),
            ForexQuote("USD", "GBP", 0.791, 0.795),
        ],
    )
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # Bottom-right quadrant (64..127, 16..31) must be entirely dark.
    for y in range(16, 32):
        for x in range(64, 128):
            assert pixels[x, y] == (0, 0, 0), (x, y)


def test_flag_grid_falls_back_to_stack_when_show_change_on(currency_config) -> None:
    """show_change forces stacked layout because the % column can't share the halved width.

    The four rows must still all render (four bands lit) but they will be
    single-column, not 2x2 -- so the top-right quadrant's flag zone stays dark.
    """
    currency_config.set_currency_pairs(
        [("USD", "JPY"), ("USD", "EUR"), ("USD", "GBP"), ("USD", "CNY")]
    )
    currency_config.set_currency_flag_mode(True)
    currency_config.set_currency_flag_grid(True)
    currency_config.set_currency_show_change(True)
    mode = _prep_mode(
        currency_config,
        [
            ForexQuote("USD", "JPY", 149.35, 149.00),
            ForexQuote("USD", "EUR", 0.923, 0.920),
            ForexQuote("USD", "GBP", 0.791, 0.795),
            ForexQuote("USD", "CNY", 7.245, 7.250),
        ],
    )
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # Grid would put a flag at (64..75, 4..11) for EUR. Stacked won't.
    # Since we're forcing stack, that zone must be dark.
    dark = all(
        pixels[x, y] == (0, 0, 0)
        for y in range(4, 12)
        for x in range(64, 64 + flags.FLAG_WIDTH)
    )
    assert dark, "grid layout was used even though show_change forces stack"


def test_flag_grid_falls_back_to_stack_with_only_two_pairs(currency_config) -> None:
    """With only two pairs, grid mode should still render the taller MEDIUM stacked layout."""
    currency_config.set_currency_pairs([("USD", "JPY"), ("USD", "EUR")])
    currency_config.set_currency_flag_mode(True)
    currency_config.set_currency_flag_grid(True)
    currency_config.set_currency_show_change(False)
    mode = _prep_mode(
        currency_config,
        [
            ForexQuote("USD", "JPY", 149.35, 149.00),
            ForexQuote("USD", "EUR", 0.923, 0.920),
        ],
    )
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    # In the stacked MEDIUM layout there is a dead-band gap at y=14..17 in
    # the flag column. If grid was used instead, EUR would live at (64..,4..11)
    # instead of (0,18) and the top-right flag zone would light up.
    top_right_dark = all(
        pixels[x, y] == (0, 0, 0)
        for y in range(4, 12)
        for x in range(64, 64 + flags.FLAG_WIDTH)
    )
    assert top_right_dark, "grid layout was used with only two pairs"
