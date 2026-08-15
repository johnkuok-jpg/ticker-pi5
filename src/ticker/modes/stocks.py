# MIT License — Copyright (c) 2026 John Kuok
"""Finnhub-backed stock and crypto ticker mode.

Two layouts are available, selected with ``STOCKS_LAYOUT``:

``card`` (default)
    One symbol at a time in large type with a wide intraday chart. Two rows of
    8x16 type on a 32-pixel panel is the largest this display can carry, so it
    is the only layout readable from across a room.

``scroll``
    The classic Times Square marquee, kept for the look of the thing.

Colour follows trading-floor convention: amber for labels and other
non-semantic text, white for the price itself, green and red reserved strictly
for direction so that a glance at colour alone answers "up or down".

Data sources, in preference order:

1. Finnhub (real-time US equities, ~seconds latency) when FINNHUB_API_KEY is set.
2. yfinance / Yahoo Finance as a fallback for any symbol Finnhub can't quote --
   obscure ADRs, some non-US listings -- and for the whole watchlist when no
   key is configured. Yahoo is delayed 15-20 min on US equities but at least
   nothing breaks if the key is missing.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from PIL import Image

from ticker import icons, market
from ticker.canvas import LARGE, SMALL, Canvas
from ticker.modes.base import Mode

_FINNHUB_BASE = "https://finnhub.io/api/v1"
_FINNHUB_TIMEOUT = 5.0


def _finnhub_get(path: str, params: dict[str, str | int], api_key: str) -> dict:
    """One HTTP GET against the Finnhub REST API, returning the parsed JSON.

    Any network error, non-200 status, or JSON parse failure bubbles up as an
    exception so the caller can decide whether to fall back or drop the symbol.
    Timeout is short (5 s) because a stock ticker that hangs on a slow request
    for a whole minute is worse than one that reuses the previous quote.
    """
    query = urllib.parse.urlencode({**params, "token": api_key})
    url = f"{_FINNHUB_BASE}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "ticker-pi5/1.0"})
    with urllib.request.urlopen(req, timeout=_FINNHUB_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _finnhub_symbol(symbol: str) -> str:
    """Map a watchlist symbol to Finnhub's expected symbol format.

    yfinance-style suffixes:
      ``BTC-USD``  -> ``BINANCE:BTCUSDT``   (crypto via Finnhub's Binance feed)
      ``ETH-USD``  -> ``BINANCE:ETHUSDT``
      ``^GSPC``    -> unsupported on the free tier; caller will fall back
    Plain US equities like ``AAPL`` / ``NVDA`` pass through unchanged.
    """
    upper = symbol.upper()
    if upper.endswith("-USD") and "-" in upper:
        base = upper.split("-", 1)[0]
        # USDT is the deepest Binance pairing; USD-quoted spot pairs are much
        # thinner and often return no ticks for hours.
        return f"BINANCE:{base}USDT"
    return upper


def _finnhub_quote(symbol: str, api_key: str) -> Quote | None:
    """Fetch one quote via Finnhub; return None if the endpoint has no data.

    Finnhub's ``/quote`` returns 200 with all-zero fields for unknown symbols
    rather than a 404, so we treat previous_close == 0 (or price == 0) as
    "unknown" and let the caller fall back.

    Intraday candles come from ``/stock/candle`` (equities) or
    ``/crypto/candle`` (Binance:*). The free tier caps candle history at ~1
    year but the last day at 5-minute resolution -- which is what we want --
    is included.
    """
    fh_symbol = _finnhub_symbol(symbol)
    quote_json = _finnhub_get("/quote", {"symbol": fh_symbol}, api_key)
    # Finnhub /quote shape: c=current, pc=previous close, h/l/o=day HLO,
    # t=unix timestamp of the last trade. 0 on all fields means "symbol not
    # covered" per their support docs.
    current = float(quote_json.get("c") or 0.0)
    previous_close = float(quote_json.get("pc") or 0.0)
    if current <= 0 or previous_close <= 0:
        return None

    intraday: list[float] = []
    # 24h of 5-minute candles: 288 samples, well under Finnhub's response
    # size limits. `to` = now, `from` = 26 hours back so a stock opening at
    # 9:30 ET still has premarket + intraday context.
    now = int(time.time())
    candle_path = "/crypto/candle" if fh_symbol.startswith("BINANCE:") else "/stock/candle"
    try:
        candles = _finnhub_get(
            candle_path,
            {"symbol": fh_symbol, "resolution": 5, "from": now - 26 * 3600, "to": now},
            api_key,
        )
        if candles.get("s") == "ok":
            intraday = [float(v) for v in candles.get("c", []) if v]
    except Exception:
        # Intraday chart is decoration -- a quote with no sparkline still
        # renders the price and percent correctly.
        intraday = []

    # If we did get intraday data, prefer its most recent close over /quote's
    # `c` field. In practice they agree, but the candle close is guaranteed to
    # match the sparkline endpoint so the sparkline and the printed price can
    # never drift by one refresh interval.
    if intraday:
        current = intraday[-1]

    return Quote(symbol, current, previous_close, intraday)

AMBER = (255, 176, 0)
WHITE = (235, 240, 250)
GREEN = (40, 230, 90)
RED = (255, 70, 70)
GREEN_FILL = (10, 66, 28)
RED_FILL = (74, 18, 18)
GREY = (62, 72, 92)
DIM = (96, 108, 132)


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

    Delegates to :mod:`ticker.market`, which carries the NYSE holiday and
    early-close calendar, so the two-pixel stripe on this screen and the market
    clock screen can never disagree with each other.
    """
    state = market.session_state(now)
    return state.label, state.color


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

    # Refresh cadence. Finnhub's free tier is 60 req/min, so a 15-second
    # cadence over a small watchlist (5-10 tickers) is well under budget.
    # Yahoo Finance rate limits are looser at this frequency too, but if we
    # ever fall back to yfinance the refresh call is still bounded by
    # its own network timeouts and won't compound.
    CACHE_SECONDS = 15
    CARD_SECONDS = 6

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.quotes: list[Quote] = []
        self._last_refresh = 0.0
        self._watched: tuple[str, ...] = ()

    # -- data ----------------------------------------------------------------

    def _refresh(self) -> None:
        """Poll one quote per symbol.

        Try Finnhub first when a key is configured. For any symbol Finnhub
        can't cover (returns all-zero fields, HTTP error, or an unsupported
        format like the ``^GSPC`` index), fall back to yfinance so the ticker
        keeps working end-to-end. If Finnhub fails wholesale (network down,
        key revoked), the whole watchlist goes to yfinance for this pass and
        the next refresh will retry Finnhub -- no sticky failure state.
        """
        api_key = getattr(self.config, "finnhub_api_key", "") or ""
        symbols = list(self.config.current_symbols())
        quotes: list[Quote] = []
        finnhub_healthy = bool(api_key)
        yahoo_fallback_symbols: list[str] = []

        for symbol in symbols:
            got: Quote | None = None
            if finnhub_healthy:
                try:
                    got = _finnhub_quote(symbol, api_key)
                except urllib.error.HTTPError as exc:
                    # 401 = bad key; 403 = tier not entitled; 429 = rate limit.
                    # Any of those means "stop trying Finnhub for the rest of
                    # this refresh" -- otherwise we'd burn the quota on every
                    # symbol before falling back.
                    if exc.code in (401, 403, 429):
                        finnhub_healthy = False
                    got = None
                except Exception:
                    # Timeout, DNS, JSON error, etc. Drop this symbol to
                    # Yahoo but keep trying Finnhub for the next ones -- one
                    # weird symbol shouldn't disable the fast path for the
                    # rest of the watchlist.
                    got = None
            if got is not None:
                quotes.append(got)
            else:
                yahoo_fallback_symbols.append(symbol)

        # yfinance fallback -- unchanged from the old implementation, just
        # scoped to symbols Finnhub couldn't cover (or all of them if the key
        # is missing/dead).
        if yahoo_fallback_symbols:
            try:
                import yfinance as yf

                for symbol in yahoo_fallback_symbols:
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
            except Exception:
                # If Yahoo is also down, keep whatever Finnhub gave us. Better
                # a partial refresh than a whole-watchlist blackout.
                pass

        if quotes:
            # Drop any quote whose symbol has since left the watchlist, so a
            # removed ticker stops appearing even if its own fetch failed on
            # this pass and the loop above skipped over it.
            watched = set(self._watched) if self._watched else set(symbols)
            kept = [quote for quote in quotes if quote.symbol in watched]
            self.quotes = kept if kept else quotes
        # Keep the last good values when everything is down.
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
        # A watchlist edit in the web app should show up straight away rather
        # than at the end of the current cache window: waiting up to a minute to
        # see the symbol you just typed reads as the add having failed.
        # Checked about once a second rather than every frame; this is a file
        # read, and no edit needs 30Hz.
        stale = False
        if not self._watched or tick % max(1, self.config.fps) == 0:
            watched = self.config.current_symbols()
            stale = bool(self._watched) and watched != self._watched
            self._watched = watched
        if stale or time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()
        canvas.clear()
        if not self.quotes:
            canvas.scroll_text(12, "MARKETS: WAITING FOR PRICES", DIM, tick * 2, SMALL)
            return

        if self.config.stocks_layout == "scroll":
            self._render_scroll(canvas, tick)
        else:
            self._render_card(canvas, tick)

    # -- card ----------------------------------------------------------------

    def _render_card(self, canvas: Canvas, tick: int) -> None:
        """One symbol, large, with a wide intraday chart.

        If the user has locked the card on a specific symbol via the web app,
        that quote is shown continuously; otherwise the card rotates through
        the watchlist ``CARD_SECONDS`` at a time. If the locked symbol has
        somehow left ``self.quotes`` (removed, not-yet-refreshed after add)
        the rotation resumes rather than showing a blank panel.
        """
        locked_symbol = self.config.current_stocks_lock_symbol()
        quote = None
        if locked_symbol:
            for candidate in self.quotes:
                if candidate.symbol == locked_symbol:
                    quote = candidate
                    break
        if quote is None:
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
