"""Offline checks for the market clock calendar and the crypto mode."""

from __future__ import annotations

import io
import json
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from ticker import market  # noqa: E402
from ticker.canvas import Canvas  # noqa: E402
from ticker.config import VALID_MODES, Config  # noqa: E402
from ticker.modes import build_mode  # noqa: E402
from ticker.modes.crypto import CryptoMode  # noqa: E402
from ticker.modes.market import MarketMode  # noqa: E402
from ticker.modes.stocks import market_status  # noqa: E402

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {label}: got {got!r} want {want!r}")


def check_true(label, value):
    check(label, bool(value), True)


def et(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=market.MARKET_TZ)


# -- calendar facts, straight off the NYSE page -----------------------------

for day, name in [
    (date(2026, 1, 1), "NEW YEAR"),
    (date(2026, 7, 3), "JULY 4TH"),
    (date(2026, 11, 26), "THANKSGIVING"),
    (date(2027, 7, 5), "JULY 4TH"),
    (date(2027, 12, 24), "CHRISTMAS"),
]:
    check(f"holiday {day}", market.HOLIDAYS.get(day), name)
    check(f"not trading {day}", market.is_trading_day(day), False)

check("2026 holiday count", sum(1 for d in market.HOLIDAYS if d.year == 2026), 10)
check("2027 holiday count", sum(1 for d in market.HOLIDAYS if d.year == 2027), 10)

# July 3 2026 is the observed holiday, so July 4 itself is a Saturday.
check("jul 4 2026 is saturday", date(2026, 7, 4).weekday(), 5)
# 2027 has no separate Christmas Eve early close because the 24th is the holiday.
check("no 2027 xmas eve early close", date(2027, 12, 23) in market.EARLY_CLOSES, False)
check("2026 early closes", sorted(d for d in market.EARLY_CLOSES if d.year == 2026),
      [date(2026, 11, 27), date(2026, 12, 24)])
check("2027 early closes", sorted(d for d in market.EARLY_CLOSES if d.year == 2027),
      [date(2027, 11, 26)])

check("early close time", market.close_time_for(date(2026, 12, 24)), market.EARLY_CLOSE)
check("normal close time", market.close_time_for(date(2026, 12, 23)), market.REGULAR_CLOSE)

# Every holiday must be a weekday: an observed date on a weekend is a data error.
for day in market.HOLIDAYS:
    check_true(f"holiday {day} is a weekday", day.weekday() < 5)

# -- phase classification ---------------------------------------------------

cases = [
    ("regular midday", et(2026, 8, 13, 12, 0), "open", "OPEN"),
    ("just opened", et(2026, 8, 13, 9, 30), "open", "OPEN"),
    ("one minute to close", et(2026, 8, 13, 15, 59), "open", "OPEN"),
    ("at close", et(2026, 8, 13, 16, 0), "after", "AFTER"),
    ("pre market", et(2026, 8, 13, 8, 0), "pre", "PRE"),
    ("pre market opens", et(2026, 8, 13, 7, 0), "pre", "PRE"),
    ("overnight", et(2026, 8, 13, 3, 0), "closed", "CLOSED"),
    ("after hours end", et(2026, 8, 13, 20, 0), "closed", "CLOSED"),
    ("saturday", et(2026, 8, 15, 12, 0), "closed", "WEEKEND"),
    ("sunday", et(2026, 8, 16, 12, 0), "closed", "WEEKEND"),
    ("thanksgiving", et(2026, 11, 26, 12, 0), "closed", "CLOSED"),
    ("good friday", et(2026, 4, 3, 12, 0), "closed", "CLOSED"),
]
for label, when, phase, wordmark in cases:
    state = market.session_state(when)
    check(f"{label} phase", state.phase, phase)
    check(f"{label} label", state.label, wordmark)

check("thanksgiving names holiday", market.session_state(et(2026, 11, 26, 12)).note, "THANKSGIVING")
check("weekend has no note", market.session_state(et(2026, 8, 15, 12)).note, "")

