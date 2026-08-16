# MIT License — Copyright (c) 2026 John Kuok
"""Costco gas-station prices.

**Why we don't call costco.com directly.** Costco's own warehouse-locator
endpoint (``www.costco.com/AjaxWarehouseBrowseLookupView``) sits behind
Akamai bot defense and returns HTTP 403/429 from every non-browser client
the author has tried, including a home Pi. Every gas tracker in the wild
(gastrak, costcogaspricelive.com, costcogasprices.com) has moved to their
own crawler + cache; we just piggyback on the biggest one.

**What we hit.** ``www.costcogasprices.com`` is a Next.js SSR site whose
per-station pages already contain the current prices in the initial
HTML -- no JS required -- inside a single ``og:description`` meta tag
that reads::

    Current Costco gas prices at 1600 El Camino Real, SOUTH SAN
    Francisco. Regular: $5.30, Premium: $5.74, Diesel: N/A.
    Updated 8/14/2026, 7:28:22 AM.

One regex against that string gives us the address, city, regular,
premium, and diesel prices for a warehouse. We fetch one URL per
configured warehouse; three warehouses at a one-hour cache means three
requests per hour -- well below any polite-poll threshold.

**How warehouse IDs work.** The user's warehouse IDs (``475`` = SSF El
Camino, ``422`` = SSF S Airport, ...) are Costco's internal ``stlocID``
values; costcogasprices.com routes by street-address slug instead. The
``WAREHOUSE_SLUGS`` table below maps every Bay Area warehouse ID to its
slug (harvested once from the site's California listing). Adding a new
region == extending the table; nothing else changes.

Layout is a two-warehouse rotation (well, up to three) at ~5 s per slide:

    ┌────────────────────────────────────────────────────────────┐
    │ COSTCO       ●○○  El Camino                                │  rows 0-4   red
    │ GASOLINE                                                   │  rows 5-9   blue
    │                                                            │
    │ REG                                                  $5.30 │  rows 11-22 MEDIUM
    │ PREM                                                 $5.74 │  rows 24-31 SMALL
    └────────────────────────────────────────────────────────────┘

Diesel, when the API returns it, replaces PREM as the third row and pushes
PREM up -- most Costco warehouses only publish regular + premium, so a
three-row layout would leave a dead row 90% of the time.

The header is a hand-drawn stacked bitmap of the Costco Gasoline sign --
red ``COSTCO`` directly above blue ``GASOLINE``, both in the same 5-row
tagline face -- because setting it in the panel font produced something
that read as a filename rather than a logo. The older 14-row mark with
blue speed stripes has been retired in favour of a compact 10-row block
so the REG price row can go up to MEDIUM. See the glyph tables below.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

LOGGER = logging.getLogger(__name__)

# CostcoGasPrices.com serves plain SSR HTML at ``/station/us/{slug}``.
STATION_URL_TEMPLATE = "https://www.costcogasprices.com/station/us/{slug}"

REQUEST_TIMEOUT = 10.0
# A realistic desktop Chrome UA. costcogasprices.com is not aggressive
# about UA policing, but keeping the shape realistic reduces the odds a
# future CDN change locks us out.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Costco warehouse ID -> (station-slug, short-name).
#
# The slug is the last path segment on costcogasprices.com. Short-names
# are hand-picked to fit the ~12-char panel window at SMALL 6x8 -- the
# source's city field is often ``SOUTH SAN Francisco``, which title-cases
# to a 19-char string that clips ugly. Where two warehouses share a
# city, we disambiguate by neighborhood/street rather than by ID number
# so the label still reads as a place.
#
# Harvested from the site's California listing. Adding a warehouse is a
# one-line edit here plus (optionally) a matching preset in
# templates/index.html.
WAREHOUSE_SLUGS: dict[str, tuple[str, str]] = {
    "1002": ("2201-verne-roberts-cir", "Antioch"),
    "1662": ("5151-heidorn-ranch-rd", "Brentwood"),
    "663": ("2400-monument-blvd", "Concord"),
    "21": ("3150-fostoria-way", "Danville"),
    "453": ("5101-business-center-dr", "Fairfield"),
    "778": ("43621-pacific-commons-blvd", "Fremont"),
    "760": ("7251-camino-arroyo", "Gilroy"),
    "823": ("22330-hathaway-ave", "Hayward"),         # Hathaway
    "1061": ("28505-hesperian-blvd", "Hesperian"),    # Hayward (Hesperian)
    "146": ("2800-independence-dr", "Livermore"),
    "1679": ("280-riversound-way", "Napa"),
    "1660": ("350-newpark-mall", "Newark"),
    "1341": ("7200-johnson-drive", "Pleasanton"),
    "1042": ("2300-middlefield-rd", "Redwood City"),
    "482": ("4801-central-avenue", "Richmond"),
    "659": ("5901-redwood-dr", "Rohnert Park"),
    "1004": ("1709-automation-pkwy", "SJ Automation"),
    "148": ("2201-senter-rd", "SJ Senter"),
    "848": ("2376-s-evergreen-loop", "SJ Evergreen"),
    "1267": ("6898-raleigh-road", "SJ Raleigh"),
    "118": ("1900-davis-st", "San Leandro"),
    "129": ("1601-coleman-ave", "Santa Clara"),
    "149": ("220-sylvania-ave", "Santa Cruz"),
    "41": ("1900-santa-rosa-ave", "Santa Rosa"),
    "475": ("1600-el-camino-real", "El Camino"),      # South SF
    "422": ("451-s-airport-blvd", "S Airport"),       # South SF
    "423": ("150-lawrence-station-rd", "Sunnyvale"),
    "694": ("1051-hume-way", "Vacaville"),
    "132": ("198-plaza-dr", "Vallejo"),
}

# Panel colors. Costco red is the exact PMS 185 hex the brand book uses;
# on an LED panel it comes out slightly hot but reads as red. The other
# tones match the palette the rest of the modes use, so muscle memory
# carries over.
COSTCO_RED = (227, 24, 55)
# The blue used for the ``WHOLESALE`` tagline on Costco's real logo is a
# navy that reads too dark on the panel; a slightly-warmer sky blue for
# the premium price row keeps the palette honest without going muddy.
COSTCO_BLUE = (90, 170, 255)
# Logo blue for the ``GASOLINE`` tagline and the speed stripes. Costco's
# spec is PMS 286 (~#003DA5), a deep navy that disappears against black
# on an LED panel -- the individual diodes are too small to carry a dark
# colour. This is that navy pushed up in luminance until it survives the
# panel while staying unmistakably the logo blue rather than the sky-blue
# used for the premium price row.
LOGO_BLUE = (52, 104, 235)
WHITE = (235, 240, 250)
DIM = (108, 118, 138)
GREEN = (40, 230, 90)
AMBER = (255, 176, 0)

# One warehouse holds the screen for this many seconds before the rotation
# advances. Five seconds is enough to read regular + premium comfortably
# without the panel feeling stuck. Mirrors the crypto/stocks card cadence.
SLIDE_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Costco Gasoline wordmark
# ---------------------------------------------------------------------------
# Spleen (the panel font) is a thin programmer's typeface -- typing
# ``COSTCO`` in it looks like a filename, not a logo. The real Costco
# Gasoline sign is a heavy rounded sans in red with a smaller blue
# ``GASOLINE`` tucked under its right shoulder, plus blue speed stripes
# filling the space under the left. None of that survives a font
# substitution, so the mark is hand-drawn.
#
# COSTCO used to be a 6x8 hand-drawn mark stacked over GASOLINE with blue
# speed stripes filling the left shoulder. That version dominated the card
# and left only two 8px price rows at the bottom. The user asked for a
# smaller wordmark and larger prices, so both marks now live in the same
# 5-row tagline face: COSTCO in red on top, GASOLINE in blue directly
# under it, no stripes, freeing 12 rows below the header for a MEDIUM
# (12-tall) REG price row. PREM stays SMALL because a second 12-row block
# would run off the panel.
#
# ``1`` lights a pixel, ``0`` leaves it black. Glyphs are 5 rows tall to
# match ``GASOLINE`` and 4-5 columns wide with 1px kerning so the whole
# ``COSTCO`` word ends up roughly the same width as ``GASOLINE`` -- a
# little narrower on purpose so the red wordmark reads as ``sits inside``
# GASOLINE rather than stretching past it.
_LOGO_GLYPHS_5x5: dict[str, tuple[str, ...]] = {
    "C": (
        "0111",
        "1000",
        "1000",
        "1000",
        "0111",
    ),
    "O": (
        "0110",
        "1001",
        "1001",
        "1001",
        "0110",
    ),
    "S": (
        "0111",
        "1000",
        "0110",
        "0001",
        "1110",
    ),
    "T": (
        "1111",
        "0110",
        "0110",
        "0110",
        "0110",
    ),
}

# Tagline face for ``GASOLINE``. Letters are 5 rows tall and mostly 3px
# wide, drawn with a 1px gap between them. The first attempt used a 4px
# body on a gapless 4px grid to buy stroke weight, but with no gap the
# round letters fused into their neighbours -- ``SO`` became one blob and
# the word read as "GAEOLIME". Narrower letters plus real separation is
# far more legible at this size than fatter letters that touch.
#
# ``N`` is the one 4px-wide letter. At 3px there is no room for a
# diagonal, so it renders as a bar-and-two-stems shape indistinguishable
# from ``M``; the 4th column buys the diagonal that makes it an ``N``.
# ``I`` carries top and bottom serifs so it fills its slot -- a bare
# centre stem reads as a word break.
_LOGO_GLYPHS_TAG: dict[str, tuple[str, ...]] = {
    "G": ("111", "100", "101", "101", "111"),
    "A": ("111", "101", "111", "101", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "O": ("111", "101", "101", "101", "111"),
    "L": ("100", "100", "100", "100", "111"),
    "I": ("111", "010", "010", "010", "111"),
    "N": ("1001", "1101", "1011", "1001", "1001"),
    "E": ("111", "100", "111", "100", "111"),
}

# Rendered geometry, all relative to the wordmark's top-left origin.
_LOGO_TEXT = "COSTCO"
_TAG_TEXT = "GASOLINE"
_LOGO_KERN = 1             # 1px gap between ``COSTCO`` letters
_LOGO_WIDTH = (
    sum(len(_LOGO_GLYPHS_5x5[c][0]) for c in _LOGO_TEXT)
    + _LOGO_KERN * (len(_LOGO_TEXT) - 1)
)                                                            # 29px
_TAG_KERN = 1              # 1px gap between tagline letters
_TAG_WIDTH = (
    sum(len(_LOGO_GLYPHS_TAG[c][0]) for c in _TAG_TEXT)
    + _TAG_KERN * (len(_TAG_TEXT) - 1)
)                                                            # 32px
# COSTCO occupies rows 0-4; GASOLINE sits directly under it at row 5. No
# stripes any more -- there is no shoulder to fill now that COSTCO is the
# same tagline face and roughly the same width as GASOLINE.
_TAG_TOP = 5               # ``GASOLINE`` band: rows 5-9
_LOGO_HEIGHT = _TAG_TOP + 5                                  # 10 rows total


@dataclass(frozen=True)
class WarehousePrices:
    """One warehouse's snapshot.

    Prices are stored as strings because the upstream renders them as
    ``$5.30`` and re-formatting to float would risk dropping meaningful
    digits. Missing grades come through empty. ``short_name`` is the
    panel-optimised label from ``WAREHOUSE_SLUGS`` (e.g. ``El Camino``,
    ``S Airport``) -- falls back to the title-cased upstream city when
    the warehouse isn't in the table.
    """

    warehouse_id: str
    city: str
    location_name: str
    regular: str
    premium: str
    diesel: str
    short_name: str = ""

    @property
    def display_city(self) -> str:
        if self.short_name:
            return self.short_name
        # ``city`` comes through in the source's shouty case (``SOUTH SAN
        # Francisco``); title-case so the panel doesn't yell.
        return self.city.strip().title() or self.location_name.strip()

    @property
    def has_prices(self) -> bool:
        return bool(self.regular or self.premium or self.diesel)


def _build_station_request(slug: str) -> urllib.request.Request:
    """Build the SSR-page GET for one warehouse."""
    return urllib.request.Request(
        STATION_URL_TEMPLATE.format(slug=slug),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


# One regex against the ``og:description`` meta tag pulls address, city,
# and all three prices at once. Diesel comes through as ``$5.29`` when
# available or literal ``N/A`` when the station doesn't sell diesel.
_OG_PATTERN = re.compile(
    r'<meta\s+property="og:description"\s+content="'
    r'Current Costco gas prices at ([^,]+),\s*([^\.]+?)\.\s*'
    r'Regular:\s*(\$[\d.]+|N/A),\s*'
    r'Premium:\s*(\$[\d.]+|N/A),\s*'
    r'Diesel:\s*(\$[\d.]+|N/A)\.',
    re.IGNORECASE,
)


def _parse_station_page(warehouse_id: str, html: str) -> WarehousePrices | None:
    """Extract a ``WarehousePrices`` from one station-page HTML.

    Returns ``None`` when the og:description tag doesn't match -- the
    site has never rendered a station page without one but a schema
    change would show up here and we'd rather skip that warehouse than
    render garbage.
    """
    match = _OG_PATTERN.search(html)
    if not match:
        return None
    address, city, regular, premium, diesel = (part.strip() for part in match.groups())
    entry = WAREHOUSE_SLUGS.get(warehouse_id)
    short_name = entry[1] if entry else ""
    return WarehousePrices(
        warehouse_id=warehouse_id,
        city=city,
        location_name=address,
        regular="" if regular.upper() == "N/A" else regular.lstrip("$"),
        premium="" if premium.upper() == "N/A" else premium.lstrip("$"),
        diesel="" if diesel.upper() == "N/A" else diesel.lstrip("$"),
        short_name=short_name,
    )


def _format_price(value: str) -> str:
    """Render a price string for the panel: ``5.199`` -> ``$5.199``.

    Costco publishes three-decimal cash prices; we keep all three digits
    because the trailing 9 is meaningful (every US station posts it) and
    dropping it would make Costco prices read as suspiciously round.
    Empty strings return an em-dash placeholder so the row keeps its
    label column intact.
    """
    text = value.strip()
    if not text:
        return "—"
    if text.startswith("$"):
        return text
    return f"${text}"


class CostcoMode(Mode):
    """Rotates through Costco warehouses showing regular / premium / diesel."""

    # Costco updates prices roughly daily and the endpoint is not free from
    # Akamai's perspective -- polling too fast risks getting IP-throttled.
    # One hour is generous but comfortably above whatever bot threshold
    # exists, and matches the cadence of every public tracker.
    CACHE_SECONDS = 60 * 60
    # After a failed fetch, back off for five minutes rather than the full
    # cache window; a transient DNS hiccup shouldn't leave the panel stale
    # for an hour.
    ERROR_BACKOFF_SECONDS = 5 * 60

    def __init__(self, config, opener=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._opener = opener or urllib.request.urlopen
        self._prices: dict[str, WarehousePrices] = {}
        self._last_refresh = -1e9
        self._failed = False
        # Warehouses the last refresh was scoped to, so a webapp edit that
        # adds a new ID invalidates the cache immediately rather than
        # waiting for the hour-long window to elapse.
        self._last_ids: tuple[str, ...] = ()

    # -- data ---------------------------------------------------------------

    def _fetch_one(self, warehouse_id: str) -> WarehousePrices | None:
        """Fetch one warehouse's snapshot from costcogasprices.com.

        Returns ``None`` on any failure (unknown ID, network error, HTTP
        error, missing og:description). The caller records the outcome
        per-warehouse so one warehouse having stale prices doesn't
        force us to retry every warehouse on the next tick.
        """
        entry = WAREHOUSE_SLUGS.get(warehouse_id)
        if entry is None:
            LOGGER.debug("costco: no slug mapping for warehouse %s", warehouse_id)
            return None
        slug, _ = entry
        request = _build_station_request(slug)
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            LOGGER.warning(
                "costco: station %s (%s) fetch failed: %s", warehouse_id, slug, error
            )
            return None
        snapshot = _parse_station_page(warehouse_id, body)
        if snapshot is None:
            LOGGER.warning(
                "costco: station %s (%s) had no parseable og:description",
                warehouse_id, slug,
            )
        return snapshot

    def _refresh(self, ids: tuple[str, ...]) -> None:
        # Record ``_last_ids`` and ``_last_refresh`` up front so we do not
        # retry the whole set on every render if one warehouse errors --
        # the old locator path shared this bug and buried the logs in a
        # 30-request-per-second retry storm.
        self._last_ids = ids
        self._last_refresh = time.monotonic()
        collected: dict[str, WarehousePrices] = {}
        for warehouse_id in ids:
            snapshot = self._fetch_one(warehouse_id)
            if snapshot is not None:
                collected[warehouse_id] = snapshot
        if collected:
            self._prices = collected
            self._failed = False
        else:
            # Keep any prior successful snapshots so the panel keeps
            # rendering yesterday's price rather than an empty card.
            self._failed = True

    # -- render -------------------------------------------------------------

    def render(self, canvas: Canvas, tick: int) -> None:
        # Read the webapp state live on every frame (same convention as
        # currency); a config that ignored edits until service restart
        # would be a confusing, stale card.
        ids = tuple(self.config.current_costco_warehouses())

        age_limit = self.ERROR_BACKOFF_SECONDS if self._failed else self.CACHE_SECONDS
        if time.monotonic() - self._last_refresh >= age_limit or ids != self._last_ids:
            self._refresh(ids)

        canvas.clear()

        rows: list[WarehousePrices] = []
        for warehouse_id in ids:
            snapshot = self._prices.get(warehouse_id)
            if snapshot and snapshot.has_prices:
                rows.append(snapshot)

        if not rows:
            self._render_placeholder(canvas, tick)
            return

        # Slide index is driven by the tick counter rather than wall time.
        # ``tick`` advances one per frame at ``config.fps`` (30 on the Pi),
        # so ``SLIDE_SECONDS * config.fps`` ticks == one warehouse. Using
        # ticks keeps the preview builder (which captures frames at fixed
        # tick strides but essentially zero wall time) in step with the
        # panel -- with monotonic time, the preview froze on a single
        # warehouse because the render loop takes microseconds.
        ticks_per_slide = max(1, int(SLIDE_SECONDS * self.config.fps))
        index = (tick // ticks_per_slide) % len(rows)
        current = rows[index]
        self._render_warehouse(canvas, current, index, len(rows))

    # -- render helpers ------------------------------------------------------

    def _render_placeholder(self, canvas: Canvas, tick: int) -> None:
        # The dim red wordmark keeps the card recognisable while it waits
        # for the first successful fetch. Falling back to a generic text
        # placeholder would let the panel look "broken" on a slow network
        # even though the card itself is fine.
        self._draw_wordmark(canvas, 0, 0, dim=True)
        # Single line on the last row: the dim mark already says COSTCO
        # GASOLINE, so repeating it in the copy wastes the only line we
        # have left under a 14-row wordmark.
        canvas.text(0, _LOGO_HEIGHT + 2, "FETCHING PRICES", DIM, SMALL)

    def _render_warehouse(
        self,
        canvas: Canvas,
        warehouse: WarehousePrices,
        index: int,
        total: int,
    ) -> None:
        # -- header row --------------------------------------------------
        self._draw_wordmark(canvas, 0, 0)
        # The mark is _LOGO_WIDTH wide. Leave a 2px gutter, then draw the
        # position dots (if more than one warehouse) and the city label.
        cursor_x = _LOGO_WIDTH + 2

        if total > 1:
            # Draw the position dots pixel-by-pixel instead of as text --
            # Spleen's bullet glyphs render as chunky rectangles that fight
            # the wordmark for attention. A 2×2 filled square (current) plus
            # a 2×2 outline square (rest) reads clean at this scale.
            dot_size = 2
            dot_gap = 2
            dot_y = 1
            for i in range(total):
                dx = cursor_x + i * (dot_size + dot_gap)
                if i == index:
                    canvas.fill_rect(dx, dot_y, dot_size, dot_size, WHITE)
                else:
                    # Outline: draw a single pixel in the top-left of the
                    # 2×2 slot so unlit slots read as "less than half" of
                    # the current slot without vanishing on the panel.
                    canvas.fill_rect(dx, dot_y + dot_size - 1, dot_size, 1, DIM)
            cursor_x += total * (dot_size + dot_gap) + 2

        # City name in white, SMALL. The wordmark spans rows 0-13, so its
        # optical centre is row 6-7; an 8px-tall SMALL line started at row
        # 3 lands rows 3-10 and centres against it. Starting at row 0
        # instead reads as clipped, because the glyph tops sit flush with
        # the panel edge and compete with the COSTCO crown.
        available = canvas.width - cursor_x
        if available > 0:
            canvas.text(cursor_x, 3, canvas.fit(warehouse.display_city, available, SMALL), WHITE, SMALL)

        # -- price rows --------------------------------------------------
        # Diesel replaces PREM as the second row when present, and PREM
        # slides up to the first row so the highest-volume grade (REG)
        # always shows and the "next up" grade fills the second slot.
        rows_to_render: list[tuple[str, str, tuple[int, int, int]]] = []
        if warehouse.regular:
            rows_to_render.append(("REG", _format_price(warehouse.regular), GREEN))
        if warehouse.premium:
            rows_to_render.append(("PREM", _format_price(warehouse.premium), COSTCO_BLUE))
        if warehouse.diesel:
            rows_to_render.append(("DIES", _format_price(warehouse.diesel), AMBER))

        # Header now ends at row 9 (``_LOGO_HEIGHT`` = 10), so the price
        # block starts at 11 -- 1px gutter, then a 12-tall MEDIUM REG row
        # at rows 11-22, then an 8-tall SMALL PREM/DIES row at rows 24-31.
        # Two MEDIUM rows would need 24 rows and don't fit alongside the
        # header, so the highest-volume grade gets the bigger face and the
        # secondary grade stays SMALL. Labels stay SMALL on both rows so
        # the price -- what the user actually reads at a glance -- gets
        # the emphasis in each row.
        row_layouts: tuple[tuple[int, int], ...] = (
            (_LOGO_HEIGHT + 1, MEDIUM),   # REG:   rows 11-22, MEDIUM (6x12)
            (_LOGO_HEIGHT + 14, SMALL),   # PREM:  rows 24-31, SMALL  (5x8)
        )
        for slot, (label, price, color) in enumerate(rows_to_render[:2]):
            top, font_size = row_layouts[slot]
            # Label baseline lines up with the price glyphs by dropping the
            # SMALL label down inside the MEDIUM row so ascenders align.
            label_top = top + (font_size - SMALL)
            canvas.text(0, label_top, label, DIM, SMALL)
            # Right-flush price so decimal columns line up between rows.
            price_x = canvas.width - 1 - canvas.text_width(price, font_size)
            canvas.text(price_x, top, price, color, font_size)

    def _draw_wordmark(self, canvas: Canvas, x: int, y: int, dim: bool = False) -> None:
        """Draw the compact stacked Costco Gasoline mark.

        Layout, relative to ``(x, y)``::

            rows 0-4    COSTCO      red,  4-5px glyphs, 1px kern (29px)
            rows 5-9    GASOLINE    blue, 3-4px glyphs, 1px kern (32px)

        Both words are left-aligned and share the same 5-row tagline face,
        so the block reads as a single stacked wordmark whose total
        footprint is ``max(_LOGO_WIDTH, _TAG_WIDTH)`` x ``_LOGO_HEIGHT``
        (32 x 10). The blue speed stripes from the old 14-row mark are
        gone -- there is no shoulder gap to fill now that COSTCO has been
        shrunk into the tagline face.

        When ``dim`` is set both inks drop to roughly half luminance so the
        loading frame reads as "not ready yet" rather than as a live card
        that has lost its prices.
        """
        red = (110, 20, 28) if dim else COSTCO_RED
        blue = (26, 52, 116) if dim else LOGO_BLUE

        self._draw_glyphs(
            canvas, x, y, _LOGO_TEXT, _LOGO_GLYPHS_5x5, red, kern=_LOGO_KERN
        )
        self._draw_glyphs(
            canvas, x, y + _TAG_TOP, _TAG_TEXT, _LOGO_GLYPHS_TAG, blue, kern=_TAG_KERN
        )

    @staticmethod
    def _draw_glyphs(
        canvas: Canvas,
        x: int,
        y: int,
        word: str,
        table: dict[str, tuple[str, ...]],
        color: tuple[int, int, int],
        kern: int,
    ) -> None:
        """Blit a hand-drawn word onto the canvas one pixel at a time.

        Each glyph advances by its own width plus ``kern``, so the same
        helper draws the uniform 6px ``COSTCO`` and the mixed 3/4px
        tagline. Unknown characters are skipped rather than raised on, so a
        typo in a caller can't crash the render loop on the panel.
        """
        cursor = x
        for char in word:
            glyph = table.get(char)
            if glyph is None:
                continue
            for row_index, row in enumerate(glyph):
                for col_index, pixel in enumerate(row):
                    if pixel == "1":
                        canvas.fill_rect(cursor + col_index, y + row_index, 1, 1, color)
            cursor += len(glyph[0]) + kern
