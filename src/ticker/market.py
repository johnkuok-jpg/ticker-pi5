# MIT License — Copyright (c) 2026 John Kuok
"""US equity market session state: phase, countdown and session progress.

The exchange calendar is hard-coded rather than fetched. A trading calendar is
small, changes once a year, and is published well in advance, so a network
dependency would buy nothing and would fail exactly when the panel is most
likely to be unplugged and moved. Dates and early closes come from the NYSE
holiday and trading-hours page:
https://www.nyse.com/markets/hours-calendars

Because it is hard-coded it also expires. Anything outside ``CALENDAR_YEARS``
falls back to weekday-only arithmetic and reports ``calendar_known=False`` so
callers can say so instead of quietly claiming a holiday is a trading day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

#: NYSE Early and Late Trading Sessions, not the 4:00 am figure retail brokers
#: quote. These are the hours the exchange itself publishes.
PREMARKET_OPEN = time(7, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
AFTERHOURS_CLOSE = time(20, 0)

#: Full closures. Dates are the observed dates, which is why 2026 shows July 3
#: and 2027 shows December 24.
HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1): "NEW YEAR",
    date(2026, 1, 19): "MLK DAY",
    date(2026, 2, 16): "PRESIDENTS",
    date(2026, 4, 3): "GOOD FRIDAY",
    date(2026, 5, 25): "MEMORIAL DAY",
    date(2026, 6, 19): "JUNETEENTH",
    date(2026, 7, 3): "JULY 4TH",
    date(2026, 9, 7): "LABOR DAY",
    date(2026, 11, 26): "THANKSGIVING",
    date(2026, 12, 25): "CHRISTMAS",
    date(2027, 1, 1): "NEW YEAR",
    date(2027, 1, 18): "MLK DAY",
    date(2027, 2, 15): "PRESIDENTS",
    date(2027, 3, 26): "GOOD FRIDAY",
    date(2027, 5, 31): "MEMORIAL DAY",
    date(2027, 6, 18): "JUNETEENTH",
    date(2027, 7, 5): "JULY 4TH",
    date(2027, 9, 6): "LABOR DAY",
    date(2027, 11, 25): "THANKSGIVING",
    date(2027, 12, 24): "CHRISTMAS",
}

#: 1:00 pm ET closes. 2027 has only one: with Christmas observed on Friday
#: December 24 there is no separate Christmas Eve early close that year.
EARLY_CLOSES: dict[date, str] = {
    date(2026, 11, 27): "EARLY CLOSE 1PM",
    date(2026, 12, 24): "EARLY CLOSE 1PM",
    date(2027, 11, 26): "EARLY CLOSE 1PM",
}

CALENDAR_YEARS = (2026, 2027)

GREEN = (40, 230, 90)
AMBER = (255, 176, 0)
BLUE = (150, 190, 255)
RED = (255, 70, 70)


@dataclass(frozen=True)
class SessionState:
    """Everything a caller needs to describe the market right now."""

    phase: str  # closed | pre | open | after
    label: str
    color: tuple[int, int, int]
    note: str  # holiday name, early-close warning, or ""
    countdown_label: str  # e.g. "3H12M LEFT", "OPENS IN 42M"
    seconds_remaining: int
    progress: float | None  # fraction through the regular session, when open
    calendar_known: bool

    @property
    def is_open(self) -> bool:
        return self.phase == "open"


def _is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def calendar_covers(day: date) -> bool:
    """Whether the hard-coded holiday table can be trusted for *day*."""
    return day.year in CALENDAR_YEARS


def is_trading_day(day: date) -> bool:
    """A weekday that is not a full exchange holiday."""
    if _is_weekend(day):
        return False
    return day not in HOLIDAYS


def close_time_for(day: date) -> time:
    """Regular-session close, honouring 1:00 pm early closes."""
    return EARLY_CLOSE if day in EARLY_CLOSES else REGULAR_CLOSE


def next_trading_day(day: date) -> date:
    """The next trading day strictly after *day*. Bounded so it cannot spin."""
    for offset in range(1, 12):
        candidate = day + timedelta(days=offset)
        if is_trading_day(candidate):
            return candidate
    return day + timedelta(days=1)


def format_duration(seconds: int) -> str:
    """Compact duration for an 8-pixel font: 2D6H, 3H12M, 42M, 30S."""
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}D{hours}H"
    if hours:
        return f"{hours}H{minutes:02d}M"
    if minutes:
        return f"{minutes}M"
    return f"{secs}S"


def _at(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=MARKET_TZ)


def _next_open_after(local: datetime) -> datetime:
    """The next regular-session open at or after *local*."""
    today = local.date()
    if is_trading_day(today):
        opening = _at(today, REGULAR_OPEN)
        if local < opening:
            return opening
    return _at(next_trading_day(today), REGULAR_OPEN)


def session_state(now: datetime) -> SessionState:
    """Classify *now* into a market phase with a countdown to the next change."""
    local = now.astimezone(MARKET_TZ) if now.tzinfo else now.replace(tzinfo=MARKET_TZ)
    today = local.date()
    known = calendar_covers(today)
    holiday = HOLIDAYS.get(today)
    early = EARLY_CLOSES.get(today)

    if not is_trading_day(today):
        target = _next_open_after(local)
        remaining = int((target - local).total_seconds())
        if holiday:
            label, note = "CLOSED", holiday
        else:
            label, note = "WEEKEND", ""
        return SessionState(
            phase="closed",
            label=label,
            color=RED,
            note=note,
            countdown_label=f"OPENS IN {format_duration(remaining)}",
            seconds_remaining=remaining,
            progress=None,
            calendar_known=known,
        )

    opening = _at(today, REGULAR_OPEN)
    closing = _at(today, close_time_for(today))
    pre_start = _at(today, PREMARKET_OPEN)
    after_end = _at(today, AFTERHOURS_CLOSE)
    note = early or ""

    if pre_start <= local < opening:
        remaining = int((opening - local).total_seconds())
        return SessionState(
            phase="pre",
            label="PRE",
            color=AMBER,
            note=note,
            countdown_label=f"OPENS IN {format_duration(remaining)}",
            seconds_remaining=remaining,
            progress=None,
            calendar_known=known,
        )

    if opening <= local < closing:
        remaining = int((closing - local).total_seconds())
        span = (closing - opening).total_seconds()
        elapsed = (local - opening).total_seconds()
        return SessionState(
            phase="open",
            label="OPEN",
            color=GREEN,
            note=note,
            countdown_label=f"{format_duration(remaining)} LEFT",
            seconds_remaining=remaining,
            progress=max(0.0, min(1.0, elapsed / span)) if span else None,
            calendar_known=known,
        )

    if closing <= local < after_end:
        target = _next_open_after(local)
        remaining = int((target - local).total_seconds())
        return SessionState(
            phase="after",
            label="AFTER",
            color=BLUE,
            note=note,
            countdown_label=f"OPENS IN {format_duration(remaining)}",
            seconds_remaining=remaining,
            progress=None,
            calendar_known=known,
        )

    target = _next_open_after(local)
    remaining = int((target - local).total_seconds())
    return SessionState(
        phase="closed",
        label="CLOSED",
        color=RED,
        note=note,
        countdown_label=f"OPENS IN {format_duration(remaining)}",
        seconds_remaining=remaining,
        progress=None,
        calendar_known=known,
    )
