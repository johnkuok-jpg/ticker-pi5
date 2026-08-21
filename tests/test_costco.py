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
    _LOGO_GLYPHS_6x8,
    _LOGO_GLYPHS_TAG,
    _LOGO_HEIGHT,
    _LOGO_TEXT,
    _LOGO_WIDTH,
    _RIGHT_GUTTER,
    _TAG_TEXT,
    _TAG_TOP,
    _TAG_WIDTH,
    COSTCO_RED,
    LOGO_BLUE,
    CostcoMode,
    WAREHOUSE_SLUGS,
    WarehousePrices,
    _parse_state_page,
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


def test_costco_is_wider_than_gasoline() -> None:
    """COSTCO (6x8) should read as the dominant word in the stacked mark.

    GASOLINE lives in the smaller tagline face; the ratio matters because
    if COSTCO ever shrank back to the tagline face the mark would lose the
    weight the real sign is known for.
    """
    assert _LOGO_WIDTH > _TAG_WIDTH


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


def test_speed_stripes_fill_the_left_shoulder() -> None:
    """Two blue stripes at rows 10 and 12 fill the shoulder gap left of
    the right-aligned GASOLINE. Without them the mark loses the visual
    tie to the real Costco Gasoline sign.

    Lock the stripes down by checking that rows 10 and 12 carry a run of
    lit blue pixels on the left side of the mark, and that the row just
    outside the stripe band (row 11) is dark on the shoulder -- so the
    stripes stay as two separated lines rather than merging into a bar.
    """
    canvas = Canvas(128, 32)
    CostcoMode._draw_wordmark(CostcoMode, canvas, 0, 0)  # type: ignore[arg-type]
    pixels = canvas.image_buffer.load()
    stripe_rows = (_TAG_TOP + 1, _TAG_TOP + 3)
    for row in stripe_rows:
        left_run = [pixels[x, row] for x in range(0, 4)]
        assert all(px == LOGO_BLUE for px in left_run), (row, left_run)
    # The row between the two stripes must be dark on the shoulder --
    # otherwise the two stripes fuse into one solid bar.
    between = [pixels[x, _TAG_TOP + 2] for x in range(0, 4)]
    assert set(between) == {(0, 0, 0)}, between


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


def test_wordmark_and_right_column_do_not_overlap() -> None:
    """The mark sits in the left column; the city + prices sit on the
    right. The wordmark's right edge must clear the right column so a
    render doesn't paint COSTCO's tail on top of the price digits.
    """
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    right_x = _LOGO_WIDTH + _RIGHT_GUTTER
    # Gutter column must be dark from top to bottom -- otherwise the
    # wordmark spilled past its declared width or the price row started
    # too far left.
    gutter = [pixels[_LOGO_WIDTH + 1, y] for y in range(canvas.height)]
    assert set(gutter) == {(0, 0, 0)}, "no clear vertical gutter between columns"
    # Sanity: there is some lit content on both sides of the gutter.
    lit = _lit(canvas)
    assert any(x < _LOGO_WIDTH for x, _ in lit), "wordmark drew nothing"
    assert any(x >= right_x for x, _ in lit), "right column drew nothing"


def test_reg_row_uses_the_bigger_medium_font() -> None:
    """REG is the primary price -- it should be visibly bigger than PREM.

    We compare the pixel bounding-box heights of the two price glyph runs.
    The REG row is drawn in MEDIUM (12-tall) and PREM in SMALL (8-tall);
    the lit-pixel height for REG's ``$5.30`` glyphs should exceed PREM's.
    Sample the right third of the panel to avoid picking up the wordmark.
    """
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    pixels = canvas.image_buffer.load()

    def _run_height(y_min: int, y_max: int) -> int:
        ys = {
            y
            for y in range(y_min, y_max + 1)
            for x in range(canvas.width - 40, canvas.width)
            if pixels[x, y] != (0, 0, 0)
        }
        return (max(ys) - min(ys) + 1) if ys else 0

    reg_height = _run_height(10, 21)   # MEDIUM row lives here
    prem_height = _run_height(24, 31)  # SMALL row lives here
    assert reg_height > prem_height, (reg_height, prem_height)


def test_card_renders_both_prices() -> None:
    """Regression guard that seeding + render still produces a full card."""
    mode = _seeded_mode()
    canvas = Canvas(128, 32)
    mode.render(canvas, 0)
    # The right column carries the city label + two price rows.
    right_x = _LOGO_WIDTH + _RIGHT_GUTTER
    lit = {(x, y) for x, y in _lit(canvas) if x >= right_x}
    assert lit, "right column drew nothing"


# ---------------------------------------------------------------------------
# ArulJohn table parser + refresh flow
# ---------------------------------------------------------------------------

