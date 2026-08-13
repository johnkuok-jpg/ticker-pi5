"""Schedule-aware flight status lookup.

The ADS-B path in :mod:`ticker.modes.flights` only knows about aircraft that are
airborne *right now* and within range of a volunteer receiver. That is useless
for the main job this display does: telling you when a friend's inbound flight
lands, hours before it takes off and while it is over an ocean.

This module adds a schedule-aware provider on top. It reads the same JSON
endpoints that the Flightradar24 web front-end calls, which need no API key:

* ``flight/list.json`` - every past and upcoming leg for a flight number,
  with scheduled/estimated/actual times and both airports.
* ``clickhandler`` - richer detail for one specific leg, including terminal,
  gate and baggage carousel. Only available once a leg has a real ``id``,
  i.e. from roughly check-in time onwards.

These are undocumented internal endpoints rather than a supported public API.
They can change shape or start refusing requests without notice, so every
failure path here returns ``None`` and the caller is expected to fall back to
the live ADS-B lookup. Nothing in this module may raise on network trouble.

Polling is deliberately gentle - one flight number, a couple of requests a
minute at most - to stay within the spirit of personal use.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

LOGGER = logging.getLogger(__name__)

LIST_URL = (
    "https://api.flightradar24.com/common/v1/flight/list.json"
    "?query={query}&fetchBy=flight&page=1&limit=25"
)
DETAIL_URL = "https://data-live.flightradar24.com/clickhandler/?flight={flight_id}"
SEARCH_URL = "https://www.flightradar24.com/v1/search/web/find?query={query}&limit=10"

# The endpoints reject the default urllib agent.
BROWSER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 12.0

#: How long a leg listing stays fresh. Schedules move slowly.
LIST_CACHE_SECONDS = 150.0
#: Detail (gate/baggage) refresh while a leg is live.
DETAIL_CACHE_SECONDS = 90.0
#: After a failure, wait this long before trying again.
ERROR_BACKOFF_SECONDS = 120.0

#: Ignore upcoming legs further out than this - we want the next arrival, not
#: the same flight number three weeks from now.
MAX_FUTURE_HOURS = 36.0
#: Keep showing a landed flight this long, so the display still reads
#: "LANDED 2:32 PM" while you are actually driving to the airport.
KEEP_LANDED_HOURS = 5.0

#: Treat a schedule difference below this as "on time".
DELAY_THRESHOLD_MINUTES = 5

_TERMINAL_STATES = frozenset({"landed", "cancelled", "diverted"})


def _now() -> float:
    return time.time()


def airline_prefix(flight_number: str) -> str | None:
    """Pull the carrier code off the front of a flight number.

    ``UA889`` -> ``UA``, ``BA286`` -> ``BA``, ``UAL889`` -> ``UAL``. Scheduled
    legs come back with ``airline: null``, so this is the only way to brand the
    logo tile before departure.
    """
    letters = ""
    for char in flight_number.strip().upper():
        if char.isalpha():
            letters += char
        else:
            break
    if len(letters) in (2, 3):
        return letters
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _dig(payload: object, *keys: str) -> object:
    """Walk nested dicts, tolerating ``None`` at any level."""
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


@dataclass
class FlightStatus:
    """One leg of a flight number, resolved to what the panel needs."""

    flight_number: str
    state: str
    """One of scheduled, enroute, landed, cancelled, diverted."""

    origin: str | None = None
    destination: str | None = None
    dest_offset_seconds: int = 0
    airline_iata: str | None = None
    airline_icao: str | None = None

    scheduled_departure: int | None = None
    actual_departure: int | None = None
    scheduled_arrival: int | None = None
    estimated_arrival: int | None = None
    actual_arrival: int | None = None

    terminal: str | None = None
    gate: str | None = None
    baggage: str | None = None

    leg_id: str | None = None
    live: bool = False

    @property
    def best_arrival(self) -> int | None:
        """The most trustworthy arrival timestamp available."""
        return self.actual_arrival or self.estimated_arrival or self.scheduled_arrival

    @property
    def delay_minutes(self) -> int | None:
        """Signed minutes late against schedule, or ``None`` if unknowable."""
        arrival = self.actual_arrival or self.estimated_arrival
        if arrival is None or self.scheduled_arrival is None:
            return None
        return int(round((arrival - self.scheduled_arrival) / 60.0))

    @property
    def is_late(self) -> bool:
        delay = self.delay_minutes
        return delay is not None and delay >= DELAY_THRESHOLD_MINUTES

    def minutes_until_arrival(self, now: float | None = None) -> float | None:
        arrival = self.best_arrival
        if arrival is None:
            return None
        return (arrival - (now if now is not None else _now())) / 60.0

    def local_arrival(self) -> datetime | None:
        """Arrival wall-clock time *at the destination airport*.

        Uses the fixed offset supplied in the payload rather than a timezone
        database, which keeps this dependency-free and correct for the one
        instant we care about.
        """
        arrival = self.best_arrival
        if arrival is None:
            return None
        tz = timezone(timedelta(seconds=self.dest_offset_seconds))
        return datetime.fromtimestamp(arrival, tz=tz)

    def progress(self, now: float | None = None) -> float | None:
        """Fraction of the flight completed, by elapsed time.

        Time-based rather than distance-based on purpose: it keeps advancing
        while the aircraft is over an ocean with no ADS-B coverage, which is
        exactly when the distance-based bar used to freeze.
        """
        if self.state in _TERMINAL_STATES:
            return 1.0 if self.state == "landed" else None
        if self.state == "scheduled":
            return 0.0
        start = self.actual_departure or self.scheduled_departure
        end = self.best_arrival
        if start is None or end is None or end <= start:
            return None
        moment = now if now is not None else _now()
        return max(0.0, min(1.0, (moment - start) / (end - start)))


def _parse_leg(raw: dict, flight_number: str) -> FlightStatus | None:
    airports = raw.get("airport") or {}
    origin = _dig(airports, "origin", "code", "iata")
    destination = _dig(airports, "destination", "code", "iata")
    offset = _as_int(_dig(airports, "destination", "timezone", "offset")) or 0

    times = raw.get("time") or {}
    scheduled_departure = _as_int(_dig(times, "scheduled", "departure"))
    scheduled_arrival = _as_int(_dig(times, "scheduled", "arrival"))
    actual_departure = _as_int(_dig(times, "real", "departure"))
    actual_arrival = _as_int(_dig(times, "real", "arrival"))
    estimated_arrival = _as_int(_dig(times, "estimated", "arrival"))
    eta = _as_int(_dig(times, "other", "eta"))

    generic = str(_dig(raw, "status", "generic", "status", "text") or "").lower()
    diverted = _dig(raw, "status", "generic", "status", "diverted")
    live = bool(_dig(raw, "status", "live"))

    if diverted:
        state = "diverted"
    elif generic in ("canceled", "cancelled"):
        state = "cancelled"
    elif generic == "landed" or actual_arrival is not None:
        state = "landed"
    elif live or actual_departure is not None:
        state = "enroute"
    elif generic == "scheduled" or scheduled_departure is not None:
        state = "scheduled"
    else:
        return None

    # An ETA only means "estimated arrival" for a flight still in the air;
    # on a landed leg the same field just repeats the touchdown time.
    if state == "enroute" and estimated_arrival is None and eta is not None:
        estimated_arrival = eta

    airline_iata = _dig(raw, "airline", "code", "iata")
    airline_icao = _dig(raw, "airline", "code", "icao")
    if not airline_iata and not airline_icao:
        # Scheduled legs carry no airline object at all.
        prefix = airline_prefix(flight_number)
        if prefix and len(prefix) == 2:
            airline_iata = prefix
        elif prefix:
            airline_icao = prefix

    return FlightStatus(
        flight_number=flight_number,
        state=state,
        origin=str(origin) if origin else None,
        destination=str(destination) if destination else None,
        dest_offset_seconds=offset,
        airline_iata=str(airline_iata) if airline_iata else None,
        airline_icao=str(airline_icao) if airline_icao else None,
        scheduled_departure=scheduled_departure,
        actual_departure=actual_departure,
        scheduled_arrival=scheduled_arrival,
        estimated_arrival=estimated_arrival,
        actual_arrival=actual_arrival,
        leg_id=str(_dig(raw, "identification", "id") or "") or None,
        live=live,
    )


def choose_leg(legs: list[FlightStatus], now: float | None = None) -> FlightStatus | None:
    """Pick the leg a person waiting at arrivals actually cares about.

    Priority: something in the air now, else the next departure inside
    :data:`MAX_FUTURE_HOURS`, else a very recent landing, else nothing.
    """
    moment = now if now is not None else _now()

    airborne = [leg for leg in legs if leg.state == "enroute"]
    if airborne:
        # Prefer a leg still transmitting; otherwise the latest departure.
        airborne.sort(key=lambda leg: (leg.live, leg.actual_departure or 0), reverse=True)
        return airborne[0]

    upcoming = [
        leg
        for leg in legs
        if leg.state == "scheduled"
        and leg.scheduled_departure is not None
        and 0 <= (leg.scheduled_departure - moment) <= MAX_FUTURE_HOURS * 3600
    ]
    if upcoming:
        upcoming.sort(key=lambda leg: leg.scheduled_departure or 0)
        return upcoming[0]

    recent = [
        leg
        for leg in legs
        if leg.state in _TERMINAL_STATES
        and leg.best_arrival is not None
        and 0 <= (moment - leg.best_arrival) <= KEEP_LANDED_HOURS * 3600
    ]
    if recent:
        recent.sort(key=lambda leg: leg.best_arrival or 0, reverse=True)
        return recent[0]

    return None


class FlightRadarClient:
    """Cached, failure-tolerant reader for the two FR24 endpoints."""

    def __init__(self, *, opener=None) -> None:  # noqa: ANN001
        self._opener = opener or urllib.request.urlopen
        self._list_cache: dict[str, tuple[float, list[FlightStatus]]] = {}
        self._detail_cache: dict[str, tuple[float, dict[str, str | None]]] = {}
        self._alias: dict[str, str | None] = {}
        self._blocked_until = 0.0

    def _get_json(self, url: str) -> dict | None:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": BROWSER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            LOGGER.warning("flight status request failed: %s", exc)
            self._blocked_until = _now() + ERROR_BACKOFF_SECONDS
            return None
        except (ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("flight status payload unreadable: %s", exc)
            self._blocked_until = _now() + ERROR_BACKOFF_SECONDS
            return None
        return payload if isinstance(payload, dict) else None

    def _legs(self, flight_number: str) -> list[FlightStatus] | None:
        cached = self._list_cache.get(flight_number)
        if cached and _now() - cached[0] < LIST_CACHE_SECONDS:
            return cached[1]

        payload = self._get_json(LIST_URL.format(query=flight_number))
        if payload is None:
            return cached[1] if cached else None

        raw_legs = _dig(payload, "result", "response", "data")
        if not isinstance(raw_legs, list):
            # A valid response with no data means the flight number is unknown.
            self._list_cache[flight_number] = (_now(), [])
            return []

        legs: list[FlightStatus] = []
        for raw in raw_legs:
            if not isinstance(raw, dict):
                continue
            leg = _parse_leg(raw, flight_number)
            if leg is not None:
                legs.append(leg)
        self._list_cache[flight_number] = (_now(), legs)
        return legs

    def _detail(self, leg_id: str) -> dict[str, str | None]:
        cached = self._detail_cache.get(leg_id)
        if cached and _now() - cached[0] < DETAIL_CACHE_SECONDS:
            return cached[1]

        payload = self._get_json(DETAIL_URL.format(flight_id=leg_id))
        if payload is None:
            return cached[1] if cached else {}

        info = _dig(payload, "airport", "destination", "info")
        detail: dict[str, str | None] = {}
        if isinstance(info, dict):
            for key, field in (("terminal", "terminal"), ("gate", "gate"), ("baggage", "baggage")):
                value = info.get(field)
                detail[key] = str(value) if value not in (None, "", "-") else None
        self._detail_cache[leg_id] = (_now(), detail)
        return detail

    def _resolve_alias(self, flight_number: str) -> str | None:
        """Map an ICAO callsign onto the IATA flight number the listing wants.

        ``flight/list.json`` only answers to IATA form (``UA889``), but people
        read ICAO callsigns (``UAL889``) off tracking sites. Rather than ship a
        carrier code table that would inevitably go stale, ask FR24's own
        search endpoint what this string means.
        """
        if flight_number in self._alias:
            return self._alias[flight_number]

        payload = self._get_json(SEARCH_URL.format(query=flight_number))
        if payload is None:
            # Transient failure - do not cache a negative answer, or one blip
            # would permanently convince us this flight number is unknown.
            return None
        resolved: str | None = None
        results = payload.get("results")
        if isinstance(results, list):
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "schedule":
                    continue
                candidate = entry.get("id")
                if isinstance(candidate, str) and candidate and candidate != flight_number:
                    resolved = candidate.upper()
                    break
        self._alias[flight_number] = resolved
        return resolved

    def lookup(self, flight_number: str) -> FlightStatus | None:
        """Resolve a flight number to the leg worth showing, or ``None``.

        ``None`` means "no schedule data" - the caller should fall back to the
        live ADS-B lookup rather than blanking the display.
        """
        flight_number = (flight_number or "").strip().upper()
        if not flight_number:
            return None
        if _now() < self._blocked_until:
            cached = self._list_cache.get(flight_number)
            if not cached:
                return None
            return choose_leg(cached[1])

        legs = self._legs(flight_number)
        if not legs:
            alias = self._resolve_alias(flight_number)
            if not alias:
                return None
            legs = self._legs(alias)
            if not legs:
                return None

        leg = choose_leg(legs)
        if leg is None:
            return None

        # Gate and baggage only exist once the leg has a real id.
        if leg.leg_id and leg.state in ("enroute", "landed"):
            detail = self._detail(leg.leg_id)
            leg.terminal = detail.get("terminal")
            leg.gate = detail.get("gate")
            leg.baggage = detail.get("baggage")
        return leg
