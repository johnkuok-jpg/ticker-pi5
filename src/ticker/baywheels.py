# MIT License — Copyright (c) 2026 John Kuok
"""Bay Wheels (Lyft Bay Area bike share) GBFS client.

GBFS is a public, keyless feed with a two-file split that this module leans on:

* ``station_information.json`` describes the physical dock: name, coordinates,
  and capacity. It effectively never changes and is cached for an hour.
* ``station_status.json`` reports live counts: bikes available, ebikes among
  them, docks free, whether the station is renting. It is refreshed every
  minute or so.

Fetching both on every render pass would spend network on data that mostly
does not change, so the fetches are memoised in module-level dictionaries and
returned as immutable :class:`Station` objects. The renderer asks by station
id; the module handles freshness.

Bay Wheels' GBFS host rejects a plain SDK-style scraper user agent, but
accepts an ordinary ``requests`` User-Agent, so the client sets one.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests

# The GBFS discovery document lists every feed URL for the operator; using it
# rather than a hard-coded feed path shields the code from a future rename
# (e.g. gbfs/2.3/ → gbfs/3.0/), which GBFS operators do occasionally.
DISCOVERY_URL = "https://gbfs.baywheels.com/gbfs/2.3/gbfs.json"

# One shared session so keepalive works across the info/status pair. The
# renderer builds a mode once and reuses it, so this session lives for the
# life of the process.
_SESSION = requests.Session()
_SESSION.headers.update(
    {
        # Mimic a small self-hosted client rather than an SDK/bot UA; Bay Wheels
        # returns 403 for anything that looks like a scraper.
        "User-Agent": "ticker-pi5/1.0 (+github.com/johnkuok-jpg/ticker-pi5)",
        "Accept": "application/json",
    }
)

INFO_TTL_SECONDS = 60 * 60  # station_information: name/capacity, ~stable
STATUS_TTL_SECONDS = 45     # station_status: live counts, refreshed on the minute
DISCOVERY_TTL_SECONDS = 24 * 60 * 60  # feed URLs almost never change

_HTTP_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class Station:
    """Everything the ticker needs for one Bay Wheels station."""

    station_id: str
    name: str
    lat: float
    lon: float
    capacity: int
    # Live counts. classic_bikes = num_bikes_available - num_ebikes_available.
    num_bikes_available: int
    num_ebikes_available: int
    num_docks_available: int
    is_renting: bool
    is_installed: bool
    last_reported: int  # unix seconds, from status feed

    @property
    def classic_bikes(self) -> int:
        """Non-electric bikes available. GBFS reports total minus ebikes."""
        return max(0, self.num_bikes_available - self.num_ebikes_available)

    @property
    def ebikes(self) -> int:
        return max(0, self.num_ebikes_available)

    @property
    def docks(self) -> int:
        return max(0, self.num_docks_available)


# ---------------------------------------------------------------------------
# Internal caches. Keyed on the discovery response, then filled as needed.
# ---------------------------------------------------------------------------

_discovery_urls: dict[str, str] | None = None
_discovery_fetched: float = 0.0

_info_by_id: dict[str, dict[str, Any]] = {}
_info_fetched: float = 0.0

_status_by_id: dict[str, dict[str, Any]] = {}
_status_fetched: float = 0.0


def _get_json(url: str) -> Any:
    response = _SESSION.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _feed_urls() -> dict[str, str]:
    """Resolve GBFS feed names -> URLs. Cached per :data:`DISCOVERY_TTL_SECONDS`."""
    global _discovery_urls, _discovery_fetched
    if _discovery_urls is not None and time.monotonic() - _discovery_fetched < DISCOVERY_TTL_SECONDS:
        return _discovery_urls
    payload = _get_json(DISCOVERY_URL)
    # GBFS 2.x nests feeds under a language key; try 'en' first, then any.
    feeds_root = payload.get("data") or {}
    feeds_by_lang = feeds_root
    if "feeds" not in feeds_by_lang:
        # 2.x form: {"data": {"en": {"feeds": [...]}, ...}}
        lang = "en" if "en" in feeds_root else next(iter(feeds_root))
        feeds_by_lang = feeds_root[lang]
    urls = {entry["name"]: entry["url"] for entry in feeds_by_lang.get("feeds", [])}
    _discovery_urls = urls
    _discovery_fetched = time.monotonic()
    return urls


def _refresh_info(force: bool = False) -> None:
    global _info_by_id, _info_fetched
    if not force and _info_by_id and time.monotonic() - _info_fetched < INFO_TTL_SECONDS:
        return
    url = _feed_urls().get("station_information")
    if not url:
        return
    payload = _get_json(url)
    stations = payload.get("data", {}).get("stations", [])
    _info_by_id = {str(entry["station_id"]): entry for entry in stations if entry.get("station_id") is not None}
    _info_fetched = time.monotonic()


def _refresh_status(force: bool = False) -> None:
    global _status_by_id, _status_fetched
    if not force and _status_by_id and time.monotonic() - _status_fetched < STATUS_TTL_SECONDS:
        return
    url = _feed_urls().get("station_status")
    if not url:
        return
    payload = _get_json(url)
    stations = payload.get("data", {}).get("stations", [])
    _status_by_id = {str(entry["station_id"]): entry for entry in stations if entry.get("station_id") is not None}
    _status_fetched = time.monotonic()


def _combine(station_id: str) -> Station | None:
    info = _info_by_id.get(station_id)
    status = _status_by_id.get(station_id)
    if not info or not status:
        return None
    try:
        return Station(
            station_id=str(info["station_id"]),
            name=str(info.get("name", "")),
            lat=float(info.get("lat", 0.0)),
            lon=float(info.get("lon", 0.0)),
            capacity=int(info.get("capacity", 0)),
            num_bikes_available=int(status.get("num_bikes_available", 0)),
            num_ebikes_available=int(status.get("num_ebikes_available", 0)),
            num_docks_available=int(status.get("num_docks_available", 0)),
            is_renting=bool(status.get("is_renting", 0)),
            is_installed=bool(status.get("is_installed", 0)),
            last_reported=int(status.get("last_reported", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_station(station_id: str) -> Station | None:
    """Live snapshot of one station, or None if it is not in the feed."""
    if not station_id:
        return None
    try:
        _refresh_info()
        _refresh_status()
    except (requests.RequestException, ValueError):
        # Serve stale data if we have any; renderer decides what to show when
        # both caches are empty.
        pass
    return _combine(str(station_id))


def list_stations() -> list[Station]:
    """All installed stations. Useful for the web picker.

    The web picker cares about names and coordinates but not live counts, so
    zeros for the status fields are fine if the status feed happens to fail;
    the info feed alone is enough to populate a searchable list.
    """
    try:
        _refresh_info()
    except (requests.RequestException, ValueError):
        return []
    try:
        _refresh_status()
    except (requests.RequestException, ValueError):
        pass
    out: list[Station] = []
    for station_id in _info_by_id:
        station = _combine(station_id)
        if station is None:
            info = _info_by_id[station_id]
            try:
                station = Station(
                    station_id=str(info["station_id"]),
                    name=str(info.get("name", "")),
                    lat=float(info.get("lat", 0.0)),
                    lon=float(info.get("lon", 0.0)),
                    capacity=int(info.get("capacity", 0)),
                    num_bikes_available=0,
                    num_ebikes_available=0,
                    num_docks_available=0,
                    is_renting=False,
                    is_installed=True,
                    last_reported=0,
                )
            except (KeyError, TypeError, ValueError):
                continue
        out.append(station)
    return out


def nearest_station(lat: float, lon: float, stations: Iterable[Station] | None = None) -> Station | None:
    """Great-circle nearest station to *(lat, lon)*.

    Uses the haversine formula rather than a flat-plane approximation so the
    "Use nearest" button in the web app can be trusted at the edges of the
    service area (Berkeley to San Jose is far enough that Euclidean distance
    on raw degrees is meaningfully wrong).
    """
    candidates = list(stations) if stations is not None else list_stations()
    if not candidates:
        return None
    return min(candidates, key=lambda station: _haversine(lat, lon, station.lat, station.lon))


def search_stations(query: str, limit: int = 20) -> list[Station]:
    """Case-insensitive substring match against station name.

    Simple substring is enough for a ~450-station list where users pick by
    landmark ("Ferry", "Powell", "Embarcadero"). Fuzzy scoring buys nothing
    at this size and would make the results feel less predictable.
    """
    normalised = query.strip().lower()
    if not normalised:
        return []
    stations = list_stations()
    hits = [station for station in stations if normalised in station.name.lower()]
    return hits[:limit]


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    radius_km = 6371.0088
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _reset_cache_for_tests() -> None:
    """Clear all module state. Tests only."""
    global _discovery_urls, _discovery_fetched, _info_by_id, _info_fetched
    global _status_by_id, _status_fetched
    _discovery_urls = None
    _discovery_fetched = 0.0
    _info_by_id = {}
    _info_fetched = 0.0
    _status_by_id = {}
    _status_fetched = 0.0
