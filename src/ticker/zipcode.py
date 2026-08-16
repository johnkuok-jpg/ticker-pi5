# MIT License — Copyright (c) 2026 John Kuok
"""US ZIP code to latitude/longitude lookup for the weather and air modes.

Both weather modes talk to US-government APIs that want coordinates:
api.weather.gov wants a ``lat,lon`` point and open-meteo's air-quality
endpoint wants ``latitude``/``longitude`` query params. Neither accepts a
ZIP. That left the ``.env`` file as the only way to aim the panel, which
means anyone who moves — or who just wants tomorrow's forecast for the
city they're flying to — has to SSH in and edit a dotfile.

This module closes that gap by resolving a 5-digit ZIP to a coordinate
pair through Zippopotam.us, a free no-key geocoder that returns the
place name and state alongside the coordinates. Those extra fields are
worth having: echoing "94103 — San Francisco, CA" back into the web UI
turns a silent success into a visible one, so a typo'd ZIP is obvious
before the user walks away from the panel.

Two design notes:

* Results are cached in-process and never expire. A ZIP's centroid is a
  postal-geography fact, not live data — it does not move between panel
  restarts, so a second lookup of the same ZIP should cost nothing.

* Zippopotam.us returns one entry per place sharing the ZIP (a handful of
  ZIPs straddle two towns). We take the first, which is the primary place
  name, and note the count so callers can decide whether to disambiguate.
  For a 128x32 LED panel, the primary name is the right answer.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Zippopotam.us: free, keyless, and stable enough that the Pi can call it
# without a fallback. Failures degrade to "ZIP not found" in the web UI
# rather than breaking the weather mode, which keeps its old coordinates.
_ENDPOINT = "https://api.zippopotam.us/us/{zip}"
_TIMEOUT = 8
_USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

# US ZIPs are exactly five digits. ZIP+4 is accepted from the user and
# truncated, because the extra four digits identify a delivery route --
# far finer than any weather grid cares about.
_ZIP_RE = re.compile(r"^\d{5}$")

_cache: dict[str, "ZipLocation | None"] = {}


@dataclass(frozen=True, slots=True)
class ZipLocation:
    """One resolved ZIP: where it is and what it is called."""

    zip_code: str
    lat: float
    lon: float
    city: str
    state: str
    place_count: int = 1

    @property
    def label(self) -> str:
        """Human-readable ``San Francisco, CA`` for the web UI."""
        if self.city and self.state:
            return f"{self.city}, {self.state}"
        return self.city or self.state or self.zip_code


def normalize(value: str) -> str:
    """Reduce user input to a bare 5-digit ZIP, or "" if it is not one.

    Accepts the shapes people actually type: ``94103``, ``94103-1234``,
    ``  94103 ``, and ``94103 1234``. Anything else -- a city name, a
    Canadian postal code, a 4-digit typo -- returns "" so callers can
    reject it without a network round trip.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 5:
        digits = digits[:5]
    return digits if _ZIP_RE.match(digits) else ""


def lookup(zip_code: str) -> ZipLocation | None:
    """Resolve a US ZIP to coordinates, or None when it cannot be resolved.

    Returns None for both "not a valid ZIP shape" and "the geocoder does
    not know this ZIP", because the caller's response is the same either
    way: tell the user the ZIP did not resolve and leave the existing
    coordinates alone.
    """
    normalized = normalize(zip_code)
    if not normalized:
        return None
    if normalized in _cache:
        return _cache[normalized]

    result: ZipLocation | None = None
    try:
        request = urllib.request.Request(
            _ENDPOINT.format(zip=normalized),
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        places = payload.get("places") or []
        if places:
            primary = places[0]
            result = ZipLocation(
                zip_code=normalized,
                lat=float(primary["latitude"]),
                lon=float(primary["longitude"]),
                city=str(primary.get("place name", "")).strip(),
                state=str(primary.get("state abbreviation", "")).strip(),
                place_count=len(places),
            )
    except urllib.error.HTTPError as error:
        # 404 is the geocoder's "no such ZIP" and is an expected outcome,
        # so cache it as a miss instead of retrying on every keystroke.
        if error.code == 404:
            _cache[normalized] = None
            return None
        log.warning("zip lookup failed for %s: HTTP %s", normalized, error.code)
        return None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
        # A network blip or malformed payload is transient: do NOT cache it,
        # so the next attempt gets a real try rather than a sticky failure.
        log.warning("zip lookup failed for %s: %s", normalized, error)
        return None

    _cache[normalized] = result
    return result


def clear_cache() -> None:
    """Drop the in-process cache. Exists for tests."""
    _cache.clear()
