"""Checks for the flights mode: maths, parsing, state file, and the web route.

Deliberately does not assert on live API content, which changes minute to minute.
It asserts on the parts that are ours and can be wrong quietly: distance maths,
flight-number normalisation, duration formatting, the progress clamp, and the
web endpoint's contract with the renderer.
"""

import sys

sys.path.insert(0, "/home/user/workspace/ticker-pi5/src")

from dataclasses import replace  # noqa: E402

from ticker import airlines  # noqa: E402
from ticker.canvas import Canvas, SMALL  # noqa: E402
from ticker.config import VALID_MODES, load_config  # noqa: E402
from ticker.modes import MODE_TYPES, build_mode  # noqa: E402
from ticker.modes.flights import (  # noqa: E402
    FlightsMode,
    Position,
    Route,
    format_duration,
    great_circle_nm,
    normalise_flight_number,
)

checks: list[tuple[str, bool, str]] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    checks.append((label, bool(condition), detail))


config = load_config()
config.flight_file.unlink(missing_ok=True)

SFO = (37.6189, -122.375)
JFK = (40.6398, -73.7789)
AMS = (52.3086, 4.76389)

# --- distance -----------------------------------------------------------------
sfo_jfk = great_circle_nm(*SFO, *JFK)
check("SFO-JFK is about 2240nm", 2200 < sfo_jfk < 2280, f"{sfo_jfk:.0f}nm")
check("zero distance to self", great_circle_nm(*SFO, *SFO) == 0.0)
check("distance is symmetric", abs(great_circle_nm(*SFO, *AMS) - great_circle_nm(*AMS, *SFO)) < 1e-9)
antipode = great_circle_nm(0, 0, 0, 180)
check("antipodal does not blow up", 10700 < antipode < 10900, f"{antipode:.0f}nm")

# --- parsing ------------------------------------------------------------------
for raw, want in [("ua889", "UA889"), (" UA 889 ", "UA889"), ("UAL889", "UAL889"), ("", "")]:
    got = normalise_flight_number(raw)
    check(f"normalise {raw!r} -> {want}", got == want, got)

for minutes, want in [(0, "0M"), (46, "46M"), (60, "1H00M"), (134, "2H14M"), (1500, "25H00M")]:
    got = format_duration(minutes)
    check(f"format {minutes}min -> {want}", got == want, got)
check("negative minutes clamp to 0M", format_duration(-5) == "0M", format_duration(-5))

# --- progress and eta ---------------------------------------------------------
route = Route("KLM282", "KL282", "KL", "KLM", "KLM", "SFO", *SFO, "AMS", *AMS)


def staged(position, flight="KL282", the_route=route):
    mode = FlightsMode(replace(config, flight_number=flight))
    mode._flight = flight
    mode._route, mode._position = the_route, position
    mode._route_checked = mode._position_checked = 9e18
    mode._route_missing = the_route is None
    mode._position_missing = position is None
    return mode


at_origin = staged(Position(*SFO, None, 0, True, None, None))
check("at origin progress is 0", at_origin._progress_and_eta()[0] == 0.0)
check("on the ground gives no eta", at_origin._progress_and_eta()[1] is None)

at_dest = staged(Position(*AMS, None, 5, True, None, None))
check("at destination progress is 1", at_dest._progress_and_eta()[0] == 1.0)

# Beyond the destination, or a route that does not match where the aircraft is,
# must not produce a progress above 1 or below 0.
overshoot = staged(Position(60.0, 30.0, 35000, 480, False, None, None))
progress, _ = overshoot._progress_and_eta()
check("progress stays within 0..1", progress is not None and 0.0 <= progress <= 1.0, str(progress))

cruising = staged(Position(52.0, 0.0, 35000, 480, False, None, None))
progress, eta = cruising._progress_and_eta()
check("near destination reads high", progress is not None and progress > 0.95, str(progress))
check("eta is a small positive number", eta is not None and 0 < eta < 60, str(eta))

slow = staged(Position(50.0, -30.0, 35000, 40, False, None, None))
check("taxi-speed gives no eta", slow._progress_and_eta()[1] is None)
check("but still reports progress", slow._progress_and_eta()[0] is not None)

no_position = staged(None)
check("no position means no progress", no_position._progress_and_eta() == (None, None))
no_route = staged(Position(*SFO, 30000, 400, False, None, None), the_route=None)
check("no route means no progress", no_route._progress_and_eta() == (None, None))

