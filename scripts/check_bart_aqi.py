# MIT License — Copyright (c) 2026 John Kuok
"""Offline checks for the BART departure board and air quality modes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("/home/user/workspace/ticker-pi5/src")))

from ticker import bart
from ticker.canvas import SMALL, Canvas
from ticker.config import VALID_MODES, Config
from ticker.modes import MODE_TYPES, build_mode
from ticker.modes.airquality import CATEGORIES, LUMINANCE_FLOOR, _luminance, classify, panel_color
from ticker.modes.bart import BartMode

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def section(title: str) -> None:
    print(f"\n== {title}")


def make_config(**overrides) -> Config:
    base = dict(
        width=128,
        height=32,
        fps=30,
        weather_lat="37.7749",
        weather_lon="-122.4194",
        timezone="America/Los_Angeles",
        state_dir=Path("/tmp/ticker-test-state"),
    )
    base.update(overrides)
    return Config(**base)


def etd_payload(entries, message=None):
    """Build an ETD response shaped like BART's."""
    root = {
        "date": "08/13/2026",
        "time": "04:10:03 PM PDT",
        "station": [{"name": "Embarcadero", "abbr": "EMBR", "etd": entries}],
    }
    if message is not None:
        root["message"] = message
    return json.dumps({"root": root}).encode()


def estimate(minutes, color="YELLOW", platform="2", direction="North", length="9", delay="0", cancel="0"):
    return {
        "minutes": minutes,
        "platform": platform,
        "direction": direction,
        "length": length,
        "color": color,
        "hexcolor": "#ffff33",
        "bikeflag": "1",
        "delay": delay,
        "cancelflag": cancel,
        "dynamicflag": "0",
    }


# ---------------------------------------------------------------- bart client

section("BART station table")
check("fifty stations", len(bart.STATIONS) == 50, f"got {len(bart.STATIONS)}")
check("abbreviations unique", len({a for a, _ in bart.STATIONS}) == 50)
check("names unique", len({n for _, n in bart.STATIONS}) == 50)
check("default station is real", bart.is_station(bart.DEFAULT_STATION))
check("lookup is case insensitive", bart.is_station("embr") and bart.is_station("EMBR"))
check("rejects nonsense", not bart.is_station("XXXX") and not bart.is_station(""))

section("BART panel names fit the destination column")
canvas = Canvas(128, 32)
# The narrowest real case: a 3-character countdown plus a platform digit.
budget = 128 - 1 - canvas.text_width("NOW", SMALL) - 3 - canvas.text_width("2", SMALL) - 3
for abbr, _ in bart.STATIONS:
    name = bart.panel_name(abbr)
    width = canvas.text_width(name, SMALL)
    check(f"{abbr} '{name}' fits ({width}px <= {budget}px)", width <= budget, f"{width}px")

section("BART panel name mapping")
check("PHIL shortened", bart.panel_name("PHIL") == "PLEASANT HILL")
check("SFIA shortened", bart.panel_name("SFIA") == "SFO AIRPORT")
check("unmapped falls back to uppercase", bart.panel_name("ASHB") == "ASHBY")
check("unknown code returns itself", bart.panel_name("ZZZZ") == "ZZZZ")

section("BART parsing")
board = bart.lookup(
    "EMBR",
    opener=lambda url: etd_payload(
        [
            {"destination": "Antioch", "abbreviation": "ANTC", "estimate": [estimate("17"), estimate("33")]},
            {"destination": "Richmond", "abbreviation": "RICH", "estimate": [estimate("1", color="RED")]},
            {"destination": "SF Airport", "abbreviation": "SFIA", "estimate": [estimate("Leaving")]},
        ]
    ),
)
check("board returned", board is not None)
assert board is not None
check("flattened across destinations", len(board.departures) == 4, f"got {len(board.departures)}")
check(
    "sorted soonest first",
    [d.minutes for d in board.departures] == [0, 1, 17, 33],
    str([d.minutes for d in board.departures]),
)
check("'Leaving' parses to zero", board.departures[0].minutes == 0)
check("'Leaving' renders as NOW", board.departures[0].countdown() == "NOW")
check("minute countdown renders with M", board.departures[1].countdown() == "1M")
check("line colour comes from name", board.departures[1].color == bart.LINE_COLORS["RED"])
check("station name is the panel spelling", board.name == "EMBARCADERO")
check("destination label mapped", board.departures[0].label == "SFO AIRPORT")
check("cars parsed", board.departures[2].cars == 9)
check("direction reduced to one letter", board.departures[2].direction == "N")

