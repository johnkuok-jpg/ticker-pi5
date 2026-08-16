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

    def _refresh(self) -> None:
        """Fetch every unique base referenced by the configured pairs."""
        pairs = self.config.currency_pairs[: self.MAX_ROWS]
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
        age_limit = self.ERROR_BACKOFF_SECONDS if self._failed else self.CACHE_SECONDS
        if time.monotonic() - self._last_refresh >= age_limit:
            self._refresh()

        canvas.clear()
        if not self.quotes:
            canvas.scroll_text(12, "FX: WAITING FOR RATES", DIM, tick * 2, SMALL)
            return

        rows = self.quotes[: self.MAX_ROWS]
        # Same font ladder as crypto: two rows fit MEDIUM cleanly; a third
        # only fits at SMALL. Matches the muscle memory a stocks/crypto user
        # already has.
        font = MEDIUM if len(rows) <= 2 else SMALL
        tops = {1: (10,), 2: (2, 18), 3: (1, 12, 23)}[len(rows)]

        for quote, top in zip(rows, tops):
            canvas.text(0, top, quote.pair_label, AMBER, font)

            percent = compact_percent(quote.change_percent, 6)
            # One-pixel inset so right-flush type does not tuck under the bezel.
            percent_x = canvas.width - 1 - canvas.text_width(percent, font)
            canvas.text(percent_x, top, percent, quote.color, font)

            rate_x = canvas.text_width(quote.pair_label + " ", font)
            available = percent_x - 2 - rate_x
            budget = canvas.max_chars_in(available, font)
            if budget >= 3:
                canvas.text(rate_x, top, _format_rate(quote.rate, budget), WHITE, font)
