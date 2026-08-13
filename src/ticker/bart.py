# MIT License — Copyright (c) 2026 John Kuok
"""BART real-time departures from the agency's public ETD endpoint.

``api.bart.gov`` is a documented public API and the key below is the one BART
itself publishes for open use, so unlike the flight endpoints this module is on
solid ground: https://api.bart.gov/docs/overview/index.aspx

One request to ``cmd=etd`` returns every upcoming train at a station, already
grouped by destination. A platform sign does not think in destinations though -
it lists the next trains in the order they arrive - so the groups are flattened
and re-sorted by minutes. Estimates are per-train, carrying their own line
colour, platform, car count and delay.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

LOGGER = logging.getLogger(__name__)

ETD_URL = "https://api.bart.gov/api/etd.aspx"
PUBLIC_KEY = "MW9S-E7SL-26DU-VV8V"
REQUEST_TIMEOUT = 8.0
USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

DEFAULT_STATION = "EMBR"

# Every station, in the order BART lists them. Hard-coded rather than fetched
# because the web app needs the list to build its picker before any network call
# has happened, and the roster changes on the order of once every few years.
STATIONS: tuple[tuple[str, str], ...] = (
    ("12TH", "12th St. Oakland City Center"),
    ("16TH", "16th St. Mission"),
    ("19TH", "19th St. Oakland"),
    ("24TH", "24th St. Mission"),
    ("ANTC", "Antioch"),
    ("ASHB", "Ashby"),
    ("BALB", "Balboa Park"),
    ("BAYF", "Bay Fair"),
    ("BERY", "Berryessa/North San Jose"),
    ("CAST", "Castro Valley"),
    ("CIVC", "Civic Center/UN Plaza"),
    ("COLS", "Coliseum"),
    ("COLM", "Colma"),
    ("CONC", "Concord"),
    ("DALY", "Daly City"),
    ("DBRK", "Downtown Berkeley"),
    ("DUBL", "Dublin/Pleasanton"),
    ("DELN", "El Cerrito del Norte"),
    ("PLZA", "El Cerrito Plaza"),
    ("EMBR", "Embarcadero"),
    ("FRMT", "Fremont"),
    ("FTVL", "Fruitvale"),
    ("GLEN", "Glen Park"),
    ("HAYW", "Hayward"),
    ("LAFY", "Lafayette"),
    ("LAKE", "Lake Merritt"),
    ("MCAR", "MacArthur"),
    ("MLBR", "Millbrae"),
    ("MLPT", "Milpitas"),
    ("MONT", "Montgomery St."),
    ("NBRK", "North Berkeley"),
    ("NCON", "North Concord/Martinez"),
    ("OAKL", "Oakland International Airport"),
    ("ORIN", "Orinda"),
    ("PITT", "Pittsburg/Bay Point"),
    ("PCTR", "Pittsburg Center"),
    ("PHIL", "Pleasant Hill/Contra Costa Centre"),
    ("POWL", "Powell St."),
    ("RICH", "Richmond"),
    ("ROCK", "Rockridge"),
    ("SBRN", "San Bruno"),
    ("SFIA", "San Francisco International Airport"),
    ("SANL", "San Leandro"),
    ("SHAY", "South Hayward"),
    ("SSAN", "South San Francisco"),
    ("UCTY", "Union City"),
    ("WCRK", "Walnut Creek"),
    ("WARM", "Warm Springs/South Fremont"),
    ("WDUB", "West Dublin/Pleasanton"),
    ("WOAK", "West Oakland"),
)

STATION_NAMES: dict[str, str] = dict(STATIONS)

# Station names are written for a website. "Pleasant Hill/Contra Costa Centre"
# is 33 characters against a column that holds about 18, and truncating it gives
# "PLEASANT HILL/CONT". These are the panel spellings, applied to both the board
# title and the destination column.
PANEL_NAMES: dict[str, str] = {
    "12TH": "12TH ST OAK",
    "16TH": "16TH ST",
    "19TH": "19TH ST OAK",
    "24TH": "24TH ST",
    "BERY": "BERRYESSA",
    "CIVC": "CIVIC CENTER",
    "DBRK": "DTWN BERKELEY",
    "DUBL": "DUBLIN",
    "DELN": "EL CERRITO DN",
    "PLZA": "EL CERRITO PL",
    "LAKE": "LAKE MERRITT",
    "MONT": "MONTGOMERY",
    "NBRK": "N BERKELEY",
    "NCON": "N CONCORD",
    "OAKL": "OAK AIRPORT",
    "PITT": "PITTSBURG",
    "PCTR": "PITTSBURG CTR",
    "PHIL": "PLEASANT HILL",
    "POWL": "POWELL ST",
    "SFIA": "SFO AIRPORT",
    "SANL": "SAN LEANDRO",
    "SHAY": "S HAYWARD",
    "SSAN": "S SAN FRAN",
    "WARM": "WARM SPRINGS",
    "WDUB": "W DUBLIN",
    "WOAK": "W OAKLAND",
}

# BART's own line colours, retuned for an LED panel. The payload carries a
# ``hexcolor``, but the literal values are chosen for white paper: #ff0000 has a
# perceptual luminance of 54 against a 60 floor, and #339933 is darker still.
# Keying on the stable colour *name* instead lets each line keep its identity at
# a brightness that survives a dim panel. Unknown lines fall back to hexcolor.
LINE_COLORS: dict[str, tuple[int, int, int]] = {
    "YELLOW": (255, 225, 40),
    "ORANGE": (255, 145, 35),
    "GREEN": (45, 220, 110),
    "RED": (255, 70, 70),
    "BLUE": (95, 155, 255),
    "PURPLE": (175, 115, 255),
    "WHITE": (235, 240, 250),
    "GRAY": (150, 165, 195),
    "GREY": (150, 165, 195),
}
FALLBACK_LINE = (150, 165, 195)

# A train BART flags as this far behind schedule gets its countdown drawn in
# amber. The countdown itself already absorbs the delay, so without a marker a
# chronically late line looks identical to one running clean.
#
# Five minutes, not two. A live Embarcadero sample had every one of the seven
# tracked trains reporting some delay, six of them between 97 and 227 seconds:
# BART simply runs a couple of minutes behind as a matter of course. At a
# two-minute bar the whole board goes amber and the colour stops meaning
# anything, so the threshold sits where a rider would actually change plans.
DELAY_WARN_SECONDS = 300

Opener = Callable[[str], bytes]


def panel_name(abbr: str) -> str:
    """Panel spelling of a station, falling back to its uppercased full name."""
    abbr = abbr.upper()
    if abbr in PANEL_NAMES:
        return PANEL_NAMES[abbr]
    return STATION_NAMES.get(abbr, abbr).upper()


def is_station(abbr: str) -> bool:
    return abbr.upper() in STATION_NAMES


@dataclass(frozen=True)
class Departure:
    destination: str
    label: str
    minutes: int
    color: tuple[int, int, int]
    platform: str
    direction: str
    cars: int
    delay_seconds: int

    @property
    def is_leaving(self) -> bool:
        return self.minutes <= 0

    @property
    def is_delayed(self) -> bool:
        return self.delay_seconds >= DELAY_WARN_SECONDS

    def countdown(self) -> str:
        return "NOW" if self.is_leaving else f"{self.minutes}M"


@dataclass(frozen=True)
class Board:
    station: str
    name: str
    departures: tuple[Departure, ...]
    message: str = ""


def _default_opener(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def _line_color(estimate: dict) -> tuple[int, int, int]:
    name = str(estimate.get("color", "")).upper()
    if name in LINE_COLORS:
        return LINE_COLORS[name]
    raw = str(estimate.get("hexcolor", "")).lstrip("#")
    if len(raw) == 6:
        try:
            return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
        except ValueError:
            pass
    return FALLBACK_LINE


def _minutes(value: object) -> int | None:
    """Parse a countdown. BART sends the string 'Leaving' for a boarding train."""
    text = str(value).strip()
    if text.lower() == "leaving":
        return 0
    try:
        return max(0, int(text))
    except ValueError:
        return None


def _as_list(value: object) -> list:
    """BART collapses single-element arrays into a bare object."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def lookup(station: str, opener: Opener | None = None) -> Board | None:
    """Fetch the next trains at *station*, soonest first, or None on failure."""
    station = (station or DEFAULT_STATION).upper()
    if not is_station(station):
        return None
    query = urllib.parse.urlencode(
        {"cmd": "etd", "orig": station, "key": PUBLIC_KEY, "json": "y"}
    )
    fetch = opener or _default_opener
    try:
        payload = json.loads(fetch(f"{ETD_URL}?{query}").decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        LOGGER.debug("BART lookup failed for %s: %s", station, error)
        return None

    try:
        root = payload["root"]
    except (KeyError, TypeError):
        return None

    stations = _as_list(root.get("station"))
    if not stations:
        # Outside service hours the endpoint answers with a warning and no
        # station block. An empty board is the honest render for that.
        return Board(station, panel_name(station), (), _root_message(root))

    entry = stations[0]
    departures: list[Departure] = []
    for group in _as_list(entry.get("etd")):
        destination = str(group.get("abbreviation", "")).upper()
        for estimate in _as_list(group.get("estimate")):
            if str(estimate.get("cancelflag", "0")) == "1":
                # A cancelled train is not a departure; listing it would push a
                # real one off a four-line board.
                continue
            minutes = _minutes(estimate.get("minutes"))
            if minutes is None:
                continue
            try:
                cars = int(str(estimate.get("length", "0")))
            except ValueError:
                cars = 0
            try:
                delay = int(str(estimate.get("delay", "0")))
            except ValueError:
                delay = 0
            departures.append(
                Departure(
                    destination=destination,
                    label=panel_name(destination) if destination else "TRAIN",
                    minutes=minutes,
                    color=_line_color(estimate),
                    platform=str(estimate.get("platform", "")).strip(),
                    direction=str(estimate.get("direction", "")).strip().upper()[:1],
                    cars=cars,
                    delay_seconds=delay,
                )
            )

    departures.sort(key=lambda item: (item.minutes, item.label))
    name = str(entry.get("name", "")).strip() or panel_name(station)
    return Board(station, panel_name(station) or name, tuple(departures), _root_message(root))


def _root_message(root: dict) -> str:
    message = root.get("message")
    if isinstance(message, dict):
        for key in ("warning", "error"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split()).upper()
    elif isinstance(message, str) and message.strip():
        return " ".join(message.split()).upper()
    return ""


__all__ = [
    "Board",
    "DEFAULT_STATION",
    "Departure",
    "LINE_COLORS",
    "PUBLIC_KEY",
    "STATIONS",
    "STATION_NAMES",
    "is_station",
    "lookup",
    "panel_name",
]
