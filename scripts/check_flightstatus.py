# MIT License — Copyright (c) 2026 John Kuok
"""Checks for the schedule-aware flight status provider.

Deliberately offline: every case is synthetic so the suite cannot be broken by
a flight landing early or an endpoint going down. Live behaviour is verified
separately in the render script.
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ticker.flightradar import (  # noqa: E402
    DELAY_THRESHOLD_MINUTES,
    KEEP_LANDED_HOURS,
    MAX_FUTURE_HOURS,
    FlightRadarClient,
    FlightStatus,
    _parse_leg,
    airline_prefix,
    choose_leg,
)

PASS = 0
FAIL: list[str] = []
# Anchored to the real clock: the client and the render path consult the wall
# clock internally, so a fabricated epoch would push every leg past the
# selection horizon and silently pass on a None result.
NOW = time.time()
HOUR = 3600


def check(label: str, got: object, want: object) -> None:
    global PASS
    if got == want:
        PASS += 1
    else:
        FAIL.append(f"{label}: got {got!r}, want {want!r}")


def approx(label: str, got: float | None, want: float | None, tol: float = 0.01) -> None:
    global PASS
    if got is None or want is None:
        check(label, got, want)
        return
    if abs(got - want) <= tol:
        PASS += 1
    else:
        FAIL.append(f"{label}: got {got!r}, want ~{want!r}")


# --------------------------------------------------------------- flight number

check("prefix UA889", airline_prefix("UA889"), "UA")
check("prefix BA286", airline_prefix("BA286"), "BA")
check("prefix UAL889 (ICAO)", airline_prefix("UAL889"), "UAL")
check("prefix lowercase", airline_prefix("ua889"), "UA")
check("prefix digits only", airline_prefix("889"), None)
check("prefix no digits", airline_prefix("B6"), None)
check("prefix single letter", airline_prefix("X1"), None)
check("prefix four letters", airline_prefix("ABCD1"), None)
check("prefix empty", airline_prefix(""), None)


# ------------------------------------------------------------------- leg parse


def leg_payload(
    *,
    generic: str = "scheduled",
    live: bool = False,
    sched_dep: int | None = None,
    sched_arr: int | None = None,
    real_dep: int | None = None,
    real_arr: int | None = None,
    est_arr: int | None = None,
    eta: int | None = None,
    airline: dict | None = None,
    leg_id: str | None = None,
    diverted: object = None,
    offset: int = -25200,
) -> dict:
    return {
        "identification": {"id": leg_id, "number": {"default": "UA889"}},
        "status": {
            "live": live,
            "generic": {"status": {"text": generic, "diverted": diverted}},
        },
        "airline": airline,
        "airport": {
            "origin": {"code": {"iata": "PEK"}},
            "destination": {"code": {"iata": "SFO"}, "timezone": {"offset": offset}},
        },
        "time": {
            "scheduled": {"departure": sched_dep, "arrival": sched_arr},
            "real": {"departure": real_dep, "arrival": real_arr},
            "estimated": {"arrival": est_arr},
            "other": {"eta": eta},
        },
    }


scheduled = _parse_leg(leg_payload(sched_dep=int(NOW + 2 * HOUR), sched_arr=int(NOW + 12 * HOUR)), "UA889")
check("scheduled state", scheduled.state, "scheduled")
check("scheduled origin", scheduled.origin, "PEK")
check("scheduled dest", scheduled.destination, "SFO")
check("scheduled offset", scheduled.dest_offset_seconds, -25200)
check("scheduled airline from prefix", scheduled.airline_iata, "UA")
check("scheduled progress is zero", scheduled.progress(NOW), 0.0)
check("scheduled delay unknown", scheduled.delay_minutes, None)

# A three-letter flight number prefix must land in the ICAO field, not IATA.
icao_leg = _parse_leg(leg_payload(sched_dep=int(NOW + HOUR), sched_arr=int(NOW + 5 * HOUR)), "UAL889")
check("icao prefix -> icao field", icao_leg.airline_icao, "UAL")
check("icao prefix leaves iata empty", icao_leg.airline_iata, None)

# A real airline object must win over the prefix guess.
branded = _parse_leg(
    leg_payload(sched_arr=int(NOW + HOUR), airline={"code": {"iata": "LH", "icao": "DLH"}}), "UA889"
)
check("payload airline wins", branded.airline_iata, "LH")

enroute = _parse_leg(
    leg_payload(
        generic="estimated",
        live=True,
        sched_dep=int(NOW - 2 * HOUR),
        sched_arr=int(NOW + 2 * HOUR),
        real_dep=int(NOW - 2 * HOUR),
        eta=int(NOW + 2 * HOUR),
    ),
    "UA889",
)
check("enroute state", enroute.state, "enroute")
check("enroute live flag", enroute.live, True)
approx("enroute halfway", enroute.progress(NOW), 0.5)
check("eta promoted to estimate", enroute.estimated_arrival, int(NOW + 2 * HOUR))

landed = _parse_leg(
    leg_payload(
        generic="landed",
        sched_dep=int(NOW - 10 * HOUR),
        sched_arr=int(NOW - HOUR),
        real_dep=int(NOW - 10 * HOUR),
        real_arr=int(NOW - HOUR + 1440),
        eta=int(NOW - HOUR + 1440),
    ),
    "UA889",
)
check("landed state", landed.state, "landed")
check("landed progress full", landed.progress(NOW), 1.0)
check("landed delay 24m", landed.delay_minutes, 24)
check("landed is late", landed.is_late, True)

# A landed leg must not treat its ETA as an "estimate" and double count.
check("landed best arrival is actual", landed.best_arrival, int(NOW - HOUR + 1440))

early = _parse_leg(
    leg_payload(
        generic="estimated",
        live=True,
        sched_dep=int(NOW - HOUR),
        sched_arr=int(NOW + 3 * HOUR),
        real_dep=int(NOW - HOUR),
        est_arr=int(NOW + 3 * HOUR - 38 * 60),
    ),
    "UA889",
)
check("early delay negative", early.delay_minutes, -38)
check("early is not late", early.is_late, False)

cancelled = _parse_leg(leg_payload(generic="canceled", sched_arr=int(NOW + HOUR)), "UA889")
check("cancelled state", cancelled.state, "cancelled")
check("cancelled has no progress", cancelled.progress(NOW), None)

diverted = _parse_leg(
    leg_payload(generic="landed", diverted="KOAK", sched_arr=int(NOW - HOUR), real_arr=int(NOW - HOUR)),
    "UA889",
)
check("diverted beats landed", diverted.state, "diverted")

# Departure with no arrival yet and no live flag still counts as airborne,
# which is the ocean-coverage-gap case the old code got wrong.
gap = _parse_leg(
    leg_payload(
        generic="estimated",
        live=False,
        sched_dep=int(NOW - 4 * HOUR),
        sched_arr=int(NOW + HOUR),
        real_dep=int(NOW - 4 * HOUR),
    ),
    "UA889",
)
check("departed but dark = enroute", gap.state, "enroute")
approx("progress keeps advancing in gap", gap.progress(NOW), 0.8)

empty = _parse_leg(leg_payload(generic="unknown"), "UA889")
check("unparseable leg dropped", empty, None)


# --------------------------------------------------------------- arrival clock

tz_leg = FlightStatus(
    flight_number="UA889",
    state="scheduled",
    scheduled_arrival=int(NOW),
    dest_offset_seconds=-25200,
)
local = tz_leg.local_arrival()
check("local arrival tzinfo applied", local.utcoffset().total_seconds(), -25200.0)
check(
    "local arrival differs from utc by offset",
    (local.hour - local.astimezone(tz=None).hour) % 24 in range(24),
    True,
)
check("no arrival -> no local time", FlightStatus(flight_number="X", state="scheduled").local_arrival(), None)
check(
    "no arrival -> no countdown",
    FlightStatus(flight_number="X", state="scheduled").minutes_until_arrival(NOW),
    None,
)


# -------------------------------------------------------------- leg  selection

def mk(state: str, *, dep: float | None = None, arr: float | None = None, live: bool = False) -> FlightStatus:
    return FlightStatus(
        flight_number="UA889",
        state=state,
        scheduled_departure=int(dep) if dep else None,
        actual_departure=int(dep) if (dep and state != "scheduled") else None,
        scheduled_arrival=int(arr) if arr else None,
        actual_arrival=int(arr) if (arr and state == "landed") else None,
        live=live,
    )


airborne = mk("enroute", dep=NOW - HOUR, arr=NOW + HOUR, live=True)
future = mk("scheduled", dep=NOW + 6 * HOUR, arr=NOW + 16 * HOUR)
past = mk("landed", dep=NOW - 12 * HOUR, arr=NOW - 2 * HOUR)

check("airborne wins over all", choose_leg([past, future, airborne], NOW), airborne)
check("next departure when nothing flying", choose_leg([past, future], NOW), future)
check("recent landing when nothing else", choose_leg([past], NOW), past)
check("empty list", choose_leg([], NOW), None)

# Nearest future departure, not merely the first in the list.
soon = mk("scheduled", dep=NOW + 2 * HOUR, arr=NOW + 11 * HOUR)
check("nearest future leg chosen", choose_leg([future, soon], NOW), soon)

# A leg beyond the horizon is not "next up".
far = mk("scheduled", dep=NOW + (MAX_FUTURE_HOURS + 5) * HOUR, arr=NOW + (MAX_FUTURE_HOURS + 15) * HOUR)
check("beyond horizon ignored", choose_leg([far], NOW), None)
check("inside horizon accepted", choose_leg([mk("scheduled", dep=NOW + (MAX_FUTURE_HOURS - 1) * HOUR, arr=NOW + MAX_FUTURE_HOURS * HOUR)], NOW) is not None, True)

# A stale landing is dropped so yesterday's flight never masquerades as today's.
stale = mk("landed", dep=NOW - 30 * HOUR, arr=NOW - (KEEP_LANDED_HOURS + 3) * HOUR)
check("stale landing dropped", choose_leg([stale], NOW), None)

# A departure in the past that never went is not "upcoming".
missed = mk("scheduled", dep=NOW - 3 * HOUR, arr=NOW - HOUR)
check("past scheduled dep ignored", choose_leg([missed], NOW), None)

# Live transmission preferred among two airborne legs.
dark = mk("enroute", dep=NOW - 3 * HOUR, arr=NOW + HOUR, live=False)
check("live leg preferred", choose_leg([dark, airborne], NOW), airborne)

# Most recent landing among several.
older = mk("landed", dep=NOW - 14 * HOUR, arr=NOW - 4 * HOUR)
check("latest landing chosen", choose_leg([older, past], NOW), past)


# ------------------------------------------------------------ client behaviour


class FakeResponse(io.BytesIO):
    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def make_opener(script: list[object]):  # noqa: ANN201
    """Return an opener that yields each scripted item in turn.

    A ``dict`` is served as JSON, an ``Exception`` is raised, and the recorded
    URL list lets a test assert how many requests were actually made.
    """
    calls: list[str] = []

    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.full_url)
        item = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode())

    return opener, calls


list_ok = {
    "result": {
        "response": {
            "data": [leg_payload(sched_dep=int(NOW + HOUR), sched_arr=int(NOW + 9 * HOUR))]
        }
    }
}

opener, calls = make_opener([list_ok])
client = FlightRadarClient(opener=opener)
first = client.lookup("UA889")
check("client resolves a leg", first is not None and first.origin, "PEK")
check("one request for one lookup", len(calls), 1)
client.lookup("UA889")
check("second lookup served from cache", len(calls), 1)

# Empty data means an unknown flight number; the alias search is then tried,
# and a failure to resolve must yield None rather than an exception.
opener, calls = make_opener([{"result": {"response": {"data": []}}}, {"results": []}])
check("unknown flight -> None", FlightRadarClient(opener=opener).lookup("NOPE999"), None)

# Network failure must return None, never raise.
opener, calls = make_opener([OSError("connection reset")])
check("network error -> None", FlightRadarClient(opener=opener).lookup("UA889"), None)

# Malformed JSON must also be survivable.
def bad_json_opener(request, timeout=None):  # noqa: ANN001, ARG001
    return FakeResponse(b"<html>blocked</html>")


check("bad payload -> None", FlightRadarClient(opener=bad_json_opener).lookup("UA889"), None)

# Blank input must not generate a request at all.
opener, calls = make_opener([list_ok])
check("blank flight -> None", FlightRadarClient(opener=opener).lookup("   "), None)
check("blank flight makes no request", len(calls), 0)

# An ICAO callsign resolves via the search endpoint, then re-lists.
alias_script = [
    {"result": {"response": {"data": []}}},
    {"results": [{"type": "operator", "id": "UAL"}, {"type": "schedule", "id": "UA889"}]},
    list_ok,
]
opener, calls = make_opener(alias_script)
resolved = FlightRadarClient(opener=opener).lookup("UAL889")
check("alias resolution works", resolved is not None and resolved.destination, "SFO")
check("alias took three requests", len(calls), 3)
check("alias hit the search endpoint", "search/web/find" in calls[1], True)

# Gate detail is fetched for a live leg and merged in.
live_list = {
    "result": {
        "response": {
            "data": [
                leg_payload(
                    generic="estimated",
                    live=True,
                    leg_id="41266936",
                    sched_dep=int(NOW - 2 * HOUR),
                    sched_arr=int(NOW + 2 * HOUR),
                    real_dep=int(NOW - 2 * HOUR),
                    eta=int(NOW + 2 * HOUR),
                )
            ]
        }
    }
}
detail = {"airport": {"destination": {"info": {"terminal": "I", "gate": "G5", "baggage": "CL2"}}}}
opener, calls = make_opener([live_list, detail])
merged = FlightRadarClient(opener=opener).lookup("UA889")
check("terminal merged", merged.terminal, "I")
check("gate merged", merged.gate, "G5")
check("baggage merged", merged.baggage, "CL2")
check("detail endpoint used", "clickhandler" in calls[1], True)

# Placeholder gate values must be treated as absent, not printed literally.
blank_detail = {"airport": {"destination": {"info": {"terminal": "-", "gate": "", "baggage": None}}}}
opener, _ = make_opener([live_list, blank_detail])
placeholder = FlightRadarClient(opener=opener).lookup("UA889")
check("dash terminal dropped", placeholder.terminal, None)
check("empty gate dropped", placeholder.gate, None)
check("null baggage dropped", placeholder.baggage, None)

# A scheduled leg has no id, so no detail request should be attempted.
opener, calls = make_opener([list_ok])
FlightRadarClient(opener=opener).lookup("UA889")
check("no detail request pre-departure", len(calls), 1)


# ------------------------------------------------------- rendering integration

from ticker.canvas import Canvas  # noqa: E402
from ticker.config import load_config  # noqa: E402
from ticker.modes.flights import FlightsMode  # noqa: E402

class StubConfig:
    """Wraps the real config but pins the flight number.

    ``_refresh`` compares ``current_flight()`` against the cached flight and
    wipes the injected status when they differ, which would otherwise make every
    render assertion below test the "NO FLIGHT SET" screen instead.
    """

    def __init__(self, inner, flight: str) -> None:  # noqa: ANN001
        self._inner = inner
        self._flight = flight

    def current_flight(self) -> str:
        return self._flight

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._inner, name)


config = load_config()
mode = FlightsMode(StubConfig(config, "UA889"))


def detail_texts(status: FlightStatus) -> list[str]:
    return [text for text, _ in mode._detail_items(status)]


landed_full = FlightStatus(
    flight_number="UA889",
    state="landed",
    origin="PEK",
    destination="SFO",
    scheduled_arrival=int(NOW - HOUR),
    actual_arrival=int(NOW - HOUR + 1440),
    baggage="CL2",
    gate="G5",
    terminal="I",
)
texts = detail_texts(landed_full)
check("landed shows LANDED first", texts[0], "LANDED")
check("landed surfaces delay", "LATE 24M" in texts, True)
check("landed surfaces baggage", "BAG CL2" in texts, True)
check("baggage precedes gate", texts.index("BAG CL2") < texts.index("GATE G5"), True)

cancel_status = FlightStatus(flight_number="UA889", state="cancelled", scheduled_arrival=int(NOW + HOUR))
check("cancelled is the only item", detail_texts(cancel_status), ["CANCELLED"])
check("diverted is the only item", detail_texts(FlightStatus(flight_number="X", state="diverted")), ["DIVERTED"])

tomorrow = FlightStatus(
    flight_number="UA889",
    state="scheduled",
    origin="PEK",
    destination="SFO",
    scheduled_departure=int(NOW + 10 * HOUR),
    scheduled_arrival=int(NOW + 20 * HOUR),
)
labels = detail_texts(tomorrow)
check("far-out flight names the weekday", any(len(t) == 3 and t.isalpha() for t in labels), True)

near = FlightStatus(
    flight_number="UA889",
    state="scheduled",
    origin="PEK",
    destination="SFO",
    scheduled_departure=int(NOW + HOUR),
    scheduled_arrival=int(NOW + 3 * HOUR),
)
check("near flight omits weekday", any(len(t) == 3 and t.isalpha() for t in detail_texts(near)), False)
check("empty status still yields an item", len(detail_texts(FlightStatus(flight_number="X", state="scheduled"))), 1)


# Every state must render without raising, and must light some pixels.
def render_once(status: FlightStatus | None, tick: int = 0) -> int:
    canvas = Canvas(config.width, config.height)
    canvas.clear()
    mode._flight = "UA889"
    mode._status = status
    pinned = time.monotonic()
    mode._status_checked = pinned
    mode._status_missing = status is None
    if status is None:
        mode._route = None
        mode._route_missing = True
        mode._route_checked = mode._position_checked = pinned
    mode.render(canvas, tick)
    return sum(1 for px in canvas.image_buffer.getdata() if any(px))


# A lookup that finds nothing must not be retried on the next frame. Before the
# miss backoff existed this re-entered the lookup thirty times a second.
probe_calls: list[str] = []


def counting_opener(request, timeout=None):  # noqa: ANN001, ARG001
    probe_calls.append(request.full_url)
    return FakeResponse(json.dumps({"result": {"response": {"data": []}}}).encode())


backoff_mode = FlightsMode(StubConfig(config, "ZZ9999"))
backoff_mode._client = FlightRadarClient(opener=counting_opener)
for _ in range(30):
    frame = Canvas(config.width, config.height)
    frame.clear()
    backoff_mode.render(frame, 0)
check("missing status backs off", backoff_mode._status_missing, True)
check("thirty frames is not thirty lookups", len(probe_calls) <= 2, True)

for name, status in [
    ("scheduled", tomorrow),
    ("near", near),
    ("enroute", enroute),
    ("landed", landed_full),
    ("cancelled", cancel_status),
    ("diverted", diverted),
]:
    lit = render_once(status)
    check(f"render {name} lights pixels", lit > 40, True)

# The rotating field must actually change across the rotation period.
frames = int(mode.DETAIL_ROTATE_SECONDS * config.fps)
snapshots = set()
for i in range(len(detail_texts(landed_full))):
    canvas = Canvas(config.width, config.height)
    canvas.clear()
    mode._flight = "UA889"
    mode._status = landed_full
    mode._status_checked = 1e9
    mode.render(canvas, i * frames)
    snapshots.add(canvas.image_buffer.tobytes())
check("rotation produces distinct frames", len(snapshots), len(detail_texts(landed_full)))

# Text must stay inside the panel for the widest realistic strings.
wide = FlightStatus(
    flight_number="QF7879",
    state="enroute",
    origin="PER",
    destination="LHR",
    scheduled_departure=int(NOW - 10 * HOUR),
    scheduled_arrival=int(NOW + 7 * HOUR),
    actual_departure=int(NOW - 10 * HOUR),
    estimated_arrival=int(NOW + 7 * HOUR + 125 * 60),
    baggage="CL12",
    terminal="T5",
    gate="A22",
)
for index in range(len(detail_texts(wide))):
    canvas = Canvas(config.width, config.height)
    canvas.clear()
    mode._flight = "QF7879"
    mode._status = wide
    mode._status_checked = 1e9
    mode.render(canvas, index * frames)
    # Nothing may be drawn in the last column pair unless it is the progress bar.
    pixels = canvas.image_buffer.load()
    overflow = any(
        any(pixels[canvas.width - 1, y]) for y in range(0, 22)
    )
    check(f"no right-edge overflow at rotation {index}", overflow, False)

for index, (text, _) in enumerate(mode._detail_items(wide)):
    width = Canvas(config.width, config.height).text_width(text, 8)
    leg_width = Canvas(config.width, config.height).text_width("PER-LHR", 8)
    check(f"detail {text!r} fits beside leg", 18 + leg_width + 2 <= config.width - width, True)


print(f"\n{PASS} checks passed")
if FAIL:
    print(f"{len(FAIL)} FAILURES:")
    for item in FAIL:
        print("  -", item)
    sys.exit(1)
print("FAILURES: none")
