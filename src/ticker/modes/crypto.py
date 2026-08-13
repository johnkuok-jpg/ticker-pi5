# MIT License — Copyright (c) 2026 John Kuok
"""Crypto prices from Coinbase's public endpoints.

Equities are frozen nights, weekends and holidays, which is most of the hours a
panel actually spends on a wall. This mode keeps something live on screen in
those hours.

Coinbase Exchange's ``/products/{id}/stats`` needs no key and returns the
24-hour open alongside the last trade, so one request per coin yields both the
price and the change. Kraken can batch several pairs into a single request, but
its ``o`` field is the opening price since UTC midnight rather than a trailing
24 hours; mixing the two would put two different windows behind one percentage
on screen, so it is deliberately not used as a fallback. When the request
fails, the last good values stay up, exactly as the stocks mode does.
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
from ticker.modes.stocks import compact_percent, compact_price

LOGGER = logging.getLogger(__name__)

STATS_URL = "https://api.exchange.coinbase.com/products/{product}/stats"
REQUEST_TIMEOUT = 8.0
USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

AMBER = (255, 176, 0)
WHITE = (235, 240, 250)
GREEN = (40, 230, 90)
RED = (255, 70, 70)
DIM = (96, 108, 132)


@dataclass(frozen=True)
class CryptoQuote:
    symbol: str
    price: float
    open_24h: float

    @property
    def change_percent(self) -> float:
        if not self.open_24h:
            return 0.0
        return (self.price - self.open_24h) / self.open_24h * 100.0

    @property
    def color(self) -> tuple[int, int, int]:
        return GREEN if self.change_percent >= 0 else RED


class CryptoMode(Mode):
    """One row per coin: symbol, price, and 24-hour change."""

    CACHE_SECONDS = 60
    ERROR_BACKOFF_SECONDS = 120
    MAX_ROWS = 3

    def __init__(self, config, opener=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.quotes: list[CryptoQuote] = []
        self._opener = opener or urllib.request.urlopen
        # Far enough back that the first render always fetches.
        self._last_refresh = -1e9
        self._failed = False

    # -- data ----------------------------------------------------------------

    def _fetch_one(self, symbol: str) -> CryptoQuote | None:
        product = f"{symbol}-USD"
        request = urllib.request.Request(
            STATS_URL.format(product=product), headers={"User-Agent": USER_AGENT}
        )
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
            LOGGER.warning("crypto request failed for %s: %s", product, error)
            return None
        try:
            price = float(payload["last"])
            opened = float(payload["open"])
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("crypto payload unusable for %s", product)
            return None
        if price <= 0:
            return None
        return CryptoQuote(symbol, price, opened)

    def _refresh(self) -> None:
        quotes = [
            quote
            for symbol in self.config.crypto_symbols[: self.MAX_ROWS]
            if (quote := self._fetch_one(symbol)) is not None
        ]
        if quotes:
            self.quotes = quotes
            self._failed = False
        else:
            self._failed = True
        self._last_refresh = time.monotonic()

    # -- render --------------------------------------------------------------

    def render(self, canvas: Canvas, tick: int) -> None:
        age_limit = self.ERROR_BACKOFF_SECONDS if self._failed else self.CACHE_SECONDS
        if time.monotonic() - self._last_refresh >= age_limit:
            self._refresh()

        canvas.clear()
        if not self.quotes:
            canvas.scroll_text(12, "CRYPTO: WAITING FOR PRICES", DIM, tick * 2, SMALL)
            return

        rows = self.quotes[: self.MAX_ROWS]
        # Two coins get the larger font; a third only fits at 5x8.
        font = MEDIUM if len(rows) <= 2 else SMALL
        tops = {1: (10,), 2: (2, 18), 3: (1, 12, 23)}[len(rows)]

        for quote, top in zip(rows, tops):
            canvas.text(0, top, quote.symbol, AMBER, font)

            percent = compact_percent(quote.change_percent, 6)
            # One pixel of inset: right-flush type sits under the panel bezel.
            percent_x = canvas.width - 1 - canvas.text_width(percent, font)
            canvas.text(percent_x, top, percent, quote.color, font)

            price_x = canvas.text_width(quote.symbol + " ", font)
            available = percent_x - 2 - price_x
            budget = canvas.max_chars_in(available, font)
            if budget >= 3:
                canvas.text(price_x, top, compact_price(quote.price, budget), WHITE, font)