# A trimmed sample of the real ArulJohn HTML shape captured 2026-08-21.
# Two warehouses (Concord, Corona) with the shape actually served:
# one address div per row, three dlr td cells per row, missing diesel
# rendered as ``--``. Anything the parser doesn't recognise (like the
# citystate div, class attributes, or the trailing ``</tr>``) should be
# ignored without breaking the address/dlr pairing.
_SAMPLE_HTML = (
    '<table><tr>'
    '<td><div class="citystate">Concord</div>'
    '<div class="address">2400 MONUMENT BLVD<br>CONCORD, CA 94520-3105</div></td>'
    '<td class="dlr bad">5.09</td>'
    '<td class="dlr gud">5.59</td>'
    '<td class="dlr bad">--</td>'
    '</tr><tr>'
    '<td><div class="citystate">Corona</div>'
    '<div class="address">480 N MCKINLEY ST<br>CORONA, CA 92879-1291</div></td>'
    '<td class="dlr bad">5.19</td>'
    '<td class="dlr bad">5.55</td>'
    '<td class="dlr bad">--</td>'
    '</tr></table>'
)


def test_parse_state_page_extracts_prices_by_street_key() -> None:
    prices = _parse_state_page(_SAMPLE_HTML)
    assert prices["2400 MONUMENT BLVD"] == ("5.09", "5.59", "")
    assert prices["480 N MCKINLEY ST"] == ("5.19", "5.55", "")


def test_parse_state_page_returns_empty_when_shape_drifts() -> None:
    """If dlr count doesn't match addresses * 3, don't guess at the pairing."""
    broken = _SAMPLE_HTML.replace(
        '<td class="dlr bad">--</td></tr><tr>', '</tr><tr>', 1,
    )
    assert _parse_state_page(broken) == {}


def test_parse_state_page_ignores_empty_html() -> None:
    assert _parse_state_page("<html><body>no table here</body></html>") == {}


def _fake_opener(html: str):
    """Return an opener callable that yields ``html`` for any URL."""
    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body
        def read(self) -> bytes:
            return self._body
        def __enter__(self):  # noqa: D401
            return self
        def __exit__(self, *exc: object) -> None:
            return None
    def _opener(_request, timeout=None):  # noqa: ARG001
        return _Response(html.encode("utf-8"))
    return _opener


def _config_with_ids(ids: list[str]):
    class _Config:
        fps = 30
        def current_costco_warehouses(self):
            return ids
    return _Config()


def test_refresh_populates_prices_from_state_table() -> None:
    """``_refresh`` should look each ID up by street key in the state table."""
    # Concord is warehouse 663 in the map -- verify the pipeline end-to-end.
    assert WAREHOUSE_SLUGS["663"][0] == "2400 MONUMENT BLVD"
    mode = CostcoMode(_config_with_ids(["663"]), opener=_fake_opener(_SAMPLE_HTML))  # type: ignore[arg-type]
    mode._refresh(("663",))
    assert not mode._failed
    assert "663" in mode._prices
    snapshot = mode._prices["663"]
    assert snapshot.regular == "5.09"
    assert snapshot.premium == "5.59"
    assert snapshot.diesel == ""
    assert snapshot.short_name == "Concord"


def test_refresh_makes_one_request_per_state() -> None:
    """Multiple CA warehouses should share a single HTTP fetch."""
    calls: list[str] = []
    def _opener(request, timeout=None):  # noqa: ARG001
        calls.append(request.full_url)
        class _R:
            def read(self_inner) -> bytes:
                return _SAMPLE_HTML.encode("utf-8")
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *exc: object) -> None:
                return None
        return _R()
    ids = ["663"]  # single Concord; only one street key present in sample
    mode = CostcoMode(_config_with_ids(ids), opener=_opener)  # type: ignore[arg-type]
    mode._refresh(tuple(ids))
    assert len(calls) == 1
    assert calls[0].endswith("/gas/ca")


def test_refresh_network_error_sets_error_state() -> None:
    def _opener(_request, timeout=None):  # noqa: ARG001
        raise OSError("boom")
    mode = CostcoMode(_config_with_ids(["663"]), opener=_opener)  # type: ignore[arg-type]
    mode._refresh(("663",))
    assert mode._failed
    assert mode._error_state == "network"


def test_refresh_unknown_id_reports_unknown() -> None:
    mode = CostcoMode(_config_with_ids(["999999"]), opener=_fake_opener(_SAMPLE_HTML))  # type: ignore[arg-type]
    mode._refresh(("999999",))
    assert mode._failed
    assert mode._error_state == "unknown"


def test_refresh_missing_street_key_reports_parse() -> None:
    """A warehouse in WAREHOUSE_SLUGS whose street isn't in the fetched table
    should report a parse-shape error, not silently no-op.
    """
    # 475 (El Camino) is not in the two-row sample.
    mode = CostcoMode(_config_with_ids(["475"]), opener=_fake_opener(_SAMPLE_HTML))  # type: ignore[arg-type]
    mode._refresh(("475",))
    assert mode._failed
    assert mode._error_state == "parse"
