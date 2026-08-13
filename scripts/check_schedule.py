# MIT License — Copyright (c) 2026 John Kuok
"""Verify the brightness schedule against hand-computed expectations.

Every case here is one I can reason about independently of the implementation:
a schedule, a wall-clock moment, and the level a human reading the schedule
would expect. The midnight-rollback and weekend-fallthrough cases are the ones
most likely to be wrong, so they get the most coverage.
"""

import sys
from pathlib import Path
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ticker.config import Config, parse_brightness_schedule  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SIMPLE = "07:00=55, 09:30=75, 19:00=45, 22:00=15, 23:30=off"
SPLIT = "mon-fri 06:30=60, mon-fri 09:00=80, sat-sun 09:00=50, 21:00=25, 23:00=off"
WEEKDAY_ONLY = "weekday 08:00=70, weekday 22:00=10"

CASES = [
    # (label, schedule, datetime, expected level)
    ("simple: before first step falls back to yesterday's last", SIMPLE, (2026, 8, 13, 3, 0), 0.0),
    ("simple: mid-morning", SIMPLE, (2026, 8, 13, 10, 0), 0.75),
    ("simple: exactly on a step boundary", SIMPLE, (2026, 8, 13, 19, 0), 0.45),
    ("simple: one minute before a step", SIMPLE, (2026, 8, 13, 18, 59), 0.75),
    ("simple: late evening off", SIMPLE, (2026, 8, 13, 23, 45), 0.0),
    ("split: Thursday morning uses weekday step", SPLIT, (2026, 8, 13, 7, 0), 0.60),
    ("split: Thursday 10am uses weekday 09:00", SPLIT, (2026, 8, 13, 10, 0), 0.80),
    ("split: Saturday 10am uses weekend step, not weekday", SPLIT, (2026, 8, 15, 10, 0), 0.50),
    ("split: Saturday 07:00 is before weekend step, so Friday's off holds", SPLIT, (2026, 8, 15, 7, 0), 0.0),
    ("split: Sunday 22:00 uses the all-days 21:00 step", SPLIT, (2026, 8, 16, 22, 0), 0.25),
    ("weekday-only: Saturday noon inherits Friday's 22:00 step", WEEKDAY_ONLY, (2026, 8, 15, 12, 0), 0.10),
    ("weekday-only: Sunday noon still inherits Friday's 22:00 step", WEEKDAY_ONLY, (2026, 8, 16, 12, 0), 0.10),
    ("weekday-only: Monday 09:00 picks up the morning step", WEEKDAY_ONLY, (2026, 8, 17, 9, 0), 0.70),
]

NEXT_CASES = [
    # (label, schedule, datetime, expected next-change wall clock, expected level)
    ("next after 10am is 19:00 same day", SIMPLE, (2026, 8, 13, 10, 0), (2026, 8, 13, 19, 0), 0.45),
    ("next after 23:45 rolls to tomorrow 07:00", SIMPLE, (2026, 8, 13, 23, 45), (2026, 8, 14, 7, 0), 0.55),
    ("Friday 23:30 next is Saturday's weekend step", SPLIT, (2026, 8, 14, 23, 30), (2026, 8, 15, 9, 0), 0.50),
    ("weekday-only Friday 23:00 skips the weekend to Monday", WEEKDAY_ONLY, (2026, 8, 14, 23, 0), (2026, 8, 17, 8, 0), 0.70),
]

PARSE_CASES = [
    # (label, text, expected number of steps)
    ("percentages and fractions both parse", "07:00=55, 08:00=0.8", 2),
    ("off aliases parse", "22:00=off, 23:00=dark, 23:30=none", 3),
    ("malformed entries are skipped, valid ones survive", "07:00=55, garbage, 25:00=50, 08:00=abc, 09:00=40", 2),
    ("bare hour without minutes parses", "7=50", 1),
    ("wrapping day range fri-mon covers four days", "fri-mon 08:00=50", 1),
    ("empty schedule yields nothing", "", 0),
    ("unknown day name is skipped", "funday 08:00=50, mon 09:00=60", 1),
]

failures = []


def check(label, got, expected):
    ok = got == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}\n          got={got!r} expected={expected!r}")
    if not ok:
        failures.append(label)


print("=== active level ===")
for label, schedule, moment, expected in CASES:
    config = replace(
        Config(), brightness_schedule=parse_brightness_schedule(schedule), timezone="America/Los_Angeles"
    )
    now = datetime(*moment, tzinfo=TZ)
    result = config.scheduled_brightness(now)
    stamp = f"{WEEKDAY_NAMES[now.weekday()]} {now:%Y-%m-%d %H:%M}"
    check(f"{label} [{stamp}]", None if result is None else round(result[0], 4), expected)

print("\n=== next change ===")
for label, schedule, moment, expected_when, expected_level in NEXT_CASES:
    config = replace(
        Config(), brightness_schedule=parse_brightness_schedule(schedule), timezone="America/Los_Angeles"
    )
    result = config.next_brightness_change(datetime(*moment, tzinfo=TZ))
    got = None if result is None else (result[1].strftime("%Y-%m-%d %H:%M"), round(result[0], 4))
    want = (datetime(*expected_when).strftime("%Y-%m-%d %H:%M"), expected_level)
    check(label, got, want)

print("\n=== parsing ===")
for label, text, expected_count in PARSE_CASES:
    check(label, len(parse_brightness_schedule(text)), expected_count)

print("\n=== fri-mon wrap membership ===")
wrap = parse_brightness_schedule("fri-mon 08:00=50")[0]
check("fri-mon days", sorted(wrap.days), [0, 4, 5, 6])

print("\n=== manual override vs schedule ===")
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    config = replace(
        Config(),
        brightness_schedule=parse_brightness_schedule(SIMPLE),
        timezone="America/Los_Angeles",
        state_dir=Path(tmp),
    )
    # No override file at all: the schedule governs.
    check("no override -> schedule level", round(config.current_brightness(), 4) > 0.0, True)

    # A legacy bare-number file has an unknown age, so the schedule must win.
    config.brightness_file.write_text("0.90\n", encoding="utf-8")
    scheduled_now = config.scheduled_brightness()[1]
    check(
        "legacy bare-number override loses to schedule",
        round(config.current_brightness(), 4),
        round(config.scheduled_brightness()[0], 4),
    )

    # A fresh override, stamped after the active step began, wins.
    config.set_brightness(0.9)
    check("fresh override wins", round(config.current_brightness(), 4), 0.9)

    # An override stamped before the active step began has expired.
    config.brightness_file.write_text(f"0.90 {scheduled_now.timestamp() - 60:.0f}\n", encoding="utf-8")
    check(
        "override predating the active step has expired",
        round(config.current_brightness(), 4),
        round(config.scheduled_brightness()[0], 4),
    )

    # The slider must never be able to reach full dark, only the schedule may.
    config.set_brightness(0.0)
    check("slider floors at 5 percent", round(config.current_brightness(), 4), 0.05)

print("\n=== no schedule configured ===")
config = replace(Config(), brightness=0.35, timezone="America/Los_Angeles")
check("unscheduled returns None", config.scheduled_brightness(), None)
check("unscheduled next change returns None", config.next_brightness_change(), None)

print("\nFAILURES:", ", ".join(failures) if failures else "none")
sys.exit(1 if failures else 0)
