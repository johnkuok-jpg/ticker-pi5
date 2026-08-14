# MIT License — Copyright (c) 2026 John Kuok
"""Tests for the stocks watchlist state file."""

from __future__ import annotations

from pathlib import Path

import pytest

from ticker.config import MAX_SYMBOLS, load_config


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text(
        "TICKER_WIDTH=128\nTICKER_HEIGHT=32\nTICKER_SYMBOLS=AAPL,NVDA\n",
        encoding="utf-8",
    )
    return load_config(env)


def test_symbols_default_to_env_when_no_state_file(config) -> None:  # type: ignore[no-untyped-def]
    assert config.current_symbols() == config.symbols


def test_add_and_remove_symbol_round_trip(config) -> None:  # type: ignore[no-untyped-def]
    assert "TSLA" in config.add_symbol("tsla")
    assert "TSLA" not in config.remove_symbol("TSLA")


def test_add_symbol_is_idempotent(config) -> None:  # type: ignore[no-untyped-def]
    config.add_symbol("TSLA")
    assert config.add_symbol("TSLA").count("TSLA") == 1


def test_remove_last_symbol_is_refused(config) -> None:  # type: ignore[no-untyped-def]
    """An empty list resolves back to the env default, so the delete would look
    like it had undone itself and repopulated symbols nobody asked for."""
    config.set_symbols(["AAPL"])
    with pytest.raises(ValueError):
        config.remove_symbol("AAPL")
    assert config.current_symbols() == ("AAPL",)


def test_reject_malformed_symbol(config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        config.add_symbol("not a ticker!")


def test_symbols_accept_exchange_suffix_and_index_caret(config) -> None:  # type: ignore[no-untyped-def]
    """Yahoo's symbol space is wider than plain letters, so shape validation has
    to allow carets, dashes and dotted exchange suffixes through."""
    config.set_symbols(["^GSPC", "BTC-USD", "7203.T"])
    assert config.current_symbols() == ("^GSPC", "BTC-USD", "7203.T")


def test_watchlist_cap_is_enforced(config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        config.set_symbols([f"SYM{n}" for n in range(MAX_SYMBOLS + 1)])


def test_clearing_the_file_falls_back_to_env(config) -> None:  # type: ignore[no-untyped-def]
    config.set_symbols([])
    assert config.current_symbols() == config.symbols