# --- rendering never raises ---------------------------------------------------
for label, mode in [
    ("at origin", at_origin),
    ("cruising", cruising),
    ("no position", no_position),
    ("no route", no_route),
]:
    canvas = Canvas(config.width, config.height)
    canvas.clear()
    try:
        mode.render(canvas, 0)
        ok, detail = True, ""
    except Exception as error:  # noqa: BLE001
        ok, detail = False, repr(error)
    check(f"render survives: {label}", ok, detail)

# Nothing may be drawn outside the panel, which would mean a silent clipping bug.
canvas = Canvas(config.width, config.height)
canvas.clear()
cruising.render(canvas, 0)
check("render fills the panel bounds", canvas.image_buffer.size == (config.width, config.height))

# --- airlines -----------------------------------------------------------------
check("United is branded", airlines.brand_for("UA") != airlines.DEFAULT_BRAND)
check("lowercase resolves", airlines.brand_for("ua") == airlines.brand_for("UA"))
check("unknown falls back", airlines.brand_for("ZZ") == airlines.DEFAULT_BRAND)
check("None falls back", airlines.brand_for(None) == airlines.DEFAULT_BRAND)
check("IATA wins for the tile", airlines.tile_code("UA", "UAL") == "UA")
check("ICAO used when no IATA", airlines.tile_code("", "TZP") == "TZP")
check("empty stays empty", airlines.tile_code(None, None) == "")

# Every brand code must fit the 16px tile, or a livery addition silently overflows.
overflowing = []
probe = Canvas(64, 16)
for code in airlines.BRANDS:
    if probe.text_width(code, SMALL) > airlines.LOGO_SIZE:
        overflowing.append(code)
check("all brand codes fit the tile", not overflowing, ",".join(overflowing))
check("brand colours are all triples", all(
    len(pair) == 2 and all(len(c) == 3 for c in pair) for pair in airlines.BRANDS.values()
))
# A tile whose text has the same brightness as its background is unreadable, and
# summed RGB does not detect that: red on mid green has a large sum difference
# and is still invisible, because red and green of equal luminance cancel. Use
# perceptual luminance, which is what the eye actually resolves at 16 pixels.
def luminance(colour):
    return 0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]


low_contrast = [
    code for code, (bg, fg) in airlines.BRANDS.items()
    if abs(luminance(bg) - luminance(fg)) < 60
]
check("every tile has contrast", not low_contrast, ",".join(low_contrast))

# --- registry and config ------------------------------------------------------
check("flights is a valid mode", "flights" in VALID_MODES)
check("flights is registered", MODE_TYPES.get("flights") is FlightsMode)
check("build_mode returns it", isinstance(build_mode("flights", config), FlightsMode))

config.set_flight(" ua 889 ")
check("set_flight normalises", config.current_flight() == "UA889", config.current_flight())
config.set_flight("")
check("empty clears to the env value", config.current_flight() == config.flight_number,
      config.current_flight())
config.set_flight("X" * 40)
check("absurd input is truncated", len(config.current_flight()) <= 12, config.current_flight())
config.set_flight("")

# --- web endpoint -------------------------------------------------------------
from ticker.web.app import create_app  # noqa: E402

client = create_app().test_client()
config.set_mode("weather")
response = client.post("/flight", json={"flight": "dl 38"})
payload = response.get_json()
check("POST /flight accepts a number", response.status_code == 200 and payload["flight"] == "DL38",
      str(payload))
check("setting a flight switches mode", payload["current_mode"] == "flights", str(payload))
check("renderer sees the same value", config.current_flight() == "DL38", config.current_flight())

status = client.get("/api/status").get_json()
check("status reports the flight", status.get("flight") == "DL38", str(status.get("flight")))

page = client.get("/").get_data(as_text=True)
check("page renders the input", 'id="flight-input"' in page)
check("page prefills the flight", 'value="DL38"' in page)
check("page offers the flights button", 'data-mode="flights"' in page)

cleared = client.post("/flight", json={"flight": ""}).get_json()
check("clearing works", cleared["flight"] == "", str(cleared))
check("clearing leaves the mode alone", cleared["current_mode"] == "flights", str(cleared))
missing = client.post("/flight", json={})
check("a missing field is not an error", missing.status_code == 200, str(missing.status_code))
config.flight_file.unlink(missing_ok=True)
config.set_mode("weather")

# --- report -------------------------------------------------------------------
failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
raise SystemExit(1 if failed else 0)
