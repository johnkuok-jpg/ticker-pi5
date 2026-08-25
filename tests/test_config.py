# MIT License — Copyright (c) 2026 John Kuok
"""Tests for the stocks watchlist state file."""

from __future__ import annotations

from pathlib import Path

import pytest

from ticker.config import (
    MAX_COSTCO_WAREHOUSES,
    MAX_CURRENCY_PAIRS,
    MAX_SYMBOLS,
    load_config,
)


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


# --- Quake alert settings ---------------------------------------------------
#
# The webapp writes to a small JSON overlay next to the .env-derived defaults.
# These tests pin the round-trip and the validation ranges since the setter is
# the only guard between a mistyped magnitude and a watcher that would happily
# alert on any USGS wobble.


@pytest.fixture
def quake_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text(
        "TICKER_WIDTH=128\nTICKER_HEIGHT=32\n"
        "QUAKE_ALERT_MIN_MAG=3.5\nQUAKE_ALERT_REGION=California\nQUAKE_ALERT_DWELL_SECONDS=120\n",
        encoding="utf-8",
    )
    return load_config(env)


def test_quake_defaults_come_from_env(quake_config) -> None:  # type: ignore[no-untyped-def]
    assert quake_config.current_quake_alert_min_mag() == pytest.approx(3.5)
    assert quake_config.current_quake_alert_region() == "California"
    assert quake_config.current_quake_alert_dwell_seconds() == 120


def test_quake_set_partial_leaves_others_at_env(quake_config) -> None:  # type: ignore[no-untyped-def]
    quake_config.set_quake_alert_settings(min_mag=4.2)
    assert quake_config.current_quake_alert_min_mag() == pytest.approx(4.2)
    # Region and dwell not written; should still resolve from env.
    assert quake_config.current_quake_alert_region() == "California"
    assert quake_config.current_quake_alert_dwell_seconds() == 120


def test_quake_set_all_three_round_trips(quake_config) -> None:  # type: ignore[no-untyped-def]
    quake_config.set_quake_alert_settings(min_mag=5.0, region="Japan", dwell_seconds=300)
    assert quake_config.current_quake_alert_min_mag() == pytest.approx(5.0)
    assert quake_config.current_quake_alert_region() == "Japan"
    assert quake_config.current_quake_alert_dwell_seconds() == 300


def test_quake_worldwide_is_empty_string(quake_config) -> None:  # type: ignore[no-untyped-def]
    quake_config.set_quake_alert_settings(region="")
    assert quake_config.current_quake_alert_region() == ""


