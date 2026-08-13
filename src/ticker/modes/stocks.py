# MIT License — Copyright (c) 2026 John Kuok
"""Yahoo Finance-backed, cached stock and crypto ticker mode."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ticker.canvas import SMALL, Canvas
from ticker.modes.base import Mode


@dataclass(slots=True)
class Quote:
    symbol: str
    price: float
    change_percent: float


class StocksMode(Mode):
    """Scroll configured symbols, refreshing market data no more than once a minute."""

    CACHE_SECONDS = 60

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.quotes: list[Quote] = []
        self._last_refresh = 0.0

    def _refresh(self) -> None:
        try:
            import yfinance as yf

            quotes: list[Quote] = []
            for symbol in self.config.symbols:
                history = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
                closes = history["Close"].dropna()
                if len(closes) < 2:
                    continue
                previous, latest = float(closes.iloc[-2]), float(closes.iloc[-1])
                change = ((latest - previous) / previous * 100) if previous else 0.0
                quotes.append(Quote(symbol, latest, change))
            if quotes:
                self.quotes = quotes
        except Exception:
            # Keep the previous good values when Yahoo Finance is unavailable/rate limited.
            pass
        finally:
            self._last_refresh = time.monotonic()

    def _logo_for(self, symbol: str) -> Image.Image | None:
        candidates = [path for path in self.config.logos_dir.glob("*.png") if path.stem.upper() == symbol.upper()]
        if not candidates:
            return None
        try:
            return Image.open(candidates[0]).convert("RGBA").resize((16, 16))
        except OSError:
            return None

    def render(self, canvas: Canvas, tick: int) -> None:
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()
        canvas.clear()
        if not self.quotes:
            canvas.scroll_text(12, "MARKETS: WAITING FOR PRICES", (120, 140, 170), tick * 2, SMALL)
            return

        offset = tick * 2
        x = -(offset % max(1, self._row_width(canvas)))
        for _ in range(2):
            for quote in self.quotes:
                label = f"{quote.symbol} {quote.price:,.2f} {quote.change_percent:+.1f}%"
                logo = self._logo_for(quote.symbol)
                if logo:
                    canvas.image(x, 8, logo)
                    x += 18
                color = (40, 230, 90) if quote.change_percent >= 0 else (255, 70, 70)
                canvas.text(x, 12, label, color, SMALL)
                x += canvas.text_width(label, SMALL) + 20

    def _row_width(self, canvas: Canvas) -> int:
        return sum(canvas.text_width(f"{q.symbol} {q.price:,.2f} {q.change_percent:+.1f}%", SMALL) + 38 for q in self.quotes)