# The old helper claimed OPEN on Thanksgiving. It must not any more.
check("stocks stripe closed on thanksgiving", market_status(et(2026, 11, 26, 12, 0))[0], "CLOSED")
check("stocks stripe open midday", market_status(et(2026, 8, 13, 12, 0))[0], "OPEN")

# Early close: 1:30 pm on Christmas Eve 2026 is after hours, not open.
xmas_eve = market.session_state(et(2026, 12, 24, 13, 30))
check("xmas eve 1:30pm phase", xmas_eve.phase, "after")
check("xmas eve carries note", xmas_eve.note, "EARLY CLOSE 1PM")
check("xmas eve noon open", market.session_state(et(2026, 12, 24, 12, 0)).phase, "open")

# -- countdown and progress -------------------------------------------------

state = market.session_state(et(2026, 8, 13, 12, 0))
check("midday countdown", state.countdown_label, "4H00M LEFT")
check("midday progress", round(state.progress, 4), round((2.5 * 60) / 390, 4))

state = market.session_state(et(2026, 8, 13, 9, 30))
check("open progress is zero", state.progress, 0.0)
state = market.session_state(et(2026, 8, 13, 15, 59))
check_true("late progress near one", 0.99 < state.progress < 1.0)

# Early close compresses the session, so progress must run on the short span.
state = market.session_state(et(2026, 12, 24, 11, 45))
check("early close progress", round(state.progress, 4), round(135 / 210, 4))
check("early close countdown", state.countdown_label, "1H15M LEFT")

check("pre market countdown", market.session_state(et(2026, 8, 13, 8, 45)).countdown_label,
      "OPENS IN 45M")
# Friday after hours must count to Monday, not Saturday.
friday = market.session_state(et(2026, 8, 14, 17, 0))
check("friday evening counts to monday", friday.countdown_label, "OPENS IN 2D16H")
# Wednesday before Thanksgiving 2026 closes early Friday; Thursday is shut.
wed = market.session_state(et(2026, 11, 25, 17, 0))
# Wednesday 5pm to Friday 9:30am is 40.5 hours: Thursday is the holiday.
check("thanksgiving eve skips holiday", wed.countdown_label, "OPENS IN 1D16H")

check("next trading day skips weekend", market.next_trading_day(date(2026, 8, 14)), date(2026, 8, 17))
check("next trading day skips holiday", market.next_trading_day(date(2026, 11, 25)), date(2026, 11, 27))
check("next trading day skips xmas", market.next_trading_day(date(2026, 12, 24)), date(2026, 12, 28))

for seconds, want in [(0, "0S"), (45, "45S"), (60, "1M"), (2520, "42M"),
                      (11520, "3H12M"), (3600, "1H00M"), (230400, "2D16H")]:
    check(f"duration {seconds}", market.format_duration(seconds), want)
check("negative duration", market.format_duration(-5), "0S")

# -- calendar expiry --------------------------------------------------------

check("2026 covered", market.calendar_covers(date(2026, 5, 1)), True)
check("2029 not covered", market.calendar_covers(date(2029, 5, 1)), False)
far = market.session_state(et(2029, 8, 13, 12, 0))
check("2029 still classifies", far.phase, "open")
check("2029 flags missing calendar", far.calendar_known, False)
check("2026 has calendar", market.session_state(et(2026, 8, 13, 12)).calendar_known, True)

# Naive datetimes must not raise.
check("naive datetime handled", market.session_state(datetime(2026, 8, 13, 12, 0)).phase, "open")

# -- market mode rendering --------------------------------------------------

config = Config(width=128, height=32, fps=30, timezone="America/New_York")
mode = MarketMode(config)


def render(mode_obj, tick=0):
    canvas = Canvas(128, 32)
    mode_obj.render(canvas, tick)
    return canvas