def test_quake_min_mag_rejects_below_2_5(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_alert_settings(min_mag=2.0)


def test_quake_min_mag_rejects_above_9_9(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_alert_settings(min_mag=10.5)


def test_quake_min_mag_rejects_non_numeric(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_alert_settings(min_mag="not-a-number")


def test_quake_dwell_rejects_below_15(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_alert_settings(dwell_seconds=5)


def test_quake_dwell_rejects_above_900(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_alert_settings(dwell_seconds=1200)


def test_quake_region_rejects_over_80_chars(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_alert_settings(region="x" * 81)


def test_quake_clearing_state_file_returns_to_env(quake_config) -> None:  # type: ignore[no-untyped-def]
    quake_config.set_quake_alert_settings(min_mag=5.5, region="Japan", dwell_seconds=300)
    quake_config.quake_alert_settings_file.unlink()
    assert quake_config.current_quake_alert_min_mag() == pytest.approx(3.5)
    assert quake_config.current_quake_alert_region() == "California"
    assert quake_config.current_quake_alert_dwell_seconds() == 120


# --- Quake display filter ---------------------------------------------------
#
# Separate from the alert settings: the filter controls what shows up while
# the user is actively viewing the quakes mode. Defaults are hardcoded (4.5 /
# worldwide) rather than env-derived because the filter is a display-only
# convenience and not something a deployment would want to pin.


def test_quake_filter_defaults_when_file_absent(quake_config) -> None:  # type: ignore[no-untyped-def]
    # No filter file yet -> defaults are baked in, independent of the env.
    assert quake_config.current_quake_filter_min_mag() == pytest.approx(4.5)
    assert quake_config.current_quake_filter_region() == ""


def test_quake_filter_round_trip(quake_config) -> None:  # type: ignore[no-untyped-def]
    quake_config.set_quake_filter(min_mag=3.5, region="California")
    assert quake_config.current_quake_filter_min_mag() == pytest.approx(3.5)
    assert quake_config.current_quake_filter_region() == "California"


def test_quake_filter_set_partial_min_mag(quake_config) -> None:  # type: ignore[no-untyped-def]
    quake_config.set_quake_filter(min_mag=5.5)
    assert quake_config.current_quake_filter_min_mag() == pytest.approx(5.5)
    # Region not written -> still default worldwide, not whatever the alert has.
    assert quake_config.current_quake_filter_region() == ""


def test_quake_filter_worldwide_is_empty_string(quake_config) -> None:  # type: ignore[no-untyped-def]
    quake_config.set_quake_filter(region="Japan")
    quake_config.set_quake_filter(region="")
    assert quake_config.current_quake_filter_region() == ""


def test_quake_filter_min_mag_rejects_below_2_5(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_filter(min_mag=2.0)


def test_quake_filter_min_mag_rejects_above_9_9(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_filter(min_mag=10.5)


def test_quake_filter_min_mag_rejects_non_numeric(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_filter(min_mag="nope")


def test_quake_filter_region_rejects_over_80_chars(quake_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        quake_config.set_quake_filter(region="x" * 81)


def test_quake_filter_and_alert_are_independent(quake_config) -> None:  # type: ignore[no-untyped-def]
    # A California-only alert paired with a worldwide display -- the exact use
    # case that motivated splitting the two settings files apart in the first
    # place. Writing one must not touch the other.
    quake_config.set_quake_alert_settings(min_mag=5.0, region="California")
    quake_config.set_quake_filter(min_mag=2.5, region="")
    assert quake_config.current_quake_alert_region() == "California"
    assert quake_config.current_quake_alert_min_mag() == pytest.approx(5.0)
    assert quake_config.current_quake_filter_region() == ""
    assert quake_config.current_quake_filter_min_mag() == pytest.approx(2.5)


def test_quake_filter_clearing_state_file_returns_to_defaults(quake_config) -> None:  # type: ignore[no-untyped-def]
    quake_config.set_quake_filter(min_mag=3.0, region="Japan")
    quake_config.quake_filter_file.unlink()
    assert quake_config.current_quake_filter_min_mag() == pytest.approx(4.5)
    assert quake_config.current_quake_filter_region() == ""


# ---- currency pairs + show-change toggle ---------------------------------


@pytest.fixture
def currency_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    # Deliberately different from the code default so we can tell fallback
    # from an accidental hard-coded three-pair list.
    env.write_text(
        "TICKER_WIDTH=128\nTICKER_HEIGHT=32\nCURRENCY_PAIRS=USD/GBP,EUR/JPY\n",
        encoding="utf-8",
    )
    return load_config(env)


def test_currency_defaults_come_from_env_when_no_state_file(currency_config) -> None:  # type: ignore[no-untyped-def]
    assert currency_config.current_currency_pairs() == (("USD", "GBP"), ("EUR", "JPY"))


def test_currency_add_and_remove_round_trip(currency_config) -> None:  # type: ignore[no-untyped-def]
    currency_config.set_currency_pairs([("USD", "JPY")])
    assert currency_config.current_currency_pairs() == (("USD", "JPY"),)
    currency_config.add_currency_pair("USD/EUR")
    assert currency_config.current_currency_pairs() == (("USD", "JPY"), ("USD", "EUR"))
    currency_config.remove_currency_pair("USD/JPY")
    assert currency_config.current_currency_pairs() == (("USD", "EUR"),)


def test_currency_add_is_idempotent(currency_config) -> None:  # type: ignore[no-untyped-def]
    currency_config.set_currency_pairs([("USD", "JPY")])
    currency_config.add_currency_pair("USD/JPY")
    currency_config.add_currency_pair("usd/jpy")  # case-insensitive
    assert currency_config.current_currency_pairs() == (("USD", "JPY"),)


def test_currency_remove_last_pair_is_refused(currency_config) -> None:  # type: ignore[no-untyped-def]
    currency_config.set_currency_pairs([("USD", "JPY")])
    with pytest.raises(ValueError):
        currency_config.remove_currency_pair("USD/JPY")
    # And the pair is still there afterwards so the state file didn't corrupt.
    assert currency_config.current_currency_pairs() == (("USD", "JPY"),)


def test_currency_rejects_bad_codes(currency_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        currency_config.set_currency_pairs([("US", "JPY")])  # too short
    with pytest.raises(ValueError):
        currency_config.set_currency_pairs([("USDD", "JPY")])  # too long
    with pytest.raises(ValueError):
        currency_config.set_currency_pairs([("US1", "JPY")])  # non-alpha
    with pytest.raises(ValueError):
        currency_config.set_currency_pairs(["USDJPY"])  # missing slash
    with pytest.raises(ValueError):
        currency_config.set_currency_pairs([("USD", "USD")])  # same both sides


def test_currency_cap_is_enforced(currency_config) -> None:  # type: ignore[no-untyped-def]
    # Overflow the cap: MAX_CURRENCY_PAIRS + 1 must always be rejected, no
    # matter what the cap is set to. Build the list from a slice of a bank of
    # valid presets so the test survives a future cap bump without editing.
    presets = [
        ("USD", "JPY"),
        ("USD", "EUR"),
        ("USD", "GBP"),
        ("USD", "CNY"),
        ("USD", "CAD"),
        ("USD", "AUD"),
    ]
    with pytest.raises(ValueError):
        currency_config.set_currency_pairs(presets[: MAX_CURRENCY_PAIRS + 1])
    # Boundary: exactly MAX_CURRENCY_PAIRS is allowed.
    currency_config.set_currency_pairs(presets[:MAX_CURRENCY_PAIRS])
    assert len(currency_config.current_currency_pairs()) == MAX_CURRENCY_PAIRS


def test_currency_clearing_state_file_falls_back_to_env(currency_config) -> None:  # type: ignore[no-untyped-def]
    currency_config.set_currency_pairs([("USD", "JPY")])
    currency_config.currency_pairs_file.unlink()
    assert currency_config.current_currency_pairs() == (("USD", "GBP"), ("EUR", "JPY"))


def test_currency_show_change_defaults_on(currency_config) -> None:  # type: ignore[no-untyped-def]
    # Missing file means "on" so the first upgrade after this toggle lands is
    # invisible -- the panel looks like it did before.
    assert currency_config.current_currency_show_change() is True


def test_currency_show_change_round_trip(currency_config) -> None:  # type: ignore[no-untyped-def]
    currency_config.set_currency_show_change(False)
    assert currency_config.current_currency_show_change() is False
    currency_config.set_currency_show_change(True)
    assert currency_config.current_currency_show_change() is True


def test_currency_flag_mode_defaults_off(currency_config) -> None:  # type: ignore[no-untyped-def]
    # Missing file means "off" -- a fresh upgrade lands on the historical
    # three-row rate board, and the flag layout is strictly opt-in.
    assert currency_config.current_currency_flag_mode() is False


def test_currency_flag_mode_round_trip(currency_config) -> None:  # type: ignore[no-untyped-def]
    currency_config.set_currency_flag_mode(True)
    assert currency_config.current_currency_flag_mode() is True
    currency_config.set_currency_flag_mode(False)
    assert currency_config.current_currency_flag_mode() is False


# ---- costco warehouses ---------------------------------------------------


@pytest.fixture
def costco_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    # A pair of confirmed Bay Area IDs -- deliberately not the default
    # ("475",) so a fallback path isn't confused with a hard-coded default.
    env.write_text(
        "TICKER_WIDTH=128\nTICKER_HEIGHT=32\nCOSTCO_WAREHOUSES=475,422\n",
        encoding="utf-8",
    )
    return load_config(env)


def test_costco_defaults_come_from_env_when_no_state_file(costco_config) -> None:  # type: ignore[no-untyped-def]
    assert costco_config.current_costco_warehouses() == ("475", "422")


def test_costco_add_and_remove_round_trip(costco_config) -> None:  # type: ignore[no-untyped-def]
    costco_config.set_costco_warehouses(["475"])
    assert costco_config.current_costco_warehouses() == ("475",)
    costco_config.add_costco_warehouse("118")
    assert costco_config.current_costco_warehouses() == ("475", "118")
    costco_config.remove_costco_warehouse("475")
    assert costco_config.current_costco_warehouses() == ("118",)


def test_costco_add_is_idempotent(costco_config) -> None:  # type: ignore[no-untyped-def]
    costco_config.set_costco_warehouses(["475"])
    costco_config.add_costco_warehouse("475")
    costco_config.add_costco_warehouse(" 475 ")  # whitespace is trimmed
    assert costco_config.current_costco_warehouses() == ("475",)


def test_costco_remove_last_warehouse_is_refused(costco_config) -> None:  # type: ignore[no-untyped-def]
    costco_config.set_costco_warehouses(["475"])
    with pytest.raises(ValueError):
        costco_config.remove_costco_warehouse("475")
    assert costco_config.current_costco_warehouses() == ("475",)


def test_costco_rejects_bad_ids(costco_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        costco_config.set_costco_warehouses(["abc"])  # not digits
    with pytest.raises(ValueError):
        costco_config.set_costco_warehouses(["475a"])  # mixed
    with pytest.raises(ValueError):
        costco_config.set_costco_warehouses(["475123456789"])  # too long


def test_costco_cap_is_enforced(costco_config) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        costco_config.set_costco_warehouses(["475", "422", "118", "157"])
    # Boundary: exactly MAX_COSTCO_WAREHOUSES is fine.
    costco_config.set_costco_warehouses(["475", "422", "118"])
    assert len(costco_config.current_costco_warehouses()) == MAX_COSTCO_WAREHOUSES


def test_costco_clearing_state_file_falls_back_to_env(costco_config) -> None:  # type: ignore[no-untyped-def]
    costco_config.set_costco_warehouses(["118"])
    costco_config.costco_warehouses_file.unlink()
    assert costco_config.current_costco_warehouses() == ("475", "422")


def test_costco_dedupes_preserves_first_occurrence(costco_config) -> None:  # type: ignore[no-untyped-def]
    # "475" appears twice with whitespace jitter; only the first survives,
    # and the fresh "118" tag lands after it so users can predict order.
    costco_config.set_costco_warehouses(["475", "118", " 475"])
    assert costco_config.current_costco_warehouses() == ("475", "118")


# --- Mode-file atomicity ---------------------------------------------------
#
# Regression: set_mode() used Path.write_text(), which truncates first then
# writes. The renderer polls current_mode() once a second, and the web app
# reads it on every /api/state poll. A read that landed between the truncate
# and the write got an empty file, current_mode() fell back to DEFAULT_MODE
# ("weather"), and /api/state told every phone the panel was in weather --
# which the settings page uses to switch the visible card. From the user's
# seat: they were editing commute settings, and the whole page snapped to
# weather for one poll interval, then snapped back.
#
# The fix is tmp+rename, the same pattern set_hidden_modes / set_focus_state /
# set_worldclock_view already use.


def test_set_mode_never_writes_bytes_directly_to_the_mode_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """set_mode() MUST NOT call write_text/open('w') on the mode file itself.

    Writing bytes straight to mode_file truncates it first, and any reader
    (the renderer polling every second, the web app on every /api/state)
    that lands in that window sees empty -> DEFAULT_MODE ("weather") ->
    every phone snaps its settings card to weather while the user is editing
    a different mode. The safe path is tmp+rename so mode_file is only ever
    swapped as a whole inode.

    This test enforces the invariant structurally: spy on write_text and
    open() and assert that neither ever targets mode_file itself. If a
    future edit reintroduces the truncation write this test fails, not the
    user.
    """
    from dataclasses import replace
    from pathlib import Path
    from ticker import config as config_module

    cfg = replace(config_module.load_config(), state_dir=tmp_path)
    target = cfg.mode_file

    offenders: list[str] = []
    real_write_text = Path.write_text
    real_open = Path.open

    def guarded_write_text(self, data, *args, **kwargs):  # noqa: ANN001, ANN202
        if self == target:
            offenders.append(f"Path.write_text -> {self}")
        return real_write_text(self, data, *args, **kwargs)

    def guarded_open(self, mode="r", *args, **kwargs):  # noqa: ANN001, ANN202
        if self == target and any(m in mode for m in ("w", "a", "x", "+")):
            offenders.append(f"Path.open({mode!r}) -> {self}")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(Path, "open", guarded_open)

    cfg.set_mode("commute")
    cfg.set_mode("weather")

    assert offenders == [], (
        "set_mode() wrote bytes directly to mode_file, which creates a "
        "truncation race window every reader can hit. Use tmp+rename.\n"
        + "\n".join(offenders)
    )
    assert cfg.current_mode() == "weather"


def test_unit_name_defaults_blank(config) -> None:  # type: ignore[no-untyped-def]
    """Single-unit setups set nothing and see the old ticker.local behavior."""
    assert config.unit_name == ""


def test_unit_name_reads_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A gift Pi's .env can label itself, e.g. TICKER_UNIT_NAME=MOM'S TICKER."""
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text("TICKER_UNIT_NAME=MOM'S TICKER\n", encoding="utf-8")
    cfg = load_config(env)
    assert cfg.unit_name == "MOM'S TICKER"


def test_unit_name_is_stripped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text("TICKER_UNIT_NAME=  DAVE'S TICKER  \n", encoding="utf-8")
    cfg = load_config(env)
    assert cfg.unit_name == "DAVE'S TICKER"
