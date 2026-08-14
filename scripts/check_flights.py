"""Checks for the flights mode: maths, parsing, state file, and the web route.

Deliberately does not assert on live API content, which changes minute to minute.
It asserts on the parts that are ours and can be wrong quietly: distance maths,
flight-number normalisation, duration formatting, the progress clamp, and the
web endpoint's contract with the renderer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataclasses import replace  # noqa: E402

from ticker import airlines  # noqa: E402
from ticker.canvas import Canvas, SMALL  # noqa: E402
from ticker.config import VALID_MODES, load_config  # noqa: E402
from ticker.modes import MODE_TYPES, build_mode  # noqa: E402
from ticker.flightradar import (  # noqa: E402
    ARRIVALS_URL,
    FlightRadarClient,
    airborne_arrivals,
)
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

config.set_mode("weather")
airport_reply = client.post("/flight-airport", json={"airport": "sfo"})
airport_payload = airport_reply.get_json()
check("POST /flight-airport accepts a code",
      airport_reply.status_code == 200 and airport_payload["flight_airport"] == "SFO",
      str(airport_payload))
check("watching an airport switches mode", airport_payload["current_mode"] == "flights",
      str(airport_payload))
check("watching an airport clears the number", airport_payload["flight"] == "",
      str(airport_payload))
check("renderer sees the airport", config.current_flight_airport() == "SFO")
check("status reports the airport",
      client.get("/api/status").get_json().get("flight_airport") == "SFO")
page = client.get("/").get_data(as_text=True)
check("page renders the airport input", 'id="airport-input"' in page)
check("page prefills the airport", 'value="SFO"' in page)
rejected = client.post("/flight-airport", json={"airport": "SFOOO"})
check("a bad code is a 400, not a crash", rejected.status_code == 400, str(rejected.status_code))
check("a rejected code leaves the old one in place",
      rejected.get_json().get("flight_airport") == "SFO", str(rejected.get_json()))
cleared_airport = client.post("/flight-airport", json={"airport": ""}).get_json()
check("clearing the airport works", cleared_airport["flight_airport"] == "", str(cleared_airport))
check("clearing the airport leaves the mode alone",
      cleared_airport["current_mode"] == "flights", str(cleared_airport))

config.flight_file.unlink(missing_ok=True)
config.flight_airport_file.unlink(missing_ok=True)
config.set_mode("weather")

# --- plane sprite -------------------------------------------------------------
from ticker import icons
from ticker.modes.flights import BAR_Y, PLANE_Y, ROW_TWO_Y
from ticker.canvas import SMALL

art = icons.PLANE_RIGHT
check("the sprite is rectangular", len({len(row) for row in art}) == 1,
      str({len(row) for row in art}))
# Eleven wide is the whole point of the redraw: the nine-wide sprite had no room
# for a sweep or a tail gap and read as a cross with a dash through it.
check("the sprite is eleven columns wide", len(art[0]) == 11, f"got {len(art[0])}")
check("the sprite is seven rows tall", len(art) == 7, f"got {len(art)}")
check("the sprite clears row two's text", PLANE_Y >= ROW_TWO_Y + SMALL,
      f"plane at {PLANE_Y}, text ends {ROW_TWO_Y + SMALL - 1}")
check("the sprite clears the bar", PLANE_Y + len(art) <= BAR_Y,
      f"plane ends {PLANE_Y + len(art) - 1}, bar at {BAR_Y}")
check("every pixel has a colour",
      set("".join(art)) <= set(icons.PLANE_PALETTE) | {"."},
      str(set("".join(art)) - set(icons.PLANE_PALETTE) - {"."}))
# The fuselage is the only full-width row; the nose is its right-hand end.
mid = art[len(art) // 2]
check("the fuselage spans the sprite", mid == "P" * len(mid), mid)
check("the nose points right",
      all(row[-1] == "." for i, row in enumerate(art) if i != len(art) // 2))
# A wing swept the wrong way points the aircraft backwards, which is the kind of
# thing that survives code review and looks wrong on the panel.
tips = [row.index("P") for row in art if "P" in row]
check("the wings sweep back from the nose",
      tips[0] > tips[len(tips) // 2] and tips[-1] > tips[len(tips) // 2],
      str(tips))
# Bare fuselage between the wing root and the tailplane is what separates an
# airliner from a dart.
tail_rows = [i for i, row in enumerate(art) if row[0] == "P"]
check("the tailplane brackets the fuselage", tail_rows == [2, 3, 4], str(tail_rows))
check("the tailplane is clear of the wing",
      art[2][1:5] == "...." and art[4][1:5] == "....",
      f"{art[2]} / {art[4]}")


# --- random arrival picker -----------------------------------------------------
# Rows on a real arrivals board, trimmed to the fields the picker reads. The
# distinction that matters is a real departure with no real arrival: that and
# only that means the aircraft is between the two right now.
def _row(number, dep, arr):
    return {"flight": {"identification": {"number": {"default": number}},
                       "time": {"real": {"departure": dep, "arrival": arr}}}}


BOARD = {"result": {"response": {"airport": {"pluginData": {"schedule": {"arrivals": {"data": [
    _row("AS812", 1786661471, 1786677745),   # landed
    _row("CI4", 1786638141, None),           # airborne
    _row("WN3497", None, None),               # not pushed back
    _row("UA1757", 1786667885, None),        # airborne, delayed
    _row(" jx12 ", 1786636011, None),        # airborne, needs tidying
    _row("CI4", 1786638141, None),           # duplicate row
    _row(None, 1786636011, None),            # unusable number
]}}}}}}}
airborne = airborne_arrivals(BOARD)
check("only airborne legs are eligible", airborne == ["CI4", "UA1757", "JX12"], str(airborne))
check("a landed leg is excluded", "AS812" not in airborne)
check("a leg that has not departed is excluded", "WN3497" not in airborne)
check("numbers are deduplicated", airborne.count("CI4") == 1)
for junk in (None, {}, {"result": None}, {"result": {"response": {"airport": None}}},
             {"result": {"response": {"airport": {"pluginData": {"schedule":
              {"arrivals": {"data": "nope"}}}}}}}):
    check(f"a broken board yields nothing, not an exception: {str(junk)[:28]}",
          airborne_arrivals(junk) == [])


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Records the URLs asked for, and can be made to fail on demand."""

    def __init__(self, payload=BOARD):
        self.urls = []
        self.payload = payload
        self.fail = False

    def __call__(self, request, timeout=None):  # noqa: ANN001
        self.urls.append(request.full_url)
        if self.fail:
            raise OSError("network down")
        return _FakeResponse(self.payload)


