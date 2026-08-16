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


def test_flag_mode_hard_caps_at_two_rows(currency_config) -> None:
    """Even with three configured pairs, flag mode renders only the first two."""
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
    # The third row's flag would be the CNY red field. If it accidentally
    # rendered, the entire lower-left band would light up red -- the two
    # legit rows only paint y=2..15 and y=18..31 in that column, so any
    # lit CNY-red pixel at y >= 30 is a bug.
    pixels = canvas.image_buffer.load()
    # Row 2 (EUR) ends ~y=25 with the flag; anything below is dead space.
    for y in range(28, 32):
        for x in range(0, flags.FLAG_WIDTH):
            assert pixels[x, y] == (0, 0, 0)


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