section("BART edge cases")
cancelled = bart.lookup(
    "EMBR",
    opener=lambda url: etd_payload(
        [{"destination": "Antioch", "abbreviation": "ANTC", "estimate": [estimate("5", cancel="1"), estimate("20")]}]
    ),
)
assert cancelled is not None
check("cancelled trains dropped", [d.minutes for d in cancelled.departures] == [20])

single = bart.lookup(
    "EMBR",
    # BART collapses one-element arrays into a bare object rather than a list.
    opener=lambda url: json.dumps(
        {
            "root": {
                "station": {
                    "name": "Embarcadero",
                    "abbr": "EMBR",
                    "etd": {"destination": "Antioch", "abbreviation": "ANTC", "estimate": estimate("7")},
                }
            }
        }
    ).encode(),
)
check("un-listed single objects handled", single is not None and len(single.departures) == 1)

no_service = bart.lookup(
    "EMBR", opener=lambda url: json.dumps({"root": {"message": {"warning": "No data matched your criteria."}}}).encode()
)
check("no-service payload yields empty board", no_service is not None and not no_service.departures)
check(
    "no-service message surfaced",
    no_service is not None and "NO DATA" in no_service.message,
    no_service.message if no_service else "",
)

check("unknown station returns None", bart.lookup("XXXX", opener=lambda url: b"{}") is None)


def boom(url):
    raise OSError("network down")


check("network failure returns None", bart.lookup("EMBR", opener=boom) is None)
check("garbage json returns None", bart.lookup("EMBR", opener=lambda url: b"not json") is None)
check("empty object returns None", bart.lookup("EMBR", opener=lambda url: b"{}") is None)

bad_minutes = bart.lookup(
    "EMBR",
    opener=lambda url: etd_payload(
        [{"destination": "Antioch", "abbreviation": "ANTC", "estimate": [estimate("soon"), estimate("9")]}]
    ),
)
assert bad_minutes is not None
check("unparseable countdown skipped", [d.minutes for d in bad_minutes.departures] == [9])

unknown_line = bart.lookup(
    "EMBR",
    opener=lambda url: etd_payload(
        [{"destination": "Antioch", "abbreviation": "ANTC", "estimate": [estimate("4", color="TEAL")]}]
    ),
)
assert unknown_line is not None
check("unknown line falls back to hexcolor", unknown_line.departures[0].color == (255, 255, 51))

delayed = bart.lookup(
    "EMBR",
    opener=lambda url: etd_payload(
        [{"destination": "Antioch", "abbreviation": "ANTC", "estimate": [estimate("9", delay="358")]}]
    ),
)
assert delayed is not None
check("delay flagged past the threshold", delayed.departures[0].is_delayed)
check("delay seconds preserved", delayed.departures[0].delay_seconds == 358)
check("routine BART lateness is not flagged", not bart.Departure(
    destination="RICH", label="RICHMOND", minutes=3, color=bart.LINE_COLORS["RED"],
    platform="2", direction="N", cars=9, delay_seconds=214).is_delayed)
check("five minute threshold", bart.DELAY_WARN_SECONDS == 300)

section("BART line colours are legible")
for name, color in bart.LINE_COLORS.items():
    lum = _luminance(color)
    check(f"{name} luminance {lum:.0f} >= 60", lum >= 60, f"{color}")

# ------------------------------------------------------------------ aqi scale

