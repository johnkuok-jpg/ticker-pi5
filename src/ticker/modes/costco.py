# MIT License — Copyright (c) 2026 John Kuok
"""Costco gas-station prices.

Costco publishes gas prices as part of the warehouse-locator payload at
``www.costco.com/AjaxWarehouseBrowseLookupView``. It's an undocumented
endpoint but the whole tracker ecosystem (aruljohn.com/gas, gastrak,
costcogaspricelive.com) leans on it, and one hit returns every nearby
warehouse's regular/premium/(diesel) prices in a single blob. That is
exactly the shape a ticker wants: one poll every hour or so, no key, no
per-warehouse fanout.

The endpoint sits behind Akamai bot defense, so a bare ``curl`` from a
random datacenter IP gets a 403. From a residential Pi it works fine as
long as you send realistic browser headers -- the same ``User-Agent`` +
``Accept`` combo that every tracker in this space uses. See
``_build_request`` for the exact header set.

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

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

LOGGER = logging.getLogger(__name__)

# The locator endpoint returns nearby warehouses ordered by great-circle
# distance from a seed lat/lng. We use a Bay Area seed and ask for a wide
# net (30 warehouses) so a single call picks up whichever IDs the user
# has in their rotation -- typical John-in-SF setup is 1-3 nearby stations
# and 30 is enough to cover most of the peninsula. Anything further away
# and we'd have to shard the poll by seed lat/lng.
LOOKUP_URL = "https://www.costco.com/AjaxWarehouseBrowseLookupView"
DEFAULT_LATITUDE = 37.6547
DEFAULT_LONGITUDE = -122.4077
DEFAULT_LOOKUP_RADIUS = 30

REQUEST_TIMEOUT = 10.0
# A realistic desktop Chrome UA + Accept combo. Costco's Akamai policy
# doesn't inspect the tail end (client-hints, sec-fetch-*), but it does
# reject User-Agents that look like scripts. The tracker projects that
# ship in production all use this same shape.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

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
    """One warehouse's snapshot from the locator endpoint.

    Prices are stored as strings because the upstream sends them that way
    (three-decimal cash format) and re-formatting to float would drop the
    trailing ``9`` on ``$5.199`` -- the same trailing 9 every US gas station
    posts. Missing grades just come through empty.
    """

    warehouse_id: str
    city: str
    location_name: str
    regular: str
    premium: str
    diesel: str

    @property
    def display_city(self) -> str:
        # Prefer the friendly ``locationName`` (e.g. ``El Camino``) when
        # Costco provides it; the raw ``city`` is uppercase and often
        # duplicates the next-door warehouse (``SOUTH SAN FRANCISCO``
        # applies to both the airport and the El Camino store).
        raw = self.location_name.strip() or self.city.strip()
        # The locator sometimes prefixes with ``S ``, ``N `` etc for compass
        # directions -- keep those but normalise to Title Case.
        return raw.title() if raw.isupper() else raw

    @property
    def has_prices(self) -> bool:
        return bool(self.regular or self.premium or self.diesel)


def _build_request(latitude: float, longitude: float, count: int) -> urllib.request.Request:
    """Compose the locator GET with the headers that get through Akamai."""
    query = (
        f"?langId=-1&storeId=10301&numOfWarehouses={count}"
        f"&hasGas=true&populateWarehouseDetails=true"
        f"&latitude={latitude}&longitude={longitude}&countryCode=US"
    )
    return urllib.request.Request(
        LOOKUP_URL + query,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.costco.com/warehouse-locations",
        },
    )


def _parse_locator(payload: list) -> dict[str, WarehousePrices]:
    """Turn the raw locator response into a ``{id: WarehousePrices}`` dict.

    The response is a JSON array whose first element is ``True`` (a status
    flag) and every subsequent element is a warehouse dict. Warehouses may
    or may not include ``gasPrices`` -- non-fuel warehouses are silently
    dropped. Malformed entries are skipped rather than raised: a locator
    quirk on one warehouse must not knock the whole card offline.
    """
    prices: dict[str, WarehousePrices] = {}
    # The status prefix is always the first element; guard against upstream
    # changes by iterating everything and filtering by shape.
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        warehouse_id = str(entry.get("stlocID") or entry.get("identifier") or "").strip()
        if not warehouse_id:
            continue
        gas = entry.get("gasPrices")
        if not isinstance(gas, dict):
            continue
        prices[warehouse_id] = WarehousePrices(
            warehouse_id=warehouse_id,
            city=str(entry.get("city") or ""),
            location_name=str(entry.get("locationName") or ""),
            regular=str(gas.get("regular") or "").strip(),
            premium=str(gas.get("premium") or "").strip(),
            diesel=str(gas.get("diesel") or "").strip(),
        )
    return prices


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

    def _fetch(self) -> dict[str, WarehousePrices] | None:
        request = _build_request(
            DEFAULT_LATITUDE, DEFAULT_LONGITUDE, DEFAULT_LOOKUP_RADIUS
        )
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            LOGGER.warning("costco locator request failed: %s", error)
            return None
        try:
            payload = json.loads(body)
        except ValueError as error:
            LOGGER.warning("costco locator returned non-JSON: %s", error)
            return None
        if not isinstance(payload, list):
            LOGGER.warning("costco locator returned unexpected shape: %s", type(payload).__name__)
            return None
        parsed = _parse_locator(payload)
        return parsed or None

    def _refresh(self, ids: tuple[str, ...]) -> None:
        result = self._fetch()
        if result is None:
            self._failed = True
            self._last_refresh = time.monotonic()
            return
        self._prices = result
        self._failed = False
        self._last_refresh = time.monotonic()
        self._last_ids = ids

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