opener = _Opener()
client = FlightRadarClient(opener=opener)
first = client.airborne_into("sfo")
check("the client reads the board", first == ["CI4", "UA1757", "JX12"], str(first))
check("a lowercase code is upper-cased for the endpoint",
      "code=SFO" in opener.urls[0], opener.urls[0])
# A present-but-empty timestamp answers 400, so it must not appear at all.
check("the endpoint carries no timestamp setting", "timestamp" not in ARRIVALS_URL)
client.airborne_into("SFO")
check("a fresh board is served from cache", len(opener.urls) == 1, str(len(opener.urls)))
check("an empty code asks for nothing", client.airborne_into("  ") == [] and len(opener.urls) == 1)

# A failure must not empty a board the panel is already using: a stale answer is
# better than telling the user nothing is flying when the truth is a dead endpoint.
stale = FlightRadarClient(opener=(bad := _Opener()))
stale.airborne_into("SFO")
stale._arrivals_cache["SFO"] = (-1e9, ["CI4"])
bad.fail = True
check("a failed refresh keeps the previous board", stale.airborne_into("SFO") == ["CI4"])
dead = _Opener()
dead.fail = True
check("a first-ever failure yields an empty board",
      FlightRadarClient(opener=dead).airborne_into("SFO") == [])


class _StubClient:
    """Stands in for the FR24 client so the mode can be driven offline."""

    def __init__(self, numbers):
        self.numbers = numbers
        self.calls = 0

    def airborne_into(self, airport):  # noqa: ANN001
        self.calls += 1
        return list(self.numbers)

    def lookup(self, flight):  # noqa: ANN001
        return None


config.flight_file.write_text("\n", encoding="utf-8")
config.flight_airport_file.write_text("SFO\n", encoding="utf-8")
mode = FlightsMode(config)
mode._client = stub = _StubClient(["CI4"])
mode.render(Canvas(config.width, config.height), 0)
check("a watched airport picks a flight", mode._flight == "CI4", mode._flight)
check("the board is asked for once", stub.calls == 1, str(stub.calls))
mode.render(Canvas(config.width, config.height), 1)
check("a pick is not re-rolled while the mode stays up", stub.calls == 1, str(stub.calls))

# Each switch into the mode builds a fresh object, which is what makes the pick
# change; over many visits a multi-flight board must produce more than one.
seen = set()
for _ in range(40):
    visit = FlightsMode(config)
    visit._client = _StubClient(["CI4", "UA1757", "JX12"])
    visit.render(Canvas(config.width, config.height), 0)
    seen.add(visit._flight)
check("each visit to the mode can pick a different flight", len(seen) > 1, str(sorted(seen)))
check("every pick comes from the board", seen <= {"CI4", "UA1757", "JX12"}, str(sorted(seen)))

empty = FlightsMode(config)
empty._client = _StubClient([])
canvas = Canvas(config.width, config.height)
empty.render(canvas, 0)
check("nothing airborne leaves no flight tracked", empty._flight == "")
check("nothing airborne still draws something", any(
    canvas.image_buffer.getpixel((x, y)) != (0, 0, 0)
    for x in range(config.width) for y in range(config.height)))
empty.render(canvas, 1)
check("an empty board is not retried every frame", empty._client.calls == 1,
      str(empty._client.calls))

switched = FlightsMode(config)
switched._client = _StubClient(["CI4"])
switched.render(Canvas(config.width, config.height), 0)
config.flight_airport_file.write_text("OAK\n", encoding="utf-8")
switched._client = _StubClient(["UA1757"])
switched.render(Canvas(config.width, config.height), 1)
check("changing airport re-picks immediately, ignoring the backoff",
      switched._flight == "UA1757" and switched._airport == "OAK", switched._flight)

# --- airport state ------------------------------------------------------------
config.set_flight_airport("sfo")
check("an airport is stored upper-cased", config.current_flight_airport() == "SFO")
check("choosing an airport clears the flight number", config.current_flight() == "")
config.set_flight("ua889")
check("typing a number clears the airport", config.current_flight_airport() == "")
check("the number survives", config.current_flight() == "UA889")
config.set_flight_airport("KSFO")
check("a four letter ICAO code is accepted", config.current_flight_airport() == "KSFO")
for bad_code in ("SF", "SFOOO", "SF1", "12345", "S F"):
    try:
        config.set_flight_airport(bad_code)
        check(f"a bad code is rejected: {bad_code!r}", False, "accepted")
    except ValueError:
        check(f"a bad code is rejected: {bad_code!r}", True)
config.set_flight_airport("")
check("an empty value clears the airport", config.current_flight_airport() == "")
config.flight_file.unlink(missing_ok=True)
config.flight_airport_file.unlink(missing_ok=True)

# --- report -------------------------------------------------------------------
failed = [c for c in checks if not c[1]]
for label, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
raise SystemExit(1 if failed else 0)
