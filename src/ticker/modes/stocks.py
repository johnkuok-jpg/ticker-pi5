# MIT License — Copyright (c) 2026 John Kuok
"""Yahoo Finance-backed stock and crypto ticker mode.

Three layouts are available, selected with ``STOCKS_LAYOUT``:

``board`` (default)
    All configured symbols side by side in fixed columns, nothing moving. A
    desk ticker is glanced at, not read, and a scrolling marquee makes the
    answer to "where is MU" depend on when you happen to look up.

``card``
    One symbol at a time in large type with a wide intraday chart. Best when
    you want detail and can wait a few seconds for the symbol to come round.

``scroll``
    The classic Times Square marquee, kept for the look of the thing.

Colour follows trading-floor convention: amber for labels and other
non-semantic text, white for the price itself, green and red reserved strictly
for direction so that a glance at colour alone answers "up or down".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import time as clock_time
from zoneinfo import ZoneInfo

from PIL import Image

from ticker import icons
from ticker.canvas import LARGE, SMALL, Canvas
from ticker.modes.base import Mode

AMBER = (255, 176, 0)
WHITE = (235, 240, 250)
GREEN = (40, 230, 90)
RED = (255, 70, 70)
GREEN_FILL = (10, 66, 28)
RED_FILL = (74, 18, 18)
GREY = (62, 72, 92)
DIM = (96, 108, 132)

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = clock_time(9, 30)
MARKET_CLOSE = clock_time(16, 0)
PREMARKET_OPEN = clock_time(4, 0)
AFTERHOURS_CLOSE = clock_time(20, 0)


@dataclass(slots=True)
class Quote:
    symbol: str
    price: float
    previous_close: float
    intraday: list[float] = field(default_factory=list)

    @property
    def change(self) -> float:
        return self.price - self.previous_close

    @property
    def change_percent(self) -> float:
        return (self.change / self.previous_close * 100) if self.previous_close else 0.0

    @property
    def up(self) -> bool:
        return self.change > 0

    @property
    def color(self) -> tuple[int, int, int]:
        """Direction colour, with amber for genuinely unchanged.

        An exactly-flat quote must not be green: the arrow glyph would be amber
        while the percent beside it was green, which is the display contradicting
        itself.
        """
        if self.change > 0:
            return GREEN
        if self.change < 0:
            return RED
        return AMBER

    @property
    def fill(self) -> tuple[int, int, int]:
        if self.change > 0:
            return GREEN_FILL
        if self.change < 0:
            return RED_FILL
        return GREY


def market_status(now) -> tuple[str, tuple[int, int, int]]:  # type: ignore[no-untyped-def]
    """Label and colour for the current US market session.

    Weekday clock arithmetic only. Exchange holidays are not tracked, so this
    will claim OPEN on Thanksgiving; it drives a two-pixel status bar, and
    pulling a holiday calendar onto the Pi to colour two pixels is not a
    trade worth making.
    """
    local = now.astimezone(MARKET_TZ)
    if local.weekday() >= 5:
        return "CLOSED", RED
    current = local.time()
    if MARKET_OPEN <= current < MARKET_CLOSE:
        return "OPEN", GREEN
    if PREMARKET_OPEN <= current < MARKET_OPEN:
        return "PRE", AMBER
    if MARKET_CLOSE <= current < AFTERHOURS_CLOSE:
        return "AFTER", AMBER
    return "CLOSED", RED


def compact_price(price: float, max_chars: int) -> str:
    """Format *price* to fit *max_chars*, dropping precision before digits.

    A truncated price is worse than a rounded one: "1,234.5" clipped to
    "1,234." reads as a different number, so decimals are shed first and the
    thousands separator last.
    """
    for candidate in (
        f"{price:,.2f}",
        f"{price:,.1f}",
        f"{price:,.0f}",
        f"{price:.0f}",
    ):
        if len(candidate) <= max_chars:
            return candidate
    return f"{price:.0f}"[:max_chars]


def compact_percent(percent: float, max_chars: int) -> str:
    """Format a percent change, shedding the decimal before the sign."""
    for candidate in (f"{percent:+.2f}%", f"{percent:+.1f}%", f"{percent:+.0f}%"):
        if len(candidate) <= max_chars:
            return candidate
    return f"{percent:+.0f}"[:max_chars]


class StocksMode(Mode):
    """Render configured symbols, refreshing market data at most once a minute."""

    CACHE_SECONDS = 60
    CARD_SECONDS = 6

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.quotes: list[Quote] = []
        self._last_refresh = 0.0

    # -- data ----------------------------------------------------------------

    def _refresh(self) -> None:
        try:
            import yfinance as yf

            quotes: list[Quote] = []
            for symbol in self.config.symbols:
                ticker = yf.Ticker(symbol)
                daily = ticker.history(period="5d", interval="1d", auto_adjust=False)
                closes = daily["Close"].dropna()
                if len(closes) < 2:
                    continue
                previous_close = float(closes.iloc[-2])
                price = float(closes.iloc[-1])

                intraday: list[float] = []
                try:
                    minutes = ticker.history(period="1d", interval="5m", auto_adjust=False)
                    intraday = [float(value) for value in minutes["Close"].dropna()]
                except Exception:
                    # The chart is decoration; a quote without one still renders.
                    intraday = []
                if intraday:
                    price = intraday[-1]

                quotes.append(Quote(symbol, price, previous_close, intraday))
            if quotes:
                self.quotes = quotes
        except Exception:
            # Keep the last good values when Yahoo Finance is down or rate limited.
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

    # -- entry point ---------------------------------------------------------

    def render(self, canvas: Canvas, tick: int) -> None:
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()
        canvas.clear()
        if not self.quotes:
            canvas.scroll_text(12, "MARKETS: WAITING FOR PRICES", DIM, tick * 2, SMALL)
            return

        layout = self.config.stocks_layout
        if layout == "card":
            self._render_card(canvas, tick)
        elif layout == "scroll":
            self._render_scroll(canvas, tick)
        else:
            self._render_board(canvas, tick)

    # -- board ---------------------------------------------------------------

    def _render_board(self, canvas: Canvas, tick: int) -> None:
        """Every symbol at once in fixed columns, with a status stripe on top."""
        _, status_color = market_status(self.config.now())
        # Dimmed to about half: at full brightness a full-width rule reads as a
        # decorative border and competes with the prices for attention.
        canvas.hline(0, tuple(channel // 2 for channel in status_color))  # type: ignore[arg-type]

        count = len(self.quotes)
        gap = 1
        column_width = (canvas.width - gap * (count - 1)) // count

        for index, quote in enumerate(self.quotes):
            x = index * (column_width + gap)
            if index:
                canvas.vline(x - 1, GREY, 2, canvas.height)
            self._draw_column(canvas, quote, x, column_width)

    def _draw_column(self, canvas: Canvas, quote: Quote, x: int, width: int) -> None:
        """One symbol in a narrow column: symbol, price, percent, sparkline.

        Nothing here is bolded. Thickening a 5x8 glyph floods Spleen's slashed
        zero solid, which makes 949.90 read as 949.98 — the price is already the
        brightest thing on the panel and needs no further emphasis.
        """
        limit = canvas.max_chars_in(width - 1, SMALL)

        # Spleen's 5x8 cell leaves its top row blank, so a text row at y=1 still
        # clears the status rule at y=0. Rows are stacked 8 apart, which leaves
        # one spare row above the sparkline.
        canvas.text(x, 1, canvas.fit(quote.symbol, width - 7, SMALL), AMBER, SMALL)
        canvas.sprite(x + width - 6, 3, icons.arrow_for(quote.change), icons.ARROW_PALETTE)
        canvas.text(x, 9, compact_price(quote.price, limit), WHITE, SMALL)
        canvas.text(x, 17, compact_percent(quote.change_percent, limit), quote.color, SMALL)

        if quote.intraday:
            canvas.area_chart(x, 26, width - 1, 6, quote.intraday, quote.color, quote.fill)

    # -- card ----------------------------------------------------------------

    def _render_card(self, canvas: Canvas, tick: int) -> None:
        """One symbol, large, with a wide intraday chart."""
        frames = max(1, self.CARD_SECONDS * self.config.fps)
        quote = self.quotes[(tick // frames) % len(self.quotes)]
        _, status_color = market_status(self.config.now())

        # Two-pixel status stripe down the left edge: session state without
        # spending any of the 16 characters a row of large type allows.
        canvas.vline(0, status_color)
        canvas.vline(1, status_color)

        left = 4
        percent = compact_percent(quote.change_percent, 6)
        percent_x = canvas.width - canvas.text_width(percent, LARGE)
        canvas.text(percent_x, 0, percent, quote.color, LARGE)
        canvas.sprite(percent_x - 7, 6, icons.arrow_for(quote.change), icons.ARROW_PALETTE)

        # Fit the symbol to whatever the percent and arrow leave behind, so a
        # long symbol like BTC-USD is not clipped to BTC-U.
        canvas.text(left, 0, canvas.fit(quote.symbol, percent_x - 8 - left, LARGE), AMBER, LARGE)

        price = compact_price(quote.price, 7)
        canvas.text_bold(left, 17, price, WHITE, LARGE)
        chart_x = left + canvas.text_bold_width(price, LARGE) + 4

        if quote.intraday and chart_x < canvas.width - 8:
            canvas.area_chart(
                chart_x,
                18,
                canvas.width - chart_x,
                14,
                quote.intraday,
                quote.color,
                quote.fill,
                baseline=quote.previous_close,
                baseline_color=GREY,
            )

    # -- scroll --------------------------------------------------------------

    def _render_scroll(self, canvas: Canvas, tick: int) -> None:
        """The original marquee, recoloured so each field reads separately."""
        offset = tick * 2
        period = self._scroll_period(canvas)
        x = -(offset % period)
        for _ in range(2):
            for quote in self.quotes:
                x = self._draw_scroll_item(canvas, quote, x)

    def _draw_scroll_item(self, canvas: Canvas, quote: Quote, x: int) -> int:
        logo = self._logo_for(quote.symbol)
        if logo:
            canvas.image(x, 8, logo)
            x += 18
        canvas.text(x, 12, quote.symbol, AMBER, SMALL)
        x += canvas.text_width(quote.symbol, SMALL) + 4

        price = f"{quote.price:,.2f}"
        canvas.text(x, 12, price, WHITE, SMALL)
        x += canvas.text_width(price, SMALL) + 4

        canvas.sprite(x, 14, icons.arrow_for(quote.change), icons.ARROW_PALETTE)
        x += 7

        percent = f"{quote.change_percent:+.2f}%"
        canvas.text(x, 12, percent, quote.color, SMALL)
        return x + canvas.text_width(percent, SMALL) + 14

    def _scroll_period(self, canvas: Canvas) -> int:
        total = 0
        for quote in self.quotes:
            total += canvas.text_width(quote.symbol, SMALL) + 4
            total += canvas.text_width(f"{quote.price:,.2f}", SMALL) + 4
            total += 7
            total += canvas.text_width(f"{quote.change_percent:+.2f}%", SMALL) + 14
            if self._logo_for(quote.symbol):
                total += 18
        return max(1, total)
