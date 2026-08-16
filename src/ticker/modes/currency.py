# MIT License — Copyright (c) 2026 John Kuok
"""Foreign-exchange rates from open.er-api.com.

A public, no-auth endpoint published by exchangerate-api.com. It updates once
an hour and returns every ISO currency keyed off a single base symbol, so one
request covers a whole watchlist. That is the right shape for a ticker: cheap
to poll, forgiving of the network, and no key to rotate.

Layout mirrors the crypto card -- three rows of BASE/QUOTE, rate, and 24-hour
change -- but rates barely move day to day, so the change column reads more as
"is the dollar up or down today" than the pulsing prices you get from crypto.
The mode keeps the last-good rates on screen when a fetch fails, matching how
the crypto and stocks modes recover from a dropped connection.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ticker import flags
from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode
from ticker.modes.stocks import compact_percent

LOGGER = logging.getLogger(__name__)

# The base URL is templated on the base currency (USD, EUR, ...); one hit
# returns every rate against that base.
RATES_URL = "https://open.er-api.com/v6/latest/{base}"
REQUEST_TIMEOUT = 8.0
USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

AMBER = (255, 176, 0)
WHITE = (235, 240, 250)
GREEN = (40, 230, 90)
RED = (255, 70, 70)
DIM = (96, 108, 132)


@dataclass(frozen=True)
class ForexQuote:
    """One currency pair with the current rate and yesterday's rate.

    ``prior`` may be zero when the previous-day snapshot is not yet cached; the
    change_percent property treats that case as "no change" so the row still
    renders instead of dividing by zero.
    """

    base: str
    quote: str
    rate: float
    prior: float

    @property
    def pair_label(self) -> str:
        return f"{self.base}/{self.quote}"

    @property
    def change_percent(self) -> float:
        if not self.prior:
            return 0.0
        return (self.rate - self.prior) / self.prior * 100.0

    @property
    def color(self) -> tuple[int, int, int]:
        # Rising quote = the base currency is buying more of the counter =
        # green ("up"). The stocks/crypto convention on the panel is the same,
        # so muscle memory carries over.
        return GREEN if self.change_percent >= 0 else RED


def _format_rate(rate: float, budget_chars: int) -> str:
    """Render an FX rate to fit *budget_chars* without misleading precision.

    JPY sits around 150, EUR around 0.9, so a single formatter has to handle
    three orders of magnitude gracefully. The rule: prefer three significant
    digits when the pair is < 10 (so 0.865 stays readable), otherwise render
    with 1-2 decimals and clip trailing zeros so 100.20 becomes 100.2.
    """
    if budget_chars < 3:
        return ""  # let the caller drop the column entirely
    if rate <= 0:
        return "-"
    if rate < 10:
        text = f"{rate:.3f}"  # 0.865
    elif rate < 100:
        text = f"{rate:.2f}"  # 45.21
    else:
        text = f"{rate:.1f}"  # 159.2
    # Trim trailing zeros / dot to reclaim characters when the budget is tight.
    if "." in text and len(text) > budget_chars:
        text = text.rstrip("0").rstrip(".")
    if len(text) > budget_chars:
        # Very small budget: drop the decimal point and any tail.
        text = text.split(".")[0][:budget_chars]
    return text


class CurrencyMode(Mode):
    """Rows of BASE/QUOTE, current rate, and 24-hour change."""

    # Rates on this feed refresh hourly, so polling faster is waste. Give it a
    # ten-minute cache so a service restart doesn't smash the endpoint but a
    # user editing pairs on the webapp still sees results within a mode cycle.
    CACHE_SECONDS = 600
    # After a failed fetch, back off for two minutes rather than the full cache
    # window; a transient DNS hiccup shouldn't leave the panel stale for ten.
    ERROR_BACKOFF_SECONDS = 120
    MAX_ROWS = 3
    # Flag mode is always exactly two rows: MEDIUM font, 12x8 flag, then
    # pair label + rate + change on the right. Two rows is a hard cap here
    # rather than a soft ladder so the flag column stays vertically aligned
    # between rows even when the user configures only one pair.
    FLAG_MAX_ROWS = 2
    # Column layout for flag mode -- kept as constants so the tests can
    # assert on the geometry without re-reading render() logic.
    FLAG_X = 0
    FLAG_ROW_TOPS = (2, 18)
    # When the user has only one pair configured we vertically centre that
    # one row rather than parking it against the top edge -- a lone row at
    # y=2 looks like a rendering bug next to the empty half below it.
    FLAG_ROW_TOP_SINGLE = 10
    # Flag is 12 wide; give it 3 pixels of breathing room before the text
    # column so the flag's edge does not blur into a currency letter.
    FLAG_TEXT_X = flags.FLAG_WIDTH + 3
    # The flag itself sits 2 pixels below the text top so its top edge
    # aligns with the cap-height of the MEDIUM font rather than the
    # rendered ascender line.
    FLAG_Y_OFFSET = 2

    def __init__(self, config, opener=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.quotes: list[ForexQuote] = []
        self._opener = opener or urllib.request.urlopen
        # Yesterday's rates, keyed by base currency, so we can compute
        # change_percent without an extra request. Each entry stores
        # {base: (unix_ts_of_snapshot, {quote: rate})}. Refreshed once every
        # 24h; if the snapshot is missing, change_percent falls back to 0.
        self._prior_snapshots: dict[str, tuple[float, dict[str, float]]] = {}
        # Far enough back that the first render always fetches.
        self._last_refresh = -1e9
        self._failed = False
        # Cache the pair list _refresh() was called with so a webapp save
        # invalidates the cache immediately instead of waiting for the
        # 10-minute window to elapse.
        self._last_pairs: tuple[tuple[str, str], ...] = ()

    # -- data ---------------------------------------------------------------

    def _fetch_base(self, base: str) -> dict[str, float] | None:
        """One request per base currency; returns {quote: rate} or None."""
        request = urllib.request.Request(
            RATES_URL.format(base=base), headers={"User-Agent": USER_AGENT}
        )
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
            LOGGER.warning("currency request failed for %s: %s", base, error)
            return None
        if payload.get("result") != "success":
            LOGGER.warning("currency payload not successful for %s: %s", base, payload.get("error-type"))
            return None
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            return None
        # Cast the whole table to floats up front; individual pairs may not be
        # numeric if the upstream ever returns a placeholder.
        clean: dict[str, float] = {}
        for symbol, value in rates.items():
            try:
                clean[str(symbol).upper()] = float(value)
            except (TypeError, ValueError):
                continue
        return clean or None

    def _refresh(self, pairs: tuple[tuple[str, str], ...]) -> None:
        """Fetch every unique base referenced by *pairs*.

        The pair list is passed in so a mid-render edit through the webapp
        picks up on the next refresh cycle -- reading ``self.config`` here
        again would race against the caller.
        """
        bases = sorted({base for base, _ in pairs})
        tables: dict[str, dict[str, float]] = {}
        for base in bases:
            table = self._fetch_base(base)
            if table is None:
                continue
            tables[base] = table

        if not tables:
            self._failed = True
            self._last_refresh = time.monotonic()
            return

        # Promote today's rates into "prior" once every 24h so change_percent
        # tracks a rolling day and not a rolling ten-minute cache. The first
        # snapshot means the very first render shows 0.00% -- the alternative
        # is inventing a fake baseline, which would lie.
        now = time.monotonic()
        for base, table in tables.items():
            snap = self._prior_snapshots.get(base)
            if snap is None or now - snap[0] >= 24 * 60 * 60:
                self._prior_snapshots[base] = (now, dict(table))

        quotes: list[ForexQuote] = []
        for base, quote in pairs:
            table = tables.get(base)
            if not table or quote not in table:
                continue
            snap = self._prior_snapshots.get(base)
            prior = snap[1].get(quote, 0.0) if snap else 0.0
            quotes.append(ForexQuote(base, quote, table[quote], prior))

        if quotes:
            self.quotes = quotes
            self._failed = False
        else:
            self._failed = True
        self._last_refresh = now

    # -- render -------------------------------------------------------------

    def render(self, canvas: Canvas, tick: int) -> None:
        # Read the webapp state live on every render (same convention as
        # stocks); a config that ignored edits until service restart would
        # be a confusing, stale card.
        flag_mode = self.config.current_currency_flag_mode()
        row_cap = self.FLAG_MAX_ROWS if flag_mode else self.MAX_ROWS
        pairs = tuple(self.config.current_currency_pairs()[:row_cap])
        show_change = self.config.current_currency_show_change()

        age_limit = self.ERROR_BACKOFF_SECONDS if self._failed else self.CACHE_SECONDS
        if time.monotonic() - self._last_refresh >= age_limit or pairs != self._last_pairs:
            self._refresh(pairs)
            self._last_pairs = pairs

        canvas.clear()
        if not self.quotes:
            canvas.scroll_text(12, "FX: WAITING FOR RATES", DIM, tick * 2, SMALL)
            return

        if flag_mode:
            self._render_flag_mode(canvas, show_change)
        else:
            self._render_classic(canvas, show_change)

    def _render_classic(self, canvas: Canvas, show_change: bool) -> None:
        """Historical three-row rate board."""
        rows = self.quotes[: self.MAX_ROWS]
        # Same font ladder as crypto: two rows fit MEDIUM cleanly; a third
        # only fits at SMALL. Matches the muscle memory a stocks/crypto user
        # already has.
        font = MEDIUM if len(rows) <= 2 else SMALL
        tops = {1: (10,), 2: (2, 18), 3: (1, 12, 23)}[len(rows)]

        for quote, top in zip(rows, tops):
            canvas.text(0, top, quote.pair_label, AMBER, font)

            if show_change:
                percent = compact_percent(quote.change_percent, 6)
                # One-pixel inset so right-flush type does not tuck under the bezel.
                percent_x = canvas.width - 1 - canvas.text_width(percent, font)
                canvas.text(percent_x, top, percent, quote.color, font)
                rate_x = canvas.text_width(quote.pair_label + " ", font)
                available = percent_x - 2 - rate_x
                budget = canvas.max_chars_in(available, font)
                if budget >= 3:
                    canvas.text(rate_x, top, _format_rate(quote.rate, budget), WHITE, font)
            else:
                # With the change column off, the rate gets the full remaining
                # width and is drawn right-flush -- same visual weight as the
                # % it replaced, and easier to skim than a rate hanging in the
                # middle of the row.
                rate_x_min = canvas.text_width(quote.pair_label + " ", font)
                available = canvas.width - 1 - rate_x_min
                budget = canvas.max_chars_in(available, font)
                if budget >= 3:
                    text = _format_rate(quote.rate, budget)
                    x = canvas.width - 1 - canvas.text_width(text, font)
                    canvas.text(x, top, text, WHITE, font)

    def _render_flag_mode(self, canvas: Canvas, show_change: bool) -> None:
        """Two-row layout with a country flag beside each pair.

        The flag identifies the quote currency (the one the base is buying
        into), because that is the country whose economy the row is
        reporting on, and the label collapses to just the quote code -- the
        flag already carries the country, so a full ``USD/EUR`` label would
        waste the pixels the rate column needs. If a currency has no
        bundled flag, the row falls back to the full ``BASE/QUOTE`` label
        so an unfamiliar pair still identifies itself.
        """
        rows = self.quotes[: self.FLAG_MAX_ROWS]
        tops = (self.FLAG_ROW_TOP_SINGLE,) if len(rows) == 1 else self.FLAG_ROW_TOPS
        for quote, top in zip(rows, tops):
            flag = flags.flag_for(quote.quote)
            if flag is not None:
                flag_rows, palette = flag
                canvas.sprite(self.FLAG_X, top + self.FLAG_Y_OFFSET, flag_rows, palette)
                text_x = self.FLAG_TEXT_X
                label = quote.quote
            else:
                text_x = 0
                label = quote.pair_label

            canvas.text(text_x, top, label, AMBER, MEDIUM)
            label_end = text_x + canvas.text_width(label + " ", MEDIUM)

            if show_change:
                percent = compact_percent(quote.change_percent, 6)
                percent_x = canvas.width - 1 - canvas.text_width(percent, MEDIUM)
                canvas.text(percent_x, top, percent, quote.color, MEDIUM)
                available = percent_x - 2 - label_end
                budget = canvas.max_chars_in(available, MEDIUM)
                if budget >= 3:
                    canvas.text(label_end, top, _format_rate(quote.rate, budget), WHITE, MEDIUM)
            else:
                available = canvas.width - 1 - label_end
                budget = canvas.max_chars_in(available, MEDIUM)
                if budget >= 3:
                    text = _format_rate(quote.rate, budget)
                    x = canvas.width - 1 - canvas.text_width(text, MEDIUM)
                    canvas.text(x, top, text, WHITE, MEDIUM)
