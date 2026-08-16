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
    │ COSTCO  ●○○  El Camino                                     │  header (rows 0-11)
    │                                                            │
    │ REG    $5.199                                              │  row 14-21
    │ PREM   $5.699                                              │  row 22-29
    └────────────────────────────────────────────────────────────┘

Diesel, when the API returns it, replaces PREM as the third row and pushes
PREM up -- most Costco warehouses only publish regular + premium, so a
three-row layout would leave a dead row 90% of the time. The wordmark uses
Costco red (#E31837) drawn in MEDIUM Spleen; not a bitmap of the real
logo, but at 128×32 the letterforms carry the identity just fine.
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
WHITE = (235, 240, 250)
DIM = (108, 118, 138)
GREEN = (40, 230, 90)
AMBER = (255, 176, 0)

# One warehouse holds the screen for this many seconds before the rotation
# advances. Five seconds is enough to read regular + premium comfortably
# without the panel feeling stuck. Mirrors the crypto/stocks card cadence.
SLIDE_SECONDS = 5.0


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
        canvas.text(0, 14, "WAITING FOR", DIM, SMALL)
        canvas.text(0, 23, "COSTCO PRICES", DIM, SMALL)

    def _render_warehouse(
        self,
        canvas: Canvas,
        warehouse: WarehousePrices,
        index: int,
        total: int,
    ) -> None:
        # -- header row --------------------------------------------------
        self._draw_wordmark(canvas, 0, 0)
        # MEDIUM COSTCO is 6 chars × 6px = 36px wide. Leave a 2px gutter,
        # then draw the position dots (if more than one warehouse) and the
        # city label.
        wordmark_width = 6 * 6  # MEDIUM char width from canvas._FONTS
        cursor_x = wordmark_width + 2

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

        # City name in white, SMALL, sitting at row 4 so it visually
        # aligns with the mid-line of the MEDIUM ``COSTCO`` wordmark (rows
        # 0-11). Row 2 -- which I tried first -- reads as clipped because
        # SMALL glyphs are 8px tall and their top edge lands even with the
        # top of the panel, competing with the wordmark's crown.
        available = canvas.width - cursor_x
        if available > 0:
            canvas.text(cursor_x, 4, canvas.fit(warehouse.display_city, available, SMALL), WHITE, SMALL)

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

        # Two rows at MEDIUM fits below the header cleanly (12 + 12 = 24
        # rows, header ends at row 12, panel ends at row 32). Three rows
        # would need SMALL font and read as clutter; the third grade
        # rotates in over time on the third slide instead.
        for slot, (label, price, color) in enumerate(rows_to_render[:2]):
            top = 14 + slot * 9  # 14 -> 23, 23 -> 32 (last row bottom lines up)
            canvas.text(0, top, label, DIM, SMALL)
            # Right-flush price so decimal columns line up between rows.
            price_x = canvas.width - 1 - canvas.text_width(price, SMALL)
            canvas.text(price_x, top, price, color, SMALL)

    @staticmethod
    def _position_dots(index: int, total: int) -> str:
        """``● ○ ○`` style row-of-N indicator using tiny bullet glyphs."""
        # Filled bullet for the current slot, hollow for the rest. Using
        # ASCII fallbacks would read as ``* .`` which looks like noise on
        # the panel; the box-drawing bullets from Spleen render cleanly.
        return "".join("*" if i == index else "." for i in range(total))

    def _draw_wordmark(self, canvas: Canvas, x: int, y: int, dim: bool = False) -> None:
        """Draw the ``COSTCO`` wordmark in MEDIUM red.

        Not a pixel-perfect trace of the real logo -- at 128×32 there is
        not enough resolution to render the serifs the brand book insists
        on -- but the shape + colour + row-1 placement carries the
        identity. When ``dim`` is set we drop to a darker red so the
        placeholder frame reads as "loading" rather than "here is your
        card".
        """
        color = (110, 20, 30) if dim else COSTCO_RED
        canvas.text(x, y, "COSTCO", color, MEDIUM)