section("EPA categories match the published scale")
expected = [
    (50, "GOOD", (0, 228, 0)),
    (100, "MODERATE", (255, 255, 0)),
    (150, "SENSITIVE", (255, 126, 0)),
    (200, "UNHEALTHY", (255, 0, 0)),
    (300, "V UNHEALTHY", (143, 63, 151)),
]
for index, (limit, name, color) in enumerate(expected):
    check(f"{name} breakpoint {limit}", CATEGORIES[index][0] == limit, str(CATEGORIES[index][0]))
    check(f"{name} official colour {color}", CATEGORIES[index][2] == color, str(CATEGORIES[index][2]))
check("hazardous colour is EPA maroon", CATEGORIES[5][2] == (126, 0, 35))
check("six categories", len(CATEGORIES) == 6)

section("AQI boundary classification")
for value, name in (
    (0, "GOOD"),
    (50, "GOOD"),
    (51, "MODERATE"),
    (100, "MODERATE"),
    (101, "SENSITIVE"),
    (150, "SENSITIVE"),
    (151, "UNHEALTHY"),
    (200, "UNHEALTHY"),
    (201, "V UNHEALTHY"),
    (300, "V UNHEALTHY"),
    (301, "HAZARDOUS"),
    (500, "HAZARDOUS"),
    (9999, "HAZARDOUS"),
):
    check(f"AQI {value} is {name}", classify(value)[0] == name, classify(value)[0])

section("Panel colours clear the legibility floor")
for limit, name, color in CATEGORIES:
    lifted = panel_color(color)
    lum = _luminance(lifted)
    check(f"{name} lifted to {lum:.0f}", lum >= LUMINANCE_FLOOR, f"{lifted} = {lum:.1f}")

section("Only the two dark categories are altered")
for name, color, changed in (
    ("GOOD", (0, 228, 0), False),
    ("MODERATE", (255, 255, 0), False),
    ("SENSITIVE", (255, 126, 0), False),
    ("V UNHEALTHY", (143, 63, 151), False),
    ("UNHEALTHY", (255, 0, 0), True),
    ("HAZARDOUS", (126, 0, 35), True),
):
    was_changed = panel_color(color) != color
    check(f"{name} {'lifted' if changed else 'untouched'}", was_changed == changed, str(panel_color(color)))

section("Lifting preserves hue order")
red = panel_color((255, 0, 0))
maroon = panel_color((126, 0, 35))
check("red stays dominant in red", red[0] > red[1] and red[0] > red[2], str(red))
check("maroon stays dominant in red", maroon[0] > maroon[1] and maroon[0] > maroon[2], str(maroon))
check("maroon keeps more blue than red", maroon[2] > maroon[1], str(maroon))
check("red and maroon remain distinguishable", abs(red[2] - maroon[2]) >= 15, f"{red} vs {maroon}")

# ------------------------------------------------------------------- registry

section("Mode registry")
for name in ("bart", "aqi"):
    check(f"{name} in VALID_MODES", name in VALID_MODES)
    check(f"{name} registered", name in MODE_TYPES)
    check(f"{name} builds", isinstance(build_mode(name, make_config()), MODE_TYPES[name]))
check("eight modes", len(VALID_MODES) == 8, str(len(VALID_MODES)))
check("registry covers every valid mode", set(MODE_TYPES) == set(VALID_MODES))

# --------------------------------------------------------------- bart render

section("BART renders without network")


class OfflineBart(BartMode):
    """BartMode with the network replaced, so renders are deterministic."""

    def __init__(self, config, board):
        super().__init__(config)
        self._board = board

    def _refresh(self, station):
        self._station = station
        self.board = self._board
        self._failed = self._board is None
        self._last_refresh = 1e9  # never refresh again during a render pass


def lit_pixels(canvas):
    return sum(1 for pixel in canvas.image_buffer.convert("RGB").getdata() if any(pixel))


three = bart.Board(
    "EMBR",
    "EMBARCADERO",
    (
        bart.Departure("RICH", "RICHMOND", 1, bart.LINE_COLORS["RED"], "2", "N", 10, 0),
        bart.Departure("SFIA", "SFO AIRPORT", 3, bart.LINE_COLORS["YELLOW"], "1", "S", 9, 0),
        bart.Departure("BERY", "BERRYESSA", 11, bart.LINE_COLORS["GREEN"], "2", "N", 6, 340),
    ),
)

