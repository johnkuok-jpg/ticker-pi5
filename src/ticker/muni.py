# MIT License — Copyright (c) 2026 John Kuok
"""SF Muni real-time bus arrivals via umoiq's public NextBus-style endpoint.

Muni predictions come from ``webservices.umoiq.com`` (Cubic/UMO's NextBus
descendant, the same feed the muni.sfmta.com mobile page uses). The key
below is shared publicly — it's the one Cubic hands out for classroom and
hobbyist use and it's baked into muni.sfmta.com's own client — so unlike a
personal 511.org token this module is on solid ground for a repo we ship
to a handful of friends.

The endpoint's contract:

* ``/agencies/sfmta-cis/stopcodes/<stopcode>/predictions`` returns a list
  where each entry pairs one route with its predictions at that stop.
  Predictions arrive as ``values`` sorted soonest-first, each carrying its
  own destination and minute countdown. There is no "next bus at this
  stop" endpoint; a stop that serves N routes returns N entries and the
  caller merges them.

* ``/agencies/sfmta-cis/routes/<route>/stops`` returns every stop for one
  route. Muni has ~68 routes; iterating them is how you build a
  stop-name/coordinate directory when you want street-name search or a
  "nearest stop" button. Cached hard because the roster changes on the
  order of once per shakeup (semi-annual).

Users type the 5-digit stopcode printed on every Muni shelter sign, so
the fast path never needs the directory at all — the directory is only
built when someone opens the picker's search box.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

# Cubic/UMO's shared demo key. Published on muni.sfmta.com itself and reused
# by every classroom Muni tutorial online (see e.g. the LWHS Raspberry Pi
# curriculum). If umoiq ever revokes it we swap to a 511.org key.
PUBLIC_KEY = "0be8ebd0284ce712a63f29dcaf7798c4"
AGENCY_ID = "sfmta-cis"
BASE_URL = "https://webservices.umoiq.com/api/pub/v1"
REQUEST_TIMEOUT = 8.0
USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

# The predictions endpoint is refreshed roughly once a minute on the server
# side, so hammering it faster only spends network on identical data.
PREDICTIONS_TTL_SECONDS = 30

# The routes list is the seed for lazy stop-directory builds; it changes on
# the order of once per shakeup, so an hour is plenty.
ROUTES_TTL_SECONDS = 60 * 60
STOPS_TTL_SECONDS = 6 * 60 * 60  # per-route stop list; even more static

# Muni panel names on real signage are short (7 CALIFORNIA, 38 GEARY),
# never the full "N Judah" essay the API returns as ``route.title``. This
# is the width at which a destination still fits alongside a "12M"
# countdown on the panel; the renderer decides where to truncate but the
# label the API hands us is chopped to something signage-shaped first.
MAX_ROUTE_LABEL = 4    # "T-3RD" fits comfortably; "N/OWL" for owl routes
MAX_DEST_LABEL = 22    # Space for a "Powell/Market"-length destination


# ---------------------------------------------------------------------------
# Dataclasses returned to the renderer/webapp
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Arrival:
    """One upcoming bus/train at a stop.

    ``minutes`` is umoiq's own countdown, floored at 0. ``route`` is the
    short route id ("38", "N", "T"); ``destination`` is the direction's
    display name already trimmed for the panel.
    """

    route: str
    destination: str
    minutes: int
    color: tuple[int, int, int]
    vehicle_id: str = ""

    @property
    def is_leaving(self) -> bool:
        return self.minutes <= 0

    def countdown(self) -> str:
        return "NOW" if self.is_leaving else f"{self.minutes}M"


@dataclass(frozen=True)
class Predictions:
    """Everything the Muni mode needs to render one stop's departures."""

    stop_code: str
    stop_name: str
    arrivals: tuple[Arrival, ...]
    message: str = ""