canvas = render(mode)
check("market mode renders", canvas.image_buffer is not None, True)
lit = sum(1 for px in canvas.image_buffer.getdata() if any(px))
check_true("market mode lights pixels", lit > 60)

# The rotation must cycle every item, and never raise on any tick.
state = market.session_state(config.now())
items = mode._detail_items(state)
check_true("at least one detail item", len(items) >= 1)
for tick in range(0, 30 * 20, 37):
    render(mode, tick)
PASS += 1

# A holiday state carries the extra row; an unknown year carries the warning.
class FixedConfig(Config):
    fixed = et(2026, 11, 26, 12, 0)

    def now(self):
        return self.fixed


holiday_mode = MarketMode(FixedConfig(width=128, height=32, fps=30))
check("holiday rotation has two items",
      len(holiday_mode._detail_items(market.session_state(et(2026, 11, 26, 12)))), 2)
check("unknown year rotation has warning",
      any("HOLIDAY DATA" in text for text, _ in
          holiday_mode._detail_items(market.session_state(et(2029, 8, 13, 12)))), True)
render(holiday_mode, 0)
render(holiday_mode, 30 * 5)

# -- crypto mode ------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def make_opener(payloads, log=None):
    def opener(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else request
        if log is not None:
            log.append(url)
        for key, body in payloads.items():
            if key in url:
                if body is None:
                    raise OSError("boom")
                return FakeResponse(json.dumps(body).encode())
        raise OSError("unexpected url")
    return opener


good = {
    "BTC-USD": {"open": "63361.58", "last": "63514.07", "high": "1", "low": "1"},
    "ETH-USD": {"open": "1877.84", "last": "1888.48", "high": "1", "low": "1"},
    "SOL-USD": {"open": "150.0", "last": "144.0", "high": "1", "low": "1"},
}

cfg = Config(width=128, height=32, fps=30, crypto_symbols=("BTC", "ETH"))
crypto = CryptoMode(cfg, opener=make_opener(good))
canvas = render(crypto)
check("two quotes fetched", len(crypto.quotes), 2)
check("btc price", round(crypto.quotes[0].price, 2), 63514.07)
check("btc percent sign", crypto.quotes[0].change_percent > 0, True)
check("btc colour green", crypto.quotes[0].color, (40, 230, 90))
lit = sum(1 for px in canvas.image_buffer.getdata() if any(px))
check_true("crypto lights pixels", lit > 80)

# A falling coin must read red.
falling = CryptoMode(Config(width=128, height=32, fps=30, crypto_symbols=("SOL",)),
                     opener=make_opener(good))
render(falling)
check("sol colour red", falling.quotes[0].color, (255, 70, 70))
check("sol percent", round(falling.quotes[0].change_percent, 2), -4.0)

# Three coins still render, at the smaller font.
three = CryptoMode(Config(width=128, height=32, fps=30, crypto_symbols=("BTC", "ETH", "SOL")),
                   opener=make_opener(good))
render(three)
check("three quotes", len(three.quotes), 3)

# A fourth coin is dropped rather than drawn off-panel.
four = CryptoMode(Config(width=128, height=32, fps=30,
                         crypto_symbols=("BTC", "ETH", "SOL", "XRP")),
                  opener=make_opener(good))
render(four)
check("fourth coin dropped", len(four.quotes), 3)

# Network failure must not raise, and must not blank a good screen.
log: list[str] = []
flaky = CryptoMode(cfg, opener=make_opener(good, log))
render(flaky)
before = list(flaky.quotes)
flaky._opener = make_opener({"BTC-USD": None, "ETH-USD": None})
flaky._last_refresh = -1e9
render(flaky)
check("stale quotes retained", flaky.quotes, before)
check("failure flagged", flaky._failed, True)

# Cold start with a dead endpoint shows the waiting message, not a crash.
dead = CryptoMode(cfg, opener=make_opener({"BTC-USD": None, "ETH-USD": None}))
render(dead)
check("no quotes on cold failure", dead.quotes, [])

# Backoff: a failing endpoint must not be hit every frame.
calls: list[str] = []


def counting_opener(request, timeout=None):
    calls.append(request.full_url)
    raise OSError("down")


backoff = CryptoMode(cfg, opener=counting_opener)
for tick in range(120):
    render(backoff, tick)
check("failed endpoint polled once", len(calls), 2)  # one per symbol, once

# Malformed payloads must be ignored, not crash.
for bad in ({}, {"open": "0", "last": "0"}, {"open": "abc", "last": "1"},
            {"last": "1"}, {"open": "1", "last": None}):
    m = CryptoMode(Config(width=128, height=32, fps=30, crypto_symbols=("BTC",)),
                   opener=make_opener({"BTC-USD": bad}))
    render(m)
    check(f"bad payload {bad} ignored", m.quotes, [])


# -- blinking clock colon ---------------------------------------------------

from ticker.modes.weather import WeatherMode  # noqa: E402

blink_cfg = frozen_market = Config(width=128, height=32, fps=30, timezone="America/New_York")
blinker = MarketMode(blink_cfg)
check("colon shown at tick 0", ":" in blinker.clock_text(0), True)
check("colon shown at tick 14", ":" in blinker.clock_text(14), True)
check("colon hidden at tick 15", ":" in blinker.clock_text(15), False)
check("colon hidden at tick 29", ":" in blinker.clock_text(29), False)
check("colon back at tick 30", ":" in blinker.clock_text(30), True)
check("colon hidden at tick 45", ":" in blinker.clock_text(45), False)

# Blanking the colon must not change the string length, or the digits shuffle.
check("blink keeps length",
      len(blinker.clock_text(0)), len(blinker.clock_text(15)))

# One full on/off cycle per second at any usable frame rate. At 1 fps a
# one-second blink is below the frame rate, so the fastest legal blink is one
# frame on, one frame off, and that is asserted separately below.
for fps in (2, 10, 24, 30, 60):
    cfg = Config(width=128, height=32, fps=fps, timezone="America/New_York")
    mode = MarketMode(cfg)
    states = [":" in mode.clock_text(tick) for tick in range(fps * 4)]
    flips = sum(1 for a, b in zip(states, states[1:]) if a != b)
    # Four seconds of frames should hold four on-phases, so 7 or 8 transitions.
    check_true(f"fps {fps} blinks about once a second", 6 <= flips <= 8)

slow = MarketMode(Config(width=128, height=32, fps=1, timezone="America/New_York"))
check("1 fps alternates every frame",
      [":" in slow.clock_text(tick) for tick in range(4)], [True, False, True, False])

# The blink has to reach the panel, not just the string.
def lit_pixels(mode_obj, tick):
    canvas = Canvas(128, 32)
    mode_obj.render(canvas, tick)
    return sum(1 for px in canvas.image_buffer.getdata() if any(px))


on, off = lit_pixels(blinker, 0), lit_pixels(blinker, 15)
check_true("market colon pixels disappear", on > off)

# Weather with no coordinates still draws the clock, offline and deterministic.
weather = WeatherMode(Config(width=128, height=32, fps=30, timezone="America/New_York"))
w_on, w_off = lit_pixels(weather, 0), lit_pixels(weather, 15)
check_true("weather colon pixels disappear", w_on > w_off)
check("weather colon delta is two dots", w_on - w_off, 2)

# -- registry ---------------------------------------------------------------

check("market in valid modes", "market" in VALID_MODES, True)
check("crypto in valid modes", "crypto" in VALID_MODES, True)
check("build market", type(build_mode("market", config)).__name__, "MarketMode")
check("build crypto", type(build_mode("crypto", config)).__name__, "CryptoMode")
check("unknown falls back", type(build_mode("nope", config)).__name__, "WeatherMode")

print(f"\n{PASS} checks passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
