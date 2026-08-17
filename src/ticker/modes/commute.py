# MIT License — Copyright (c) 2026 John Kuok
"""Google-Maps-backed door-to-door commute ETA.

**Why this mode exists.** BART / Muni / Bikes each answer "when is the
next departure at this stop?" -- great for a specific leg, terrible at
the actual morning question, which is "how long from my apartment to
my desk right now?". This mode collapses the whole trip into one
number by asking Google Maps directly, so a rainy morning where BART
is running late reads as a bigger door-to-door number instead of
requiring the driver to eyeball three different cards.

**Why the mode does not auto-refresh.** For a ~0.6-mile home-to-work
that could be walked, an auto-refresh every N seconds burns API
quota to move the number by fractions of a minute; every tap in the
webapp fetches fresh instead. The panel shows the last fetched result
and a "fetched HH:MM" line so freshness is obvious rather than
implied. If you never tap, the card idles on its placeholder rather
than pretending to know.

**Why Google, not the free upstreams.** SFMTA (511.org) is transit-only
and the coverage of walking segments would have to be estimated on
device. Apple's transit routing needs a $99/yr developer account for
API access. Google's Directions API charges $5/1000 requests but bakes
in a 200-req/day free floor as of 2025 -- for the intended usage
(a handful of taps per commute) the effective cost is $0 and the
routing is the most accurate of the three.

Layout is the same left-icon-column / right-content grammar the rest of
the panel uses::

    ┌──────────────────────────────────────────────┐
    │ [icon]     HOME → WORK             14M       │  row 0-8   SMALL + MEDIUM minutes
    │            via 38-GEARY                      │  row 12-19 SMALL route hint
    │            fetched 8:47                      │  row 22-29 SMALL freshness
    └──────────────────────────────────────────────┘

The icon on the left changes with the travel mode (walk/drive/transit/
bike) so a glance tells you which routing brain answered. Minutes are
green (<20), amber (20-45), or red (>45) so the severity reads at
peripheral vision even before you focus on the digits.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

LOGGER = logging.getLogger(__name__)

DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
REQUEST_TIMEOUT = 10.0

# Google Directions accepts these four ``mode`` values. The webapp is
# constrained to the same set so a malformed persisted value can't sneak
# through and produce a silent 400. ``TRAVEL_MODES[0]`` is the fallback.
TRAVEL_MODES: tuple[str, ...] = ("transit", "driving", "walking", "bicycling")

# Colors -- reused from the panel palette so the mode reads as part of
# the family rather than a bolted-on one-off.
WHITE = (235, 240, 250)
DIM = (108, 118, 138)
GREEN = (40, 230, 90)
AMBER = (255, 176, 0)
RED = (240, 60, 60)
BLUE = (90, 170, 255)
BROWN = (200, 140, 60)   # Muni-style route hint

# Minute thresholds for the color of the big number. Tuned to the SoMa
# home-to-work distance (~14 min walk / ~7 min drive) so today's baseline
# is comfortably green; a green->amber jump signals real friction rather
# than routine variance.
_GREEN_MAX = 20
_AMBER_MAX = 45


@dataclass(frozen=True)
class CommuteResult:
    """One route lookup snapshot.

    ``minutes`` is the whole-number Google-reported duration. ``hint`` is
    a short line ("via BART", "0.6 mi", "TRAFFIC OK") that gives the
    number context; empty string means "nothing to add". ``mode`` is
    the travel mode used for the request and is echoed here so a stale
    result labelled with the mode it was actually fetched under -- if
    the user flips the picker between fetches, the card doesn't
    silently mislabel the previous number.
    """

    mode: str
    minutes: int
    hint: str
    fetched_epoch: float


def _minutes_color(minutes: int) -> tuple[int, int, int]:
    """Green under 20, amber under 45, red beyond."""
    if minutes < _GREEN_MAX:
        return GREEN
    if minutes < _AMBER_MAX:
        return AMBER
    return RED


def _extract_result(payload: dict, mode: str, now: float) -> CommuteResult | None:
    """Pull minutes + a one-line hint out of a Google Directions response.

    Returns ``None`` when the response has no usable routes so the
    caller can surface a specific error state instead of a garbage
    number. All keys are looked up defensively -- Directions is stable
    but the ``duration_in_traffic`` field only exists for the driving
    mode with ``departure_time=now``, and ``transit_details`` only for
    transit steps that ride a line.
    """
    if payload.get("status") != "OK":
        return None
    routes = payload.get("routes") or []
    if not routes:
        return None
    leg = (routes[0].get("legs") or [{}])[0]
    # Prefer live traffic duration when Google returned one -- that's the
    # whole point of asking with ``departure_time=now`` on driving mode.
    duration = leg.get("duration_in_traffic") or leg.get("duration") or {}
    seconds = duration.get("value")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    minutes = max(1, int(round(seconds / 60)))

    # Route hint: for transit, name the first non-walking line the trip
    # rides. For everything else, distance is more useful than duration
    # (which we already surface as minutes).
    hint = ""
    if mode == "transit":
        for step in leg.get("steps") or []:
            transit = step.get("transit_details")
            if not transit:
                continue
            line = transit.get("line") or {}
            name = (line.get("short_name") or line.get("name") or "").strip().upper()
            if name:
                hint = f"VIA {name}"
                break
    else:
        distance = (leg.get("distance") or {}).get("text", "").strip()
        if distance:
            hint = distance.upper()
    return CommuteResult(mode=mode, minutes=minutes, hint=hint, fetched_epoch=now)


class CommuteMode(Mode):
    """Door-to-door ETA fetched from Google Maps Directions.

    All refreshes are user-triggered: :meth:`fetch` is called from the
    webapp's ``/commute/route`` handler and the render loop shows the
    last cached result plus its age. The mode never spins up its own
    fetch on render (this deliberately gates API spend on user intent).
    """

    # Results older than this are shown greyed in the freshness line to
    # nudge a re-tap, but still displayed -- a 20-minute-old walk ETA is
    # still useful.
    STALE_SECONDS = 15 * 60

    def __init__(self, config, opener=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self._opener = opener or urllib.request.urlopen

    # -- data ---------------------------------------------------------------

    def fetch(self, mode: str | None = None) -> CommuteResult | None:
        """Ask Google Directions and cache the result.

        Called from the webapp on the Route-now tap. Returns the fresh
        result on success, ``None`` on any failure (with ``_error_state``
        populated so the placeholder can explain what went wrong).
        Never raises -- the webapp handler just re-reads
        :meth:`state` and returns that to the browser.
        """
        origin = self.config.current_commute_origin()
        destination = self.config.current_commute_destination()
        key = self.config.google_maps_api_key.strip()
        travel_mode = (mode or self.config.current_commute_mode()).strip().lower()
        if travel_mode not in TRAVEL_MODES:
            travel_mode = TRAVEL_MODES[0]
        if not key:
            self._error_state = "no_key"
            LOGGER.warning("commute: GOOGLE_MAPS_API_KEY is unset")
            return None
        if not origin or not destination:
            self._error_state = "no_route"
            LOGGER.warning("commute: origin or destination unset")
            return None

        params = {
            "origin": origin,
            "destination": destination,
            "mode": travel_mode,
            "key": key,
            # ``now`` unlocks ``duration_in_traffic`` on driving requests
            # and pins transit routing to the current schedule; harmless
            # for the other modes.
            "departure_time": "now",
            # Google returns durations in seconds regardless of ``units``
            # but the distance ``text`` on the walking/biking hint uses
            # this. Imperial keeps the panel's mile labels honest.
            "units": "imperial",
        }
        url = f"{DIRECTIONS_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
        )
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            LOGGER.warning("commute: directions fetch failed: %s", error)
            self._error_state = "network"
            return None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            LOGGER.warning("commute: directions payload not JSON: %s", error)
            self._error_state = "api"
            return None
        status = payload.get("status")
        if status != "OK":
            LOGGER.warning(
                "commute: directions returned %s (%s)",
                status,
                payload.get("error_message", ""),
            )
            self._error_state = "no_route" if status == "ZERO_RESULTS" else "api"
            return None
        result = _extract_result(payload, travel_mode, time.time())
        if result is None:
            self._error_state = "no_route"
            return None
        self._write_result(result)
        self._error_state = "idle"
        return result

    # Result + error state are file-backed rather than instance-scoped so
    # the webapp process (which runs :meth:`fetch`) and the renderer
    # process (which runs :meth:`render`) share the same view. Reading
    # per-render is cheap -- one open + json.loads on a ~150-byte file --
    # and the alternative would be a socket/IPC layer just for one card.
    #
    # State files live under ``config.state_dir``:
    #   ``commute_result.json`` -- last successful result
    #   ``commute_error``       -- last error state (single line)

    def _result_file(self):  # type: ignore[no-untyped-def]
        return self.config.state_dir / "commute_result.json"

    def _error_file(self):  # type: ignore[no-untyped-def]
        return self.config.state_dir / "commute_error"

    def _write_result(self, result: CommuteResult) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._result_file().write_text(
            json.dumps(
                {
                    "mode": result.mode,
                    "minutes": result.minutes,
                    "hint": result.hint,
                    "fetched_epoch": result.fetched_epoch,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _read_result(self) -> CommuteResult | None:
        try:
            raw = self._result_file().read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
            return CommuteResult(
                mode=str(payload["mode"]),
                minutes=int(payload["minutes"]),
                hint=str(payload.get("hint", "")),
                fetched_epoch=float(payload["fetched_epoch"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    @property
    def _error_state(self) -> str:
        try:
            raw = self._error_file().read_text(encoding="utf-8").strip()
        except OSError:
            return "idle"
        return raw or "idle"

    @_error_state.setter
    def _error_state(self, value: str) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._error_file().write_text((value or "idle") + "\n", encoding="utf-8")

    def state(self) -> dict:
        """Snapshot used by the webapp to echo the last result under the button."""
        result = self._read_result()
        if result is None:
            return {
                "has_result": False,
                "error_state": self._error_state,
            }
        return {
            "has_result": True,
            "mode": result.mode,
            "minutes": result.minutes,
            "hint": result.hint,
            "fetched_epoch": result.fetched_epoch,
            "error_state": self._error_state,
        }

    # -- render -------------------------------------------------------------

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        result = self._read_result()
        if result is None:
            self._render_placeholder(canvas)
            return
        self._render_result(canvas, result)

    # -- render helpers ------------------------------------------------------

    _PLACEHOLDER_LABELS: dict[str, tuple[str, str, tuple[int, int, int]]] = {
        # "TAP TO ROUTE" read as an instruction to tap the panel, which has no
        # touchscreen. Name the surface that actually has the button instead.
        "idle":     ("COMMUTE",  "WEB: ROUTE NOW", DIM),
        "no_key":   ("COMMUTE",  "API KEY?",       AMBER),
        "no_route": ("COMMUTE",  "NO ROUTE",       AMBER),
        "network":  ("COMMUTE",  "NO NETWORK",     RED),
        "api":      ("COMMUTE",  "API ERROR",      RED),
    }

    def _render_placeholder(self, canvas: Canvas) -> None:
        """Idle / error card. Icon on the left, two SMALL lines on the right.

        Matches the layout the loaded card uses so the transition on the
        first successful fetch is a content swap, not a layout jump.
        """
        mode = self.config.current_commute_mode()
        icon_color = DIM
        self._draw_icon(canvas, 0, (canvas.height - 16) // 2, mode, icon_color)

        label, detail, detail_color = self._PLACEHOLDER_LABELS.get(
            self._error_state, self._PLACEHOLDER_LABELS["idle"]
        )
        right_x = 20
        # fit() both rows for the same reason the loaded card does it: these
        # strings are edited by hand, and an over-long one would otherwise run
        # off the right edge of the panel with no visible failure.
        available = canvas.width - right_x
        canvas.text(right_x, 3, canvas.fit(label, available, SMALL), WHITE, SMALL)
        canvas.text(
            right_x,
            canvas.height - SMALL - 2,
            canvas.fit(detail, available, SMALL),
            detail_color,
            SMALL,
        )

    def _render_result(self, canvas: Canvas, result: CommuteResult) -> None:
        """Loaded card: icon left, minutes-big + two SMALL context lines right."""
        mode = result.mode
        icon_color = _mode_icon_color(mode)
        self._draw_icon(canvas, 0, (canvas.height - 16) // 2, mode, icon_color)

        right_x = 20

        # Top line: "HOME → WORK" on the left, big minutes on the right.
        # Right-flush the minutes so the tens/ones column stays put across
        # 4-min and 44-min values.
        minutes_text = f"{result.minutes}M"
        minutes_color = _minutes_color(result.minutes)
        minutes_width = canvas.text_width(minutes_text, MEDIUM)
        minutes_x = canvas.width - 1 - minutes_width
        # ``HOME → WORK``: the arrow glyph is drawn instead of ASCII "->" so
        # the row reads as a route rather than a comment. Spleen renders
        # a solid-arrow at U+2192, so this survives the panel font.
        route_text = "HOME \u2192 WORK"
        route_available = minutes_x - right_x - 2
        if route_available > 0:
            canvas.text(
                right_x, 0, canvas.fit(route_text, route_available, SMALL), WHITE, SMALL
            )
        canvas.text(minutes_x, 0, minutes_text, minutes_color, MEDIUM)

        # Middle line: hint (transit line / distance). Falls back to the
        # travel-mode name so the row never renders empty.
        hint = result.hint or mode.upper()
        canvas.text(right_x, 12, canvas.fit(hint, canvas.width - right_x, SMALL), BLUE, SMALL)

        # Bottom line: freshness. Greyed if beyond STALE_SECONDS so the
        # user knows to re-tap; otherwise the same DIM as the freshness
        # rows on the market card.
        age_seconds = max(0.0, time.time() - result.fetched_epoch)
        fetched_when = time.strftime("%-I:%M", time.localtime(result.fetched_epoch))
        stamp = f"FETCHED {fetched_when}"
        if age_seconds >= self.STALE_SECONDS:
            stamp = f"{stamp} · STALE"
        canvas.text(
            right_x, canvas.height - SMALL - 1, canvas.fit(stamp, canvas.width - right_x, SMALL), DIM, SMALL
        )

    def _draw_icon(
        self, canvas: Canvas, x: int, y: int, mode: str, color: tuple[int, int, int]
    ) -> None:
        """Blit a 16x16 mode icon at ``(x, y)`` in ``color``.

        Icons are hand-drawn bitmaps rather than fonts so they read as
        pictograms at panel scale (a Unicode 🚉 in Spleen renders as an
        unrecognisable smear at 8-12px). Each glyph is a tuple of 16
        16-char strings; ``1`` lights a pixel.
        """
        glyph = _ICONS.get(mode, _ICONS["transit"])
        for row_index, row in enumerate(glyph):
            for col_index, pixel in enumerate(row):
                if pixel == "1":
                    canvas.fill_rect(x + col_index, y + row_index, 1, 1, color)


def _mode_icon_color(mode: str) -> tuple[int, int, int]:
    """Distinct color per mode so the icon doubles as a mode readout."""
    return {
        "transit":   BLUE,
        "driving":   AMBER,
        "walking":   GREEN,
        "bicycling": BROWN,
    }.get(mode, WHITE)


# 16x16 mode icons. Kept intentionally simple -- a single stroke silhouette
# reads as the object at panel scale better than a detailed rendering
# does. The transit icon is a BART-style train because that's the anchor
# leg for John's actual commute; a bus would be the alternative but reads
# nearly identical at 16x16.
_ICONS: dict[str, tuple[str, ...]] = {
    "transit": (
        "0000000000000000",
        "0000111111110000",
        "0001111111111000",
        "0011111111111100",
        "0111111111111110",
        "0110011001100110",
        "0110011001100110",
        "0111111111111110",
        "0111111111111110",
        "0111100000011110",
        "0111100110011110",
        "0111100110011110",
        "0011111001111100",
        "0000110000110000",
        "0001100000011000",
        "0000000000000000",
    ),
    "driving": (
        "0000000000000000",
        "0000000000000000",
        "0000011111100000",
        "0000111111110000",
        "0000100000010000",
        "0111111111111110",
        "0111111111111110",
        "0100000000000010",
        "0100000000000010",
        "0111111111111110",
        "0011100000011100",
        "0011100000011100",
        "0011100000011100",
        "0001100000011000",
        "0000000000000000",
        "0000000000000000",
    ),
    "walking": (
        "0000001110000000",
        "0000001110000000",
        "0000000000000000",
        "0000011111100000",
        "0000011011000000",
        "0000001110000000",
        "0000011110000000",
        "0000111111000000",
        "0001110110000000",
        "0011000110000000",
        "0000000110000000",
        "0000000110000000",
        "0000001100000000",
        "0000011000000000",
        "0000110000000000",
        "0000000000000000",
    ),
    "bicycling": (
        "0000000000000000",
        "0000000000110000",
        "0000000001100000",
        "0000011111000000",
        "0000000110000000",
        "0000001100000000",
        "0000011000000000",
        "0000110001100000",
        "0001100011110000",
        "0111011111011100",
        "1101111011111110",
        "1100110000110110",
        "1100000000000110",
        "0110000000001100",
        "0011000000011000",
        "0000111111100000",
    ),
}