@dataclass(frozen=True)
class Stop:
    """One physical Muni stop, for the web app's picker."""

    code: str
    name: str
    lat: float
    lon: float


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _default_opener(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


# Function pointer so tests can inject a fake without monkeypatching urllib.
_opener = _default_opener


def set_opener(opener) -> None:  # type: ignore[no-untyped-def]
    """Install an opener callable ``str -> bytes``. Tests only."""
    global _opener
    _opener = opener


def _reset_for_tests() -> None:
    """Reset module state (caches + opener) between tests."""
    global _opener, _predictions_cache, _routes_cache, _routes_fetched
    global _stops_by_code, _stops_route_seen
    _opener = _default_opener
    _predictions_cache = {}
    _routes_cache = ()
    _routes_fetched = 0.0
    _stops_by_code = {}
    _stops_route_seen = {}


# ---------------------------------------------------------------------------
# Colour handling: Muni route colours from the feed, retuned for the panel
# ---------------------------------------------------------------------------

# The API sends a per-route hex colour, but many of Muni's official colours
# (34% luminance red for the 5, the T's dark teal) sit below the LED panel's
# legibility floor. So the feed colour is treated as a hint and, when it's
# darker than the floor, brightened along its own hue toward the panel's
# comfortable brightness. This preserves each route's identity while keeping
# every label readable at the dim-panel step used at night.
_LUMA_FLOOR = 90  # roughly matches BART's tuned line colours
_FALLBACK = (235, 240, 250)  # panel white, used when the feed omits a colour


def _luma(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _brighten_to_floor(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Scale ``rgb`` up toward its hue until its luma clears the floor.

    A uniform channel scale preserves hue exactly. Capped at 255 per channel.
    Pure black is treated as "no colour" and returns the panel-white fallback,
    since scaling a zero doesn't move it.
    """
    lum = _luma(rgb)
    if lum <= 0:
        return _FALLBACK
    if lum >= _LUMA_FLOOR:
        return rgb
    scale = _LUMA_FLOOR / lum
    return (
        min(255, round(rgb[0] * scale)),
        min(255, round(rgb[1] * scale)),
        min(255, round(rgb[2] * scale)),
    )


def _parse_hex(value: object) -> tuple[int, int, int] | None:
    raw = str(value or "").lstrip("#").strip()
    if len(raw) != 6:
        return None
    try:
        return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))
    except ValueError:
        return None


def _route_color(route_entry: dict) -> tuple[int, int, int]:
    parsed = _parse_hex(route_entry.get("color"))
    if parsed is None:
        return _FALLBACK
    return _brighten_to_floor(parsed)


# ---------------------------------------------------------------------------
# Route label shaping
# ---------------------------------------------------------------------------


def route_label(route_id: str) -> str:
    """Panel-style short label for a Muni route.

    Muni's own signage uses just the route id: "38", "N", "T", "38R" for
    rapid, "N OWL" for late-night owl service. The API's ``id`` field is
    already the short id ("38", "N"); we uppercase it and cap length so
    an exotic route like "K OWL" still fits the badge column.
    """
    value = str(route_id or "").strip().upper()
    if not value:
        return ""
    if len(value) <= MAX_ROUTE_LABEL:
        return value
    return value[:MAX_ROUTE_LABEL]


def _destination_label(direction: dict, route_entry: dict) -> str:
    """Best signage-style destination for one prediction.

    Prefers ``direction.destinationName`` (what a Muni sign shows), falls
    back to ``direction.name``, then the route's own title. Truncated to
    something the panel column can hold.
    """
    for key in ("destinationName", "name"):
        value = str(direction.get(key, "")).strip()
        if value:
            return _shorten(value)
    return _shorten(str(route_entry.get("title", "")).strip())


def _shorten(name: str) -> str:
    """Squeeze a NextBus destination string into signage width.

    Removes redundant "+" glue that the feed uses for cross-streets, drops
    trailing route qualifiers in parentheses, and hard-truncates at the
    column width the panel actually has.
    """
    text = " ".join(name.split())
    if "(" in text:
        text = text.split("(", 1)[0].rstrip()
    # NextBus writes cross-streets as "24th St + Castro St"; the plus is
    # visual noise on a 128-pixel row where every character counts.
    text = text.replace(" + ", " & ")
    if len(text) <= MAX_DEST_LABEL:
        return text
    return text[: MAX_DEST_LABEL - 1].rstrip() + "\u2026"


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------


_predictions_cache: dict[str, tuple[float, Predictions]] = {}


def _minutes_from_value(value: object) -> int | None:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, raw)


def _predictions_url(stop_code: str) -> str:
    quoted = urllib.parse.quote(str(stop_code), safe="")
    return f"{BASE_URL}/agencies/{AGENCY_ID}/stopcodes/{quoted}/predictions?key={PUBLIC_KEY}"


def is_stop_code(value: str) -> bool:
    """Muni stopcodes are 5-digit numbers printed on every shelter sign."""
    text = str(value or "").strip()
    return text.isdigit() and 4 <= len(text) <= 6


def lookup(stop_code: str) -> Predictions | None:
    """Fetch the next arrivals at *stop_code*, soonest first, or None on failure.

    Cached for :data:`PREDICTIONS_TTL_SECONDS` per stopcode: the mode
    re-renders many times a second, but the upstream feed only refreshes
    once a minute or so and the API rate-limits aggressively otherwise.
    """
    code = str(stop_code or "").strip()
    if not is_stop_code(code):
        return None
    now = time.monotonic()
    cached = _predictions_cache.get(code)
    if cached and now - cached[0] < PREDICTIONS_TTL_SECONDS:
        return cached[1]
    try:
        payload = json.loads(_opener(_predictions_url(code)).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        LOGGER.debug("Muni predictions failed for %s: %s", code, error)
        return cached[1] if cached else None
    parsed = _parse_predictions(code, payload)
    if parsed is None:
        return cached[1] if cached else None
    _predictions_cache[code] = (now, parsed)
    return parsed


def _parse_predictions(stop_code: str, payload: object) -> Predictions | None:
    if not isinstance(payload, list):
        return None
    if not payload:
        return Predictions(stop_code=stop_code, stop_name="", arrivals=(), message="")
    arrivals: list[Arrival] = []
    stop_name = ""
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        stop = entry.get("stop") or {}
        if not stop_name and isinstance(stop, dict):
            stop_name = str(stop.get("name", "")).strip()
        route_entry = entry.get("route") or {}
        if not isinstance(route_entry, dict):
            continue
        route = route_label(str(route_entry.get("id", "")))
        color = _route_color(route_entry)
        for value in entry.get("values") or ():
            if not isinstance(value, dict):
                continue
            minutes = _minutes_from_value(value.get("minutes"))
            if minutes is None:
                continue
            direction = value.get("direction") or {}
            if not isinstance(direction, dict):
                direction = {}
            destination = _destination_label(direction, route_entry)
            arrivals.append(
                Arrival(
                    route=route,
                    destination=destination,
                    minutes=minutes,
                    color=color,
                    vehicle_id=str(value.get("vehicleId", "")).strip(),
                )
            )
    # Signage sorts soonest first, ties broken by route so two 5M arrivals
    # keep a stable order between refreshes.
    arrivals.sort(key=lambda item: (item.minutes, item.route))
    return Predictions(
        stop_code=stop_code,
        stop_name=stop_name,
        arrivals=tuple(arrivals),
        message="",
    )


# ---------------------------------------------------------------------------
# Stop directory: built lazily by iterating routes so we never make 68
# requests on boot when nobody has asked to search yet.
# ---------------------------------------------------------------------------


_routes_cache: tuple[str, ...] = ()
_routes_fetched: float = 0.0

# stop_code -> Stop. Filled as we visit routes; a stop can appear on many
# routes but its coordinates are identical, so the last write wins harmlessly.
_stops_by_code: dict[str, Stop] = {}

# Route id -> monotonic timestamp of last successful fetch, so we don't
# re-fetch every route every time somebody types another character in the
# search box.
_stops_route_seen: dict[str, float] = {}


def _fetch_routes() -> tuple[str, ...]:
    """Route ids ("38", "N", "T", ...). Cached hard; roster is semi-annual."""
    global _routes_cache, _routes_fetched
    now = time.monotonic()
    if _routes_cache and now - _routes_fetched < ROUTES_TTL_SECONDS:
        return _routes_cache
    url = f"{BASE_URL}/agencies/{AGENCY_ID}/routes?key={PUBLIC_KEY}"
    try:
        payload = json.loads(_opener(url).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        LOGGER.debug("Muni routes list failed: %s", error)
        return _routes_cache  # stale is better than empty
    if not isinstance(payload, list):
        return _routes_cache
    ids = tuple(
        str(item.get("id", "")).strip()
        for item in payload
        if isinstance(item, dict) and item.get("id")
    )
    _routes_cache = ids
    _routes_fetched = now
    return ids


def _fetch_route_stops(route_id: str) -> None:
    """Merge stops for one route into the shared directory. Idempotent, cached."""
    now = time.monotonic()
    last = _stops_route_seen.get(route_id, 0.0)
    if last and now - last < STOPS_TTL_SECONDS:
        return
    quoted = urllib.parse.quote(str(route_id), safe="")
    url = f"{BASE_URL}/agencies/{AGENCY_ID}/routes/{quoted}/stops?key={PUBLIC_KEY}"
    try:
        payload = json.loads(_opener(url).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
        LOGGER.debug("Muni stops for %s failed: %s", route_id, error)
        return
    if not isinstance(payload, list):
        return
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", "")).strip()
        if not code:
            continue
        try:
            lat = float(entry.get("lat", 0.0))
            lon = float(entry.get("lon", 0.0))
        except (TypeError, ValueError):
            continue
        name = str(entry.get("name", "")).strip()
        _stops_by_code[code] = Stop(code=code, name=name, lat=lat, lon=lon)
    _stops_route_seen[route_id] = now


def _ensure_directory() -> None:
    """Ensure the full stop directory is loaded. Slow first call (~68 fetches)."""
    for route_id in _fetch_routes():
        _fetch_route_stops(route_id)


def search_stops(query: str, limit: int = 20) -> list[Stop]:
    """Case-insensitive substring match against stop name.

    First call is slow (walks every Muni route to build the directory);
    subsequent calls hit the cache. Fine for a webapp picker where the
    first keystroke waits a second or two and everything after is instant.
    Falls back to whatever is already cached if the network is flaky.
    """
    text = query.strip().lower()
    if not text:
        return []
    _ensure_directory()
    hits: list[Stop] = []
    for stop in _stops_by_code.values():
        if text in stop.name.lower():
            hits.append(stop)
            if len(hits) >= limit:
                break
    hits.sort(key=lambda stop: stop.name.lower())
    return hits[:limit]


def nearest_stop(lat: float, lon: float) -> Stop | None:
    """Great-circle nearest Muni stop to *(lat, lon)*.

    Uses the same haversine as the Bay Wheels picker so "Nearest" behaves
    consistently across pickers. Returns None if the directory failed to
    load and there is nothing cached.
    """
    _ensure_directory()
    if not _stops_by_code:
        return None
    return min(
        _stops_by_code.values(),
        key=lambda stop: _haversine(lat, lon, stop.lat, stop.lon),
    )


def get_stop(stop_code: str) -> Stop | None:
    """Look up a stop by code, populating the directory if it isn't cached yet."""
    code = str(stop_code or "").strip()
    if not code:
        return None
    if code in _stops_by_code:
        return _stops_by_code[code]
    _ensure_directory()
    return _stops_by_code.get(code)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    radius_km = 6371.0088
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


__all__ = [
    "AGENCY_ID",
    "Arrival",
    "PUBLIC_KEY",
    "Predictions",
    "Stop",
    "get_stop",
    "is_stop_code",
    "lookup",
    "nearest_stop",
    "route_label",
    "search_stops",
    "set_opener",
]
