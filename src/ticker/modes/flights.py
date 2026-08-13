# MIT License — Copyright (c) 2026 John Kuok
"""Track one flight number: airline, landing time, and a progress bar.

The job this screen actually does is "tell me when to leave for the airport",
so schedule data comes first and live ADS-B is the fallback:

* :mod:`ticker.flightradar` resolves a flight number to a specific leg with
  scheduled, estimated and actual times plus terminal, gate and baggage
  carousel. Crucially it works *before* the aircraft is airborne and while it
  is over an ocean out of receiver range, which is when the ADS-B path below
  goes blind.
* If that returns nothing - the endpoints are undocumented and may break - the
  original ADS-B pair below still drives the display, so the panel degrades to
  live-position-only instead of going blank.

The ADS-B fallback combines two free services because neither is sufficient
alone:

* ``adsbdb.com`` maps a flight number to its airline and its origin and
  destination airports with coordinates. It knows the route but nothing about
  where the aircraft is right now.
* ``adsb.lol`` reports live ADS-B positions by callsign. It knows where the
  aircraft is but returns no route, so progress and a landing time cannot be
  derived from it alone. (Its documented route endpoint answers 201 with an
  empty body, so it is not used.)

Route data from adsbdb may not be republished or built into another database, so
it is fetched at run time and held only in memory for the life of the process.
Nothing about a route is written to disk.

The landing estimate is ours, not an airline's: remaining great-circle distance
divided by current ground speed, plus a fixed allowance for descent and taxi. It
does not know the filed route, the arrival sequence, or holding, so treat it as
a good guess that tightens as the aircraft gets closer.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from ticker import airlines, icons
from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.flightradar import FlightRadarClient, FlightStatus
from ticker.modes.base import Mode

LOGGER = logging.getLogger(__name__)

ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
LIVE_URL = "https://api.adsb.lol/v2/callsign/{callsign}"

# Minutes added to the pure distance-over-speed estimate to stand in for the
# descent, approach and taxi that cruise ground speed cannot predict.
ARRIVAL_ALLOWANCE_MINUTES = 8

# Below this ground speed an aircraft is manoeuvring or taxiing, and dividing
# remaining distance by it produces a landing time hours in the future.
MIN_CRUISE_KNOTS = 80.0

LOGO_X = 0
TEXT_X = 18
ROW_ONE_Y = 0
ROW_TWO_Y = 13
PLANE_Y = 22
BAR_Y = 30

AMBER = (255, 205, 40)
WHITE = (235, 240, 250)
BLUE = (150, 190, 255)
DIM = (72, 84, 106)
GREEN = (60, 220, 120)
ORANGE = (255, 150, 90)
RED = (255, 90, 80)


def great_circle_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in nautical miles."""
    radius_nm = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius_nm * math.asin(min(1.0, math.sqrt(a)))


def normalise_flight_number(text: str) -> str:
    """Strip a typed flight number down to what the APIs accept.

    People write 'UA 889', 'ua889' and 'UAL889'; all three are the same flight.
    adsbdb accepts either the IATA or ICAO form, so only spacing and case need
    fixing here.
    """
    return "".join(text.split()).upper()


def format_duration(minutes: float) -> str:
    """Render minutes as 2H14M, or 46M under an hour."""
    total = max(0, int(round(minutes)))
    hours, remainder = divmod(total, 60)
    return f"{hours}H{remainder:02d}M" if hours else f"{remainder}M"


@dataclass(slots=True)
class Route:
    """Static facts about a flight number."""

    callsign_icao: str
    callsign_iata: str
    airline_iata: str
    airline_icao: str
    airline_name: str
    origin: str
    origin_lat: float
    origin_lon: float
    destination: str
    destination_lat: float
    destination_lon: float

    @property
    def total_nm(self) -> float:
        return great_circle_nm(
            self.origin_lat, self.origin_lon, self.destination_lat, self.destination_lon
        )