canvas = Canvas(128, 32)
mode = OfflineBart(make_config(), three)
mode.render(canvas, 0)
check("board draws something", lit_pixels(canvas) > 150, str(lit_pixels(canvas)))

# Nothing may spill past the right edge or below the panel.
pixels = canvas.image_buffer.convert("RGB").load()
right_column = [pixels[127, y] for y in range(32)]
check("right column clear of the bezel", not any(any(p) for p in right_column))

for extra in ("empty", "loading", "bad station"):
    if extra == "empty":
        subject = OfflineBart(make_config(), bart.Board("EMBR", "EMBARCADERO", (), "NO DATA MATCHED"))
    elif extra == "loading":
        subject = OfflineBart(make_config(), None)
    else:
        subject = OfflineBart(make_config(bart_station="XXXX"), three)
    blank = Canvas(128, 32)
    try:
        subject.render(blank, 0)
        drew = lit_pixels(blank) > 20
    except Exception as error:  # noqa: BLE001
        drew = False
        print(f"       raised {error!r}")
    check(f"{extra} state renders a message", drew)

section("BART clock blinks in the header")
first = Canvas(128, 32)
second = Canvas(128, 32)
blink_mode = OfflineBart(make_config(), three)
blink_mode.render(first, 0)
blink_mode.render(second, 15)
delta = sum(
    1
    for a, b in zip(first.image_buffer.convert("RGB").getdata(), second.image_buffer.convert("RGB").getdata())
    if a != b
)
check("colon is the only difference", delta == 2, f"{delta} pixels differ")

section("Station change beats the cache")
switcher = OfflineBart(make_config(), three)
switcher.render(Canvas(128, 32), 0)
check("station recorded on first render", switcher._station == "EMBR", switcher._station)
switcher._board = bart.Board("MONT", "MONTGOMERY", three.departures, "")
switcher.config = make_config()  # frozen; station comes from the state file
check("cache would otherwise hold", switcher._last_refresh == 1e9)

# ---------------------------------------------------------------- aqi render

section("Air quality renders without network")
from ticker.modes.airquality import AirQuality, AirQualityMode


class OfflineAir(AirQualityMode):
    def __init__(self, config, reading):
        super().__init__(config)
        self.reading = reading
        self._last_refresh = 1e9

    def _refresh(self):  # pragma: no cover - must never be called
        raise AssertionError("render must not hit the network")


trend = tuple(float(v) for v in (44, 41, 39, 43, 52, 58, 61, 55, 48, 42, 38, 35, 33, 31, 30, 30, 29, 29, 29, 28, 28, 29, 31, 29))
for aqi in (7, 29, 88, 142, 175, 260, 355, 500):
    canvas = Canvas(128, 32)
    subject = OfflineAir(make_config(), AirQuality(aqi=aqi, pm2_5=12.4, trend=trend))
    subject.render(canvas, 0)
    count = lit_pixels(canvas)
    check(f"AQI {aqi} draws", count > 200, str(count))
    pixels = canvas.image_buffer.convert("RGB").load()
    check(f"AQI {aqi} stays inside the panel", not any(any(pixels[127, y]) for y in range(32)))

section("Air quality edge cases")
for label, reading in (
    ("no pm2.5", AirQuality(aqi=42, pm2_5=None, trend=trend)),
    ("no trend", AirQuality(aqi=42, pm2_5=8.0, trend=())),
    ("single trend point", AirQuality(aqi=42, pm2_5=8.0, trend=(42.0,))),
    ("flat trend", AirQuality(aqi=42, pm2_5=8.0, trend=(42.0,) * 24)),
):
    canvas = Canvas(128, 32)
    try:
        OfflineAir(make_config(), reading).render(canvas, 0)
        ok = lit_pixels(canvas) > 100
    except Exception as error:  # noqa: BLE001
        ok = False
        print(f"       raised {error!r}")
    check(f"{label} renders", ok)

