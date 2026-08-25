# MIT License — Copyright (c) 2026 John Kuok
"""Stocks mode: watchlist-scaled refresh cadence.

``cache_seconds`` is the one piece of ``StocksMode`` that is pure logic with
no network or config-file dependency, so these tests exercise it directly by
poking ``_watched`` rather than going through the full config/env fixture
machinery used elsewhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticker.config import load_config
from ticker.modes.stocks import StocksMode


@pytest.fixture
def stocks_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text("TICKER_WIDTH=128\nTICKER_HEIGHT=32\n", encoding="utf-8")
    return load_config(env)


def _mode_with_watchlist(config, symbols: tuple[str, ...]) -> StocksMode:
    mode = StocksMode(config)
    mode._watched = symbols
    return mode


# ---- cache_seconds scaling -------------------------------------------------


def test_small_watchlist_hits_the_floor(stocks_config) -> None:
    """Anything from 1 up to the Finnhub-budget breakpoint gets the fastest
    allowed cadence, MIN_CACHE_SECONDS -- the "under 5 stocks -> 5s" case."""
    for count in (1, 2, 3, 4, 5):
        mode = _mode_with_watchlist(stocks_config, tuple(f"SYM{i}" for i in range(count)))
        assert mode.cache_seconds == StocksMode.MIN_CACHE_SECONDS, count


def test_cadence_scales_up_with_watchlist_size(stocks_config) -> None:
    """Beyond the floor, cadence tracks watchlist size 1:1 in seconds, so the
    whole list always costs exactly FINNHUB_REQUESTS_PER_MINUTE requests/min."""
    for count in (6, 8, 10, 15, 20, 30):
        mode = _mode_with_watchlist(stocks_config, tuple(f"SYM{i}" for i in range(count)))
        assert mode.cache_seconds == pytest.approx(count), count


def test_very_large_watchlist_keeps_scaling_for_budget_safety(stocks_config) -> None:
    """There is no upper cap -- a big watchlist keeps backing off so it never
    exceeds the Finnhub budget and silently falls back to Yahoo's 15-20
    minute delayed quotes."""
    mode = _mode_with_watchlist(stocks_config, tuple(f"SYM{i}" for i in range(50)))
    assert mode.cache_seconds == pytest.approx(50)


def test_empty_watchlist_uses_idle_cadence(stocks_config) -> None:
    """Nothing to refresh -- avoid dividing the budget across zero symbols."""
    mode = _mode_with_watchlist(stocks_config, ())
    assert mode.cache_seconds == StocksMode.IDLE_CACHE_SECONDS


def test_cadence_never_exceeds_finnhub_budget(stocks_config) -> None:
    """For every watchlist size, watchlist_size / cache_seconds * 60 must stay
    at or under the free-tier requests-per-minute budget -- the whole point
    of scaling cadence by list size in the first place."""
    for count in range(1, 61):
        mode = _mode_with_watchlist(stocks_config, tuple(f"SYM{i}" for i in range(count)))
        requests_per_minute = count / mode.cache_seconds * 60
        assert requests_per_minute <= StocksMode.FINNHUB_REQUESTS_PER_MINUTE + 1e-9, count


def test_cache_seconds_reads_watched_live(stocks_config) -> None:
    """A watchlist edit (via the web UI) should be picked up on the next
    property read, not frozen at construction time -- render() re-derives
    ``_watched`` from config on every call, so cadence must follow suit."""
    mode = StocksMode(stocks_config)
    mode._watched = ("AAPL",)
    assert mode.cache_seconds == StocksMode.MIN_CACHE_SECONDS
    mode._watched = tuple(f"SYM{i}" for i in range(10))
    assert mode.cache_seconds == pytest.approx(10)