@dataclass(slots=True)
class Position:
    """Where the aircraft is now."""

    latitude: float
    longitude: float
    altitude_ft: int | None
    ground_speed_kt: float | None
    on_ground: bool
    registration: str | None
    aircraft_type: str | None


class FlightsMode(Mode):
    """Follow a single flight number from pushback to touchdown."""

    #: How often to ask for schedule data. The client caches internally, so
    #: this only needs to be often enough to catch a delay being published.
    STATUS_POLL_SECONDS = 45
    #: Backoff after a lookup that found nothing. Without a separate interval a
    #: flight number the schedule feed does not know would be retried on every
    #: render pass - thirty times a second.
    STATUS_MISS_SECONDS = 10 * 60
    #: Seconds each item stays on screen in the rotating detail field.
    DETAIL_ROTATE_SECONDS = 4

    LIVE_CACHE_SECONDS = 15
    ROUTE_CACHE_SECONDS = 6 * 60 * 60
    # A flight number that no route database recognises will never start
    # working mid-flight, so a failed lookup is retried slowly rather than
    # every render pass.
    ROUTE_MISS_SECONDS = 10 * 60

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._flight = ""
        self._route: Route | None = None
        self._route_checked = 0.0
        self._route_missing = False
        self._position: Position | None = None
        self._position_checked = 0.0
        self._position_missing = False
        self._client = FlightRadarClient()
        self._status: FlightStatus | None = None
        self._status_missing = False
        # Far enough in the past that the first render always looks, whatever
        # base value this platform's monotonic clock happens to start from.
        self._status_checked = -1e9
        self._static_root = Path(__file__).resolve().parents[1] / "web" / "static"

    # ------------------------------------------------------------------ fetch

    def _fetch_route(self, flight: str) -> None:
        try:
            response = requests.get(ROUTE_URL.format(callsign=flight), timeout=10)
            # adsbdb answers 404 with a JSON body for an unrecognised callsign,
            # which is information rather than a failure.
            if response.status_code == 404:
                self._route, self._route_missing = None, True
                return
            response.raise_for_status()
            payload = response.json().get("response")
            if not isinstance(payload, dict):
                self._route, self._route_missing = None, True
                return
            leg = payload["flightroute"]
            airline = leg.get("airline") or {}
            origin, destination = leg["origin"], leg["destination"]
            self._route = Route(
                callsign_icao=str(leg.get("callsign_icao") or flight),
                callsign_iata=str(leg.get("callsign_iata") or flight),
                airline_iata=str(airline.get("iata") or ""),
                airline_icao=str(airline.get("icao") or ""),
                airline_name=str(airline.get("name") or ""),
                origin=str(origin.get("iata_code") or origin.get("icao_code") or "???"),
                origin_lat=float(origin["latitude"]),
                origin_lon=float(origin["longitude"]),
                destination=str(destination.get("iata_code") or destination.get("icao_code") or "???"),
                destination_lat=float(destination["latitude"]),
                destination_lon=float(destination["longitude"]),
            )
            self._route_missing = False
        except Exception:
            # Keep any route already held; a transient network error should not
            # blank a display that was working a second ago.
            if self._route is None:
                self._route_missing = True
        finally:
            self._route_checked = time.monotonic()

    def _fetch_position(self, callsign: str) -> None:
        try:
            response = requests.get(LIVE_URL.format(callsign=callsign), timeout=10)
            response.raise_for_status()
            aircraft = response.json().get("ac") or []
            if not aircraft:
                self._position, self._position_missing = None, True
                return
            best = aircraft[0]
            altitude = best.get("alt_baro")
            # alt_baro is an integer of feet or the literal string "ground".
            on_ground = altitude == "ground"
            self._position = Position(
                latitude=float(best["lat"]),
                longitude=float(best["lon"]),
                altitude_ft=None if on_ground or altitude is None else int(altitude),
                ground_speed_kt=None if best.get("gs") is None else float(best["gs"]),
                on_ground=on_ground,
                registration=(best.get("r") or None),
                aircraft_type=(best.get("t") or None),
            )
            self._position_missing = False
        except Exception:
            self._position_missing = self._position is None
        finally:
            self._position_checked = time.monotonic()

    def _refresh(self) -> None:
        flight = normalise_flight_number(self.config.current_flight())
        if flight != self._flight:
            # A new flight number invalidates everything, including the misses.
            self._flight = flight
            self._route = self._position = None
            self._status = None
            self._route_checked = self._position_checked = 0.0
            self._status_checked = -1e9
            self._route_missing = self._position_missing = False
            self._status_missing = False
        if not flight:
            return

        now = time.monotonic()

        status_age_limit = (
            self.STATUS_MISS_SECONDS if self._status_missing else self.STATUS_POLL_SECONDS
        )
        if now - self._status_checked >= status_age_limit:
            self._status_checked = now
            try:
                self._status = self._client.lookup(flight)
            except Exception:  # noqa: BLE001 - a mode must never break the loop
                LOGGER.exception("flight status lookup failed")
                self._status = None
            self._status_missing = self._status is None

        if self._status is not None:
            # Schedule data covers everything the panel draws, so the ADS-B
            # requests are skipped entirely rather than made and discarded.
            return

        route_age_limit = self.ROUTE_MISS_SECONDS if self._route_missing else self.ROUTE_CACHE_SECONDS
        if self._route is None or now - self._route_checked >= route_age_limit:
            if now - self._route_checked >= route_age_limit:
                self._fetch_route(flight)

        callsign = self._route.callsign_icao if self._route else flight
        if now - self._position_checked >= self.LIVE_CACHE_SECONDS:
            self._fetch_position(callsign)

    # ------------------------------------------------------------- computation

    def _progress_and_eta(self) -> tuple[float | None, float | None]:
        """Fraction of the route flown and minutes until landing.

        Progress is measured by distance still to run rather than distance
        covered, so a diversion or a hold shows as progress stalling instead of
        a bar that keeps advancing.
        """
        route, position = self._route, self._position
        if route is None or position is None:
            return None, None

        remaining = great_circle_nm(
            position.latitude, position.longitude, route.destination_lat, route.destination_lon
        )
        total = route.total_nm
        progress = None if total <= 1 else max(0.0, min(1.0, 1.0 - remaining / total))

        speed = position.ground_speed_kt or 0.0
        if position.on_ground or speed < MIN_CRUISE_KNOTS:
            return progress, None
        minutes = (remaining / speed) * 60.0 + ARRIVAL_ALLOWANCE_MINUTES
        return progress, minutes

    def _clock_for(self, minutes: float) -> str:
        """Landing time on the wall clock, in the panel's configured zone."""
        from datetime import timedelta

        arrival = self.config.now() + timedelta(minutes=minutes)
        if self.config.clock_24h:
            return arrival.strftime("%H:%M")
        return arrival.strftime("%-I:%M%p").replace("AM", "A").replace("PM", "P")

    # ----------------------------------------------------------------- drawing

    def _draw_bar(self, canvas: Canvas, progress: float | None, accent: tuple[int, int, int]) -> None:
        """Two-pixel progress bar with the plane riding its leading edge."""
        # An unlit track leaves the bar with no visible extent, so the empty
        # portion is drawn dim rather than black.
        canvas.fill_rect(0, BAR_Y, canvas.width, 2, (30, 38, 54))
        if progress is None:
            # No route means no meaningful fraction; a dotted track says
            # "tracking, but position along the route is unknown" rather than
            # implying the flight has not left.
            canvas.dotted_hline(BAR_Y, DIM, 0, canvas.width, step=4)
            return

        filled = int(round(progress * canvas.width))
        canvas.fill_rect(0, BAR_Y, filled, 2, accent)

        plane_width = len(icons.PLANE_RIGHT[0])
        # Centre the sprite on the leading edge, then keep it fully on-panel so
        # the nose is not clipped away at 100 per cent.
        plane_x = max(0, min(canvas.width - plane_width, filled - plane_width // 2))
        canvas.sprite(plane_x, PLANE_Y, icons.PLANE_RIGHT, icons.PLANE_PALETTE)

    def _draw_message(self, canvas: Canvas, headline: str, detail: str, color) -> None:  # noqa: ANN001
        canvas.text(TEXT_X, ROW_ONE_Y, canvas.fit(headline, canvas.width - TEXT_X, MEDIUM), color, MEDIUM)
        canvas.text(TEXT_X, ROW_TWO_Y, canvas.fit(detail, canvas.width - TEXT_X), DIM, SMALL)

    def _format_clock(self, moment) -> str:  # noqa: ANN001
        """Wall-clock time, already in the destination airport's zone."""
        if self.config.clock_24h:
            return moment.strftime("%H:%M")
        return moment.strftime("%-I:%M%p").replace("AM", "A").replace("PM", "P")

    def _detail_items(self, status: FlightStatus) -> list[tuple[str, tuple[int, int, int]]]:
        """Build the rotating right-hand field on row two.

        Only facts that exist are offered, so the field never shows a blank or a
        placeholder. Order matters: the most decision-relevant item first, since
        with a single item there is no rotation at all.
        """
        items: list[tuple[str, tuple[int, int, int]]] = []

        if status.state == "cancelled":
            return [("CANCELLED", RED)]
        if status.state == "diverted":
            return [("DIVERTED", RED)]

        minutes = status.minutes_until_arrival()
        if status.state == "landed":
            items.append(("LANDED", GREEN))
        elif minutes is not None and minutes > 0:
            items.append((f"IN {format_duration(minutes)}", WHITE))

        delay = status.delay_minutes
        if delay is not None and abs(delay) >= 5:
            if delay > 0:
                items.append((f"LATE {format_duration(delay)}", ORANGE))
            else:
                items.append((f"EARLY {format_duration(-delay)}", GREEN))

        # Baggage before gate: for meeting an arriving passenger the carousel is
        # where they end up, the arrival gate is where you cannot go.
        if status.baggage:
            items.append((f"BAG {status.baggage}", BLUE))
        if status.terminal:
            items.append((f"TERM {status.terminal}", BLUE))
        if status.gate:
            items.append((f"GATE {status.gate}", BLUE))

        # A flight landing tomorrow shown as a bare "2:20P" is genuinely
        # misleading, so name the day once it is more than 12 hours out.
        local = status.local_arrival()
        if local is not None and minutes is not None and minutes > 12 * 60:
            items.append((local.strftime("%a").upper(), AMBER))

        if not items:
            items.append(("SCHEDULED", DIM))
        return items

    def _render_status(self, canvas: Canvas, tick: int, status: FlightStatus) -> None:
        """Draw the schedule-driven layout."""
        airlines.draw_logo(
            canvas, LOGO_X, 0, status.airline_iata, self._static_root, status.airline_icao
        )
        accent = airlines.brand_for(status.airline_iata)[0]
        if sum(accent) < 210:
            accent = (90, 140, 220)

        canvas.text(TEXT_X, ROW_ONE_Y, canvas.fit(status.flight_number, 62, MEDIUM), WHITE, MEDIUM)

        # Row one right: the landing time is the headline number. Colour carries
        # the schedule verdict so it reads correctly at a glance from a sofa.
        local = status.local_arrival()
        if local is None:
            headline, headline_color = "--:--", DIM
        else:
            headline = self._format_clock(local)
            if status.state == "cancelled" or status.state == "diverted":
                headline_color = RED
            elif status.is_late:
                headline_color = ORANGE
            elif status.state == "landed":
                headline_color = GREEN
            elif status.state == "enroute":
                headline_color = GREEN
            else:
                # Not airborne yet: this is a timetable, not an estimate.
                headline_color = WHITE
        headline_width = canvas.text_width(headline, MEDIUM)
        canvas.text(canvas.width - headline_width, ROW_ONE_Y, headline, headline_color, MEDIUM)

        # Row two left: the leg.
        leg = f"{status.origin or '???'}-{status.destination or '???'}"
        canvas.text(TEXT_X, ROW_TWO_Y, leg, BLUE, SMALL)

        items = self._detail_items(status)
        frames = max(1, int(self.DETAIL_ROTATE_SECONDS * max(1, self.config.fps)))
        text, color = items[(tick // frames) % len(items)]
        text_width = canvas.text_width(text, SMALL)
        canvas.text(canvas.width - text_width, ROW_TWO_Y, text, color, SMALL)

        self._draw_bar(canvas, status.progress(), accent)

    def render(self, canvas: Canvas, tick: int) -> None:
        self._refresh()
        canvas.clear()

        if not self._flight:
            canvas.text_centered(6, "NO FLIGHT SET", AMBER, SMALL)
            canvas.text_centered(18, "ENTER ONE IN THE WEB APP", DIM, SMALL)
            return

        if self._status is not None:
            self._render_status(canvas, tick, self._status)
            return

        route = self._route
        airline_code = route.airline_iata if route else ""
        airline_icao = route.airline_icao if route else ""
        airlines.draw_logo(canvas, LOGO_X, 0, airline_code, self._static_root, airline_icao)
        accent = airlines.brand_for(airline_code)[0]
        # A very dark livery makes an invisible progress bar, so the bar borrows
        # a guaranteed-visible colour instead of the brand background.
        if sum(accent) < 210:
            accent = (90, 140, 220)

        # Operators without an IATA code come back with a stub IATA callsign
        # like "34", which is not a flight number anyone would recognise, so a
        # suspiciously short one is discarded in favour of the ICAO callsign.
        label = self._flight
        if route is not None:
            label = route.callsign_iata if len(route.callsign_iata) >= 4 else route.callsign_icao
        canvas.text(TEXT_X, ROW_ONE_Y, canvas.fit(label, 62, MEDIUM), WHITE, MEDIUM)

        progress, eta_minutes = self._progress_and_eta()

        if route is None:
            detail = "NO ROUTE FOUND" if self._route_missing else "LOOKING UP ROUTE"
            canvas.text(TEXT_X, ROW_TWO_Y, detail, ORANGE if self._route_missing else DIM, SMALL)
            self._draw_bar(canvas, None, accent)
            return

        # Right-hand status on row one: the landing time is the headline number.
        if eta_minutes is not None:
            status, status_color = self._clock_for(eta_minutes), GREEN
        elif self._position is None:
            status, status_color = "--:--", DIM
        elif self._position.on_ground:
            status, status_color = "GROUND", AMBER
        else:
            status, status_color = "SLOW", AMBER
        status_width = canvas.text_width(status, MEDIUM)
        canvas.text(canvas.width - status_width, ROW_ONE_Y, status, status_color, MEDIUM)

        # Row two: the leg on the left, time remaining or a reason on the right.
        canvas.text(TEXT_X, ROW_TWO_Y, f"{route.origin}-{route.destination}", BLUE, SMALL)
        if eta_minutes is not None:
            right = f"IN {format_duration(eta_minutes)}"
            right_color = WHITE
        elif self._position is None:
            right = "NO SIGNAL" if self._position_missing else "SEARCHING"
            right_color = RED if self._position_missing else DIM
        elif progress is not None and progress > 0.98:
            right = "LANDED"
            right_color = GREEN
        else:
            right = "ON GROUND"
            right_color = AMBER
        right_width = canvas.text_width(right, SMALL)
        canvas.text(canvas.width - right_width, ROW_TWO_Y, right, right_color, SMALL)

        self._draw_bar(canvas, progress, accent)