missing = AirQualityMode(make_config(weather_lat="", weather_lon=""))
canvas = Canvas(128, 32)
missing.render(canvas, 0)
check("missing location asks for one", lit_pixels(canvas) > 50)

section("Trend windowing")
mode = AirQualityMode(make_config())
payload = {
    "hourly": {
        "time": [f"2026-08-13T{hour:02d}:00" for hour in range(24)],
        "us_aqi": list(range(100, 124)),
    }
}
window = mode._trend(payload, "2026-08-13T10:00")
check("window ends at the current hour", window[-1] == 110.0, str(window[-1]))
check("window is 11 points at hour 10", len(window) == 11, str(len(window)))
check("forecast hours excluded", max(window) == 110.0, str(max(window)))

full = mode._trend({"hourly": {"time": [f"h{i}" for i in range(48)], "us_aqi": list(range(48))}}, "h30")
check("window caps at 24 hours", len(full) == 24, str(len(full)))
check("unknown current time falls back to the end", len(mode._trend(payload, "nope")) == 24)
check("nulls dropped", mode._trend({"hourly": {"time": ["a", "b"], "us_aqi": [None, 5]}}, "b") == (5.0,))
check("no hourly data is empty", mode._trend({}, "a") == ())

# -- advisory wrapping and rider wording ------------------------------------

from ticker.modes.bart import _rider_message, _wrap  # noqa: E402
from ticker.canvas import SMALL as SMALL_FONT  # noqa: E402

check("boilerplate becomes rider wording", _rider_message("No data matched your criteria.") == "NO TRAINS RUNNING")
check("empty message becomes rider wording", _rider_message("   ") == "NO TRAINS RUNNING")
check("real advisory passes through", _rider_message("Major delay at Powell") == "MAJOR DELAY AT POWELL")
check("trailing period trimmed", _rider_message("Elevator out.") == "ELEVATOR OUT")

wrap_canvas = Canvas(128, 32)
short = _wrap(wrap_canvas, "NO TRAINS RUNNING", 126, SMALL_FONT)
check("short message stays one line", len(short) == 1, str(short))
long = _wrap(wrap_canvas, "MAJOR DELAY IN THE ANTIOCH DIRECTION AT MACARTHUR", 126, SMALL_FONT)
check("long message wraps to two lines", len(long) == 2, str(long))
check("no line overflows the panel", all(wrap_canvas.text_width(line, SMALL_FONT) <= 126 for line in long), str(long))
check("wrap breaks on a space", all(not line.startswith(" ") and not line.endswith(" ") for line in long), str(long))
check("first word intact", long[0].split()[0] == "MAJOR", str(long))
huge = _wrap(wrap_canvas, "SUPERCALIFRAGILISTICEXPIALIDOCIOUSNESSNESS ANDMORE", 126, SMALL_FONT)
check("unbreakable word is cut not dropped", huge and huge[0].startswith("SUPERCALI"), str(huge))
check("wrap never exceeds the limit", len(huge) <= 2, str(huge))
check("empty text yields a blank line", _wrap(wrap_canvas, "", 126, SMALL_FONT) == [""])

# -- pinned AQI hero column -------------------------------------------------

from ticker.modes.airquality import COLUMN_X, HERO_WIDTH  # noqa: E402

check("hero column fits two HUGE digits", HERO_WIDTH == 24)
check("column starts clear of the hero", COLUMN_X == 29)
widths = []
for value in (7, 29, 355):
    canvas = Canvas(128, 32)
    posed = AirQualityMode(make_config())
    posed.reading = AirQuality(value, 10.0, tuple(float(v) for v in range(20, 44)))
    posed._last_refresh = 1e18
    posed.render(canvas, 0)
    pixels = canvas.image_buffer.load()
    columns = [x for x in range(128) for y in range(32) if pixels[x, y] != (0, 0, 0)]
    widths.append((value, min(columns), max(columns)))
check("single digit is centred not flush left", widths[0][1] >= 4, str(widths))
check("every reading stays inside the panel", all(right <= 126 for _, _, right in widths), str(widths))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
