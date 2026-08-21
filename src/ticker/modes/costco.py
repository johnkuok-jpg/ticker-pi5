# MIT License — Copyright (c) 2026 John Kuok
"""Costco gas-station prices.

**Why we don't call costco.com directly.** Costco's own warehouse-locator
endpoint (``www.costco.com/AjaxWarehouseBrowseLookupView``) sits behind
Akamai bot defense and returns HTTP 403/429 from every non-browser client
the author has tried, including a home Pi. Every gas tracker in the wild
(gastrak, costcogaspricelive.com, costcogasprices.com) has moved to their
own crawler + cache; we just piggyback on one of them.

**What we hit.** ``aruljohn.com/gas/{state}`` renders a plain HTML table
with every Costco warehouse in a state on one page, refreshed daily. One
row per warehouse::

    <tr>
      <td>
        <div class="citystate">Concord</div>
        <div class="address">2400 MONUMENT BLVD<br>CONCORD, CA 94520-3105</div>
      </td>
      <td class="dlr ...">5.09</td>   <!-- regular -->
      <td class="dlr ...">5.59</td>   <!-- premium -->
      <td class="dlr ...">--</td>     <!-- diesel, -- when the station has none -->
    </tr>

We parse the table with stdlib ``html.parser``, match rows to warehouses
by street address (which is embedded in the address div), and return one
``WarehousePrices`` per configured ID. One request per state per hour;
for an all-California fleet that is a single HTTP call an hour.

**Prior source.** The module previously called
``costcogasprices.com/station/us/{slug}`` per warehouse. That site's
Vercel deployment was disabled on 2026-08-21 (HTTP 402
``DEPLOYMENT_DISABLED``) and every fetch now fails; ``aruljohn.com`` has
been running the same table since 2022 with no monetisation pressure to
take it down.

**How warehouse IDs work.** The user's warehouse IDs (``475`` = SSF El
Camino, ``422`` = SSF S Airport, ...) are Costco's internal ``stlocID``
values; ``aruljohn.com`` uses street addresses. The ``WAREHOUSE_SLUGS``
table below maps every Bay Area warehouse ID to its street key (the
first line of its address, uppercased) and a short display name. Adding
a new region == extending the table plus (rarely) adding a state to
``_states_for_ids``.

Layout is a two-warehouse rotation (well, up to three) at ~5 s per slide.
The hand-drawn Costco Gasoline wordmark takes the left half of the card,
vertically centred; the right half stacks the location and prices::

    ┌────────────────────────────────────────────────────────────┐
    │                     ●○○  El Camino                     │  rows 0-7   SMALL
    │                                                            │
    │ COSTCO              REGULAR                  $5.30       │  rows 12-19 SMALL
    │ ==== GASOLINE                                              │
    │                     PREMIUM                  $5.74       │  rows 22-29 SMALL
    └────────────────────────────────────────────────────────────┘

Diesel, when the API returns it, replaces PREMIUM as the third row and pushes
PREMIUM off -- most Costco warehouses only publish regular + premium, so a
three-row layout would leave a dead row 90% of the time.

The left column carries a hand-drawn stacked bitmap of the Costco Gasoline
sign -- red ``COSTCO`` in a 6x8 face over blue ``GASOLINE`` in the 5-row
tagline face -- because setting it in the panel font produced something
that read as a filename rather than a logo. The compact 5-over-5 version
lost the visual weight the sign is known for, so we brought the bigger
6x8 ``COSTCO`` back and moved it to the left so the wider wordmark stops
competing with the price row for the same horizontal band.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

LOGGER = logging.getLogger(__name__)

# ArulJohn.com serves one plain-HTML table per state at ``/gas/{state}``.
STATE_URL_TEMPLATE = "https://aruljohn.com/gas/{state}"

REQUEST_TIMEOUT = 10.0
# A realistic desktop Chrome UA. ArulJohn's site is a personal Cloudflare-
# fronted page with no visible bot policing, but a browser-shaped UA is
# cheap insurance against a future CDN rule change.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Costco warehouse ID -> (street-key, short-name, state).
#
# ``street-key`` is the first line of the warehouse's street address in the
# ArulJohn table, uppercased. That's what we match table rows on. Match
# is exact after uppercasing; ArulJohn is consistent enough that fuzzy
# matching is not needed.
#
# ``short-name`` is what appears on the panel -- hand-picked to fit the
# ~12-char SMALL window. The source's ``citystate`` field is often
# ``SOUTH SAN Francisco``, which title-cases to a 19-char string that
# clips ugly. Where two warehouses share a city, we disambiguate by
# neighborhood/street rather than by ID number so the label still reads
# as a place.
#
# ``state`` is the two-letter USPS code used to construct the ArulJohn
# URL. All Bay Area warehouses are ``"ca"``; the field exists so
# out-of-state warehouses can be added without teaching the fetcher.
WAREHOUSE_SLUGS: dict[str, tuple[str, str, str]] = {
    "1002": ("2201 VERNE ROBERTS CIR", "Antioch", "ca"),
    "1662": ("5151 HEIDORN RANCH RD", "Brentwood", "ca"),
    "663":  ("2400 MONUMENT BLVD", "Concord", "ca"),
    "21":   ("3150 FOSTORIA WAY", "Danville", "ca"),
    "453":  ("5101 BUSINESS CENTER DR", "Fairfield", "ca"),
    "778":  ("43621 PACIFIC COMMONS BLVD", "Fremont", "ca"),
    "760":  ("7251 CAMINO ARROYO", "Gilroy", "ca"),
    "823":  ("22330 HATHAWAY AVE", "Hayward", "ca"),         # Hathaway
    "1061": ("28505 HESPERIAN BLVD", "Hesperian", "ca"),     # Hayward (Hesperian)
    "146":  ("2800 INDEPENDENCE DR", "Livermore", "ca"),
    "1679": ("280 RIVERSOUND WAY", "Napa", "ca"),
    "1660": ("350 NEWPARK MALL", "Newark", "ca"),
    "1341": ("7200 JOHNSON DRIVE", "Pleasanton", "ca"),
    "1042": ("2300 MIDDLEFIELD RD", "Redwood City", "ca"),
    "482":  ("4801 CENTRAL AVENUE", "Richmond", "ca"),
    "659":  ("5901 REDWOOD DR", "Rohnert Park", "ca"),
    "1004": ("1709 AUTOMATION PKWY", "SJ Automation", "ca"),
    "148":  ("2201 SENTER RD", "SJ Senter", "ca"),
    "848":  ("2376 S EVERGREEN LOOP", "SJ Evergreen", "ca"),
    "1267": ("6898 RALEIGH ROAD", "SJ Raleigh", "ca"),
    "118":  ("1900 DAVIS ST", "San Leandro", "ca"),
    "129":  ("1601 COLEMAN AVE", "Santa Clara", "ca"),
    "149":  ("220 SYLVANIA AVE", "Santa Cruz", "ca"),
    "41":   ("1900 SANTA ROSA AVE", "Santa Rosa", "ca"),
    "475":  ("1600 EL CAMINO REAL", "El Camino", "ca"),      # South SF
    "422":  ("451 S AIRPORT BLVD", "S Airport", "ca"),       # South SF
    "423":  ("150 LAWRENCE STATION RD", "Sunnyvale", "ca"),
    "694":  ("1051 HUME WAY", "Vacaville", "ca"),
    "132":  ("198 PLAZA DR", "Vallejo", "ca"),
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
# ``COSTCO`` uses a 6-wide, 8-tall hand-drawn face -- heavy strokes and
# rounded corners that read as the real sign at a glance. The earlier
# attempt at a compact 5x5 wordmark stacked over GASOLINE freed rows for
# the price block but lost the visual weight the sign is known for; the
# 6x8 letters are back, and the price block moved to the right column
# instead of stealing rows from the wordmark.
#
# ``1`` lights a pixel, ``0`` leaves it black. The strokes are 2px so the
# glyph body has real presence at panel scale -- a 1px stroke at this
# size looks like any other label on the panel.
_LOGO_GLYPHS_6x8: dict[str, tuple[str, ...]] = {
    "C": (
        "011110",
        "111111",
        "110000",
        "110000",
        "110000",
        "110000",
        "111111",
        "011110",
    ),
    "O": (
        "011110",
        "111111",
        "110011",
        "110011",
        "110011",
        "110011",
        "111111",
        "011110",
    ),
    "S": (
        "011111",
        "111111",
        "110000",
        "111110",
        "011111",
        "000011",
        "111111",
        "111110",
    ),
    "T": (
        "111111",
        "111111",
        "001100",
        "001100",
        "001100",
        "001100",
        "001100",
        "001100",
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
    sum(len(_LOGO_GLYPHS_6x8[c][0]) for c in _LOGO_TEXT)
    + _LOGO_KERN * (len(_LOGO_TEXT) - 1)
)                                                            # 41px
_TAG_KERN = 1              # 1px gap between tagline letters
_TAG_WIDTH = (
    sum(len(_LOGO_GLYPHS_TAG[c][0]) for c in _TAG_TEXT)
    + _TAG_KERN * (len(_TAG_TEXT) - 1)
)                                                            # 32px
# COSTCO occupies rows 0-7 in its local frame; GASOLINE sits below at
# row 9 (a 1-row gap keeps the two words optically separate rather than
# fusing into a single block on an LED panel). GASOLINE is right-aligned
# under COSTCO so the leftover shoulder on the left carries the two
# straight blue speed stripes -- that shoulder + stripes composition is
# what makes the mark read as the real Costco Gasoline sign.
_TAG_TOP = 9               # ``GASOLINE`` band: rows 9-13 (in local frame)
_LOGO_HEIGHT = _TAG_TOP + 5                                  # 14 rows total

# Two straight stripes fill the gap under ``COSTCO``'s left shoulder,
# where the right-aligned ``GASOLINE`` doesn't reach. The real sign slants
# them for a speed effect, but a diagonal drawn across 9px of height
# becomes a visible staircase rather than a line at this resolution, so
# these run flat.
_STRIPE_ROWS = (_TAG_TOP + 1, _TAG_TOP + 3)   # rows 10 and 12 (local frame)
_STRIPE_GAP = 2            # clear pixels between stripe end and ``GASOLINE``

# Horizontal gap between the wordmark's right edge and the right-column
# content. A single pixel would let the price row's "$" sign visually
# fuse with the ``GASOLINE`` tail; 3px is the smallest gap that reads
# as two separate columns on the panel.
_RIGHT_GUTTER = 3


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


def _build_state_request(state: str) -> urllib.request.Request:
    """Build the GET for one state's ArulJohn table page."""
    return urllib.request.Request(
        STATE_URL_TEMPLATE.format(state=state.lower()),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


class _ArulJohnTableParser(HTMLParser):
    """Stream-parse one ArulJohn state page into ``{street_key_upper: prices}``.

    ArulJohn serves a single HTML table whose rows repeat the same shape::

        <tr>
          <td>
            <div class="citystate">Concord</div>
            <div class="address">2400 MONUMENT BLVD<br>CONCORD, CA 94520-3105</div>
          </td>
          <td class="dlr ...">5.09</td>   <!-- regular -->
          <td class="dlr ...">5.59</td>   <!-- premium -->
          <td class="dlr ...">--</td>     <!-- diesel; -- when unsold -->
        </tr>

    We collect the text of each ``div.address`` and the text of every
    ``td.dlr`` in order. Row segmentation is implicit: the address divs
    and the dlr cells arrive in matched groups (one address followed by
    three dlrs), so the parser accumulates them into flat lists and pairs
    them up at the end. That is more forgiving of stray whitespace nodes
    than trying to track ``<tr>`` boundaries, and ArulJohn's markup has
    been stable enough since 2022 that the shape is safe to assume.

    The street key is the first line of the address div's text (before
    the ``<br>``), uppercased. That's what ``WAREHOUSE_SLUGS`` stores.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # ``_capture`` is the current stream we are appending character
        # data to -- one of ``"address"`` or ``"dlr"`` or ``None``. The
        # address div sometimes contains a ``<br>`` which we translate to
        # a literal newline so the street/city split is unambiguous.
        self._capture: str | None = None
        self._address_chunks: list[str] = []
        self._dlr_chunks: list[str] = []
        self.addresses: list[str] = []
        self.dlrs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name: (value or "") for name, value in attrs}
        classes = attr_map.get("class", "").split()
        if tag == "div" and "address" in classes:
            self._capture = "address"
            self._address_chunks = []
        elif tag == "td" and "dlr" in classes:
            self._capture = "dlr"
            self._dlr_chunks = []
        elif tag == "br" and self._capture == "address":
            # Preserve the street/city split as a newline so the caller
            # can grab the first line as the street key without having
            # to know where the city starts.
            self._address_chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._capture == "address":
            self.addresses.append("".join(self._address_chunks).strip())
            self._capture = None
        elif tag == "td" and self._capture == "dlr":
            self.dlrs.append("".join(self._dlr_chunks).strip())
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture == "address":
            self._address_chunks.append(data)
        elif self._capture == "dlr":
            self._dlr_chunks.append(data)


def _parse_state_page(html: str) -> dict[str, tuple[str, str, str]]:
    """Return ``{street_key_upper: (regular, premium, diesel)}`` for a state page.

    Missing prices (``--``) come through as empty strings. Rows whose
    dlrs don't align 3-to-1 with an address are skipped rather than
    silently miscounted -- ArulJohn's shape is stable, so a misalignment
    means the page has changed and we'd rather return nothing than
    render a wrong price against the wrong warehouse.
    """
    parser = _ArulJohnTableParser()
    parser.feed(html)
    parser.close()
    if len(parser.dlrs) != len(parser.addresses) * 3:
        LOGGER.warning(
            "costco: aruljohn table shape drifted -- addresses=%d dlrs=%d",
            len(parser.addresses), len(parser.dlrs),
        )
        return {}
    prices: dict[str, tuple[str, str, str]] = {}
    for i, address in enumerate(parser.addresses):
        # First line before the newline we injected for ``<br>``. Handles
        # single-line addresses too (no newline == whole string).
        street = address.split("\n", 1)[0].strip().upper()
        if not street:
            continue
        regular = _clean_price(parser.dlrs[i * 3])
        premium = _clean_price(parser.dlrs[i * 3 + 1])
        diesel = _clean_price(parser.dlrs[i * 3 + 2])
        prices[street] = (regular, premium, diesel)
    return prices


def _clean_price(cell: str) -> str:
    """Normalise one ArulJohn price cell. ``--`` -> ``""``, ``5.09`` stays."""
    text = cell.strip()
    if not text or text == "--":
        return ""
    return text


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
        # Diagnostic bucket for the placeholder card: one of
        # ``"pending"``  -- no refresh has ever run
        # ``"empty"``    -- no warehouses configured
        # ``"unknown"``  -- every configured ID is missing from WAREHOUSE_SLUGS
        # ``"network"``  -- every fetch raised (DNS, timeout, TLS, 5xx)
        # ``"parse"``    -- fetches succeeded but no og:description found
        # ``"mixed"``    -- some combination of the failure modes above
        # When set to anything other than ``"pending"``/``"empty"`` the
        # placeholder card surfaces a human-readable label instead of the
        # generic ``FETCHING PRICES``, so ``sudo journalctl -u ticker`` is
        # not the first line of defense when the card looks stuck.
        self._error_state: str = "pending"

    # -- data ---------------------------------------------------------------

    def _fetch_state(
        self, state: str
    ) -> tuple[dict[str, tuple[str, str, str]] | None, str]:
        """Fetch one state's ArulJohn table.

        Returns ``(prices_by_street, outcome)`` where ``outcome`` is one
        of ``"ok"``, ``"network"``, ``"parse"``. ``prices_by_street`` is
        the ``{STREET_KEY: (regular, premium, diesel)}`` dict shape used
        by the caller to look up individual warehouses.
        """
        request = _build_state_request(state)
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            LOGGER.warning("costco: state %s fetch failed: %s", state, error)
            return None, "network"
        prices = _parse_state_page(body)
        if not prices:
            LOGGER.warning(
                "costco: state %s page had no parseable warehouse rows", state
            )
            return None, "parse"
        return prices, "ok"

    def _refresh(self, ids: tuple[str, ...]) -> None:
        # Record ``_last_ids`` and ``_last_refresh`` up front so we do not
        # retry the whole set on every render if a fetch errors -- the
        # old locator path shared this bug and buried the logs in a
        # 30-request-per-second retry storm.
        self._last_ids = ids
        self._last_refresh = time.monotonic()
        if not ids:
            # No warehouses configured -- keep any prior snapshots so a
            # briefly-cleared list doesn't wipe the card, but flag the
            # state so the placeholder can say so.
            self._failed = True
            self._error_state = "empty"
            return

        # Bucket the configured IDs by which state's page holds their
        # row. IDs missing from WAREHOUSE_SLUGS have no state, so we
        # bail on them with ``"unknown"`` and don't waste a fetch.
        states_needed: dict[str, list[str]] = {}
        unknown_ids: list[str] = []
        for warehouse_id in ids:
            entry = WAREHOUSE_SLUGS.get(warehouse_id)
            if entry is None:
                unknown_ids.append(warehouse_id)
                continue
            _, _, state = entry
            states_needed.setdefault(state, []).append(warehouse_id)
        for warehouse_id in unknown_ids:
            LOGGER.warning(
                "costco: no mapping for warehouse %s -- add it to "
                "WAREHOUSE_SLUGS or pick a different ID from the preset list",
                warehouse_id,
            )

        # One HTTP request per state, regardless of how many warehouses
        # in that state are configured. All-California fleet == one
        # call per hour.
        collected: dict[str, WarehousePrices] = {}
        outcomes: list[str] = []
        for warehouse_id in unknown_ids:
            outcomes.append("unknown")
        for state, warehouse_ids in states_needed.items():
            state_prices, state_outcome = self._fetch_state(state)
            if state_prices is None:
                # Whole state fetch/parse failed -- all its warehouses
                # inherit the same outcome so the roll-up label reflects
                # the actual failure mode.
                outcomes.extend([state_outcome] * len(warehouse_ids))
                continue
            for warehouse_id in warehouse_ids:
                street_key, short_name, _ = WAREHOUSE_SLUGS[warehouse_id]
                lookup = state_prices.get(street_key.upper())
                if lookup is None:
                    LOGGER.warning(
                        "costco: warehouse %s (%s) not found in %s table -- "
                        "street key may have changed on ArulJohn",
                        warehouse_id, street_key, state,
                    )
                    outcomes.append("parse")
                    continue
                regular, premium, diesel = lookup
                collected[warehouse_id] = WarehousePrices(
                    warehouse_id=warehouse_id,
                    city=short_name,          # short_name is our display city
                    location_name=street_key,  # street address as location
                    regular=regular,
                    premium=premium,
                    diesel=diesel,
                    short_name=short_name,
                )
                outcomes.append("ok")

        if collected:
            self._prices = collected
            self._failed = False
            self._error_state = "pending"  # unused once has_prices renders
            return
        # Keep any prior successful snapshots so the panel keeps
        # rendering yesterday's price rather than an empty card.
        self._failed = True
        # Roll per-warehouse outcomes into a single bucket. If every
        # warehouse hit the same failure mode we surface that; otherwise
        # ``mixed`` covers the ambiguous case so the label isn't
        # actively misleading.
        distinct_failures = {o for o in outcomes if o != "ok"}
        if len(distinct_failures) == 1:
            self._error_state = next(iter(distinct_failures))
        else:
            self._error_state = "mixed"

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

    # Placeholder labels: each error state maps to a two-word status pair
    # that fits the 12-char SMALL slots the loaded card's REG/PREM rows
    # use, so the placeholder shape stays identical to the real card.
    # The second word carries the color: amber for "look at your config"
    # states, red for network faults, dim otherwise so the wordmark stays
    # the loudest element while the card is still waking up.
    _PLACEHOLDER_LABELS: dict[str, tuple[str, str, tuple[int, int, int]]] = {
        "pending": ("FETCHING", "PRICES", DIM),
        "empty":   ("NO",       "WAREHOUSE", AMBER),
        "unknown": ("UNKNOWN",  "ID",     AMBER),
        "network": ("NO",       "NETWORK", COSTCO_RED),
        "parse":   ("SITE",     "CHANGED", AMBER),
        "mixed":   ("FETCH",    "ERROR",   COSTCO_RED),
    }

    def _render_placeholder(self, canvas: Canvas, tick: int) -> None:
        # The dim red wordmark keeps the card recognisable while it waits
        # for the first successful fetch. Falling back to a generic text
        # placeholder would let the panel look "broken" on a slow network
        # even though the card itself is fine.
        logo_y = (canvas.height - _LOGO_HEIGHT) // 2
        self._draw_wordmark(canvas, 0, logo_y, dim=True)
        # Two SMALL lines live on the right-column baselines the loaded
        # card uses for its REG / PREM rows so the layout stays stable
        # when the first fetch lands. The label word is DIM (it's
        # supporting text); the state word gets the color that matches
        # its severity so at a glance you can tell config-fault from
        # network-fault from just-slow.
        label, detail, detail_color = self._PLACEHOLDER_LABELS.get(
            self._error_state, self._PLACEHOLDER_LABELS["pending"]
        )
        right_x = _LOGO_WIDTH + _RIGHT_GUTTER
        canvas.text(right_x, 3, label, DIM, SMALL)
        canvas.text(right_x, canvas.height - SMALL, detail, detail_color, SMALL)

    def _render_warehouse(
        self,
        canvas: Canvas,
        warehouse: WarehousePrices,
        index: int,
        total: int,
    ) -> None:
        # -- left column: wordmark -----------------------------------------
        # 14-row mark vertically centred in a 32-row panel: leaves 9 rows
        # of gutter top and bottom (rows 9-22 hold the mark itself), which
        # is the amount of dark space needed for the red COSTCO block to
        # read as a floating sign instead of a header bar.
        logo_y = (canvas.height - _LOGO_HEIGHT) // 2
        self._draw_wordmark(canvas, 0, logo_y)

        # -- right column: city + prices -----------------------------------
        # Everything after the mark shares a common left edge so the city
        # label and the price labels line up in one vertical column, which
        # keeps the right half from reading as "three unrelated snippets".
        right_x = _LOGO_WIDTH + _RIGHT_GUTTER
        cursor_x = right_x

        # Position dots first when there is more than one warehouse. We
        # keep them on the same top row as the city so they stay glued to
        # the label they annotate, and we avoid drawing them from the
        # font (Spleen's bullet is a chunky rectangle that fights the
        # wordmark for attention at this size).
        if total > 1:
            dot_size = 2
            dot_gap = 2
            dot_y = 1
            for i in range(total):
                dx = cursor_x + i * (dot_size + dot_gap)
                if i == index:
                    canvas.fill_rect(dx, dot_y, dot_size, dot_size, WHITE)
                else:
                    canvas.fill_rect(dx, dot_y + dot_size - 1, dot_size, 1, DIM)
            cursor_x += total * (dot_size + dot_gap) + 2

        # City on the top row, SMALL. Sits at y=0 so it hugs the top
        # bezel and the whole right column has visible top-to-bottom
        # travel from label -> REG -> PREM.
        available = canvas.width - cursor_x
        if available > 0:
            canvas.text(
                cursor_x,
                0,
                canvas.fit(warehouse.display_city, available, SMALL),
                WHITE,
                SMALL,
            )

        # Full grade names -- with GASOLINE right-aligned and the mark
        # pulled left, the right column has ~84px and MEDIUM prices only
        # need ~30px, so REGULAR/PREMIUM/DIESEL (35px/35px/30px at SMALL)
        # fit with a healthy gap and read cleanly at a glance instead of
        # forcing the driver to expand 3-letter shorthands.
        # Diesel replaces PREMIUM as the second row when present, and
        # PREMIUM slides off so the highest-volume grade (REGULAR)
        # always shows and the "next up" grade fills the second slot.
        rows_to_render: list[tuple[str, str, tuple[int, int, int]]] = []
        if warehouse.regular:
            rows_to_render.append(("REGULAR", _format_price(warehouse.regular), GREEN))
        if warehouse.premium:
            rows_to_render.append(("PREMIUM", _format_price(warehouse.premium), COSTCO_BLUE))
        if warehouse.diesel:
            rows_to_render.append(("DIESEL", _format_price(warehouse.diesel), AMBER))

        # Both price rows share one size so REGULAR and PREMIUM read as a
        # matched pair rather than a primary + secondary. SMALL keeps the
        # 32-row card comfortable: city on rows 0-7, price rows at 12-19
        # and 22-29, leaving one row of gutter between them and one row
        # of bottom padding. Going MEDIUM would force the city off the
        # card, which we need when the mode rotates between warehouses.
        row_tops: tuple[int, ...] = (12, 22)
        for slot, (label, price, color) in enumerate(rows_to_render[:2]):
            top = row_tops[slot]
            canvas.text(right_x, top, label, DIM, SMALL)
            # Right-flush price so decimal columns line up between rows.
            price_x = canvas.width - 1 - canvas.text_width(price, SMALL)
            canvas.text(price_x, top, price, color, SMALL)

    def _draw_wordmark(self, canvas: Canvas, x: int, y: int, dim: bool = False) -> None:
        """Draw the hand-drawn Costco Gasoline mark.

        Layout, relative to ``(x, y)``::

            rows 0-7    COSTCO      red,  6px glyphs, 1px kern   (41px)
            rows 10,12  stripes     blue, straight, left shoulder
            rows 9-13   GASOLINE    blue, 3-4px glyphs, right-aligned (32px)

        ``GASOLINE`` is right-aligned under ``COSTCO`` and the stripes fill
        the gap left over on the left, which is how the real sign is
        composed. Total footprint is ``_LOGO_WIDTH`` x ``_LOGO_HEIGHT``
        (41 x 14).

        When ``dim`` is set both inks drop to roughly half luminance so the
        loading frame reads as "not ready yet" rather than as a live card
        that has lost its prices.
        """
        red = (110, 20, 28) if dim else COSTCO_RED
        blue = (26, 52, 116) if dim else LOGO_BLUE

        self._draw_glyphs(
            canvas, x, y, _LOGO_TEXT, _LOGO_GLYPHS_6x8, red, kern=_LOGO_KERN
        )

        # Right-align GASOLINE under COSTCO so the shoulder is on the left.
        tag_x = x + _LOGO_WIDTH - _TAG_WIDTH
        self._draw_glyphs(
            canvas, tag_x, y + _TAG_TOP, _TAG_TEXT, _LOGO_GLYPHS_TAG, blue, kern=_TAG_KERN
        )

        # Stripes run from the mark's left edge to just short of where
        # GASOLINE begins. With the current metrics that is 7px; if the
        # tagline ever grows wide enough to leave no room, skip them rather
        # than drawing a 1-2px stub that reads as a stuck pixel.
        stripe_width = tag_x - x - _STRIPE_GAP
        if stripe_width >= 4:
            for row in _STRIPE_ROWS:
                canvas.fill_rect(x, y + row, stripe_width, 1, blue)

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
