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

#: Places Autocomplete (New). Note this is a POST with a JSON body on
#: ``places.googleapis.com``, not a query-string GET on ``maps.googleapis.com``
#: like Directions -- they are separate APIs that must each be enabled on the
#: Cloud project, and the key's API restrictions must list both.
PLACES_AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"

REQUEST_TIMEOUT = 10.0

#: Autocomplete runs while the user types, so it gets a tighter deadline than a
#: deliberate Route tap: a suggestion that lands after the next keystroke is
#: worthless, and the caller falls back to plain typing.
AUTOCOMPLETE_TIMEOUT = 4.0

#: Most suggestions the panel's tiny form can usefully show.
AUTOCOMPLETE_LIMIT = 5

#: Shortest query worth spending a request on. One or two characters match
#: half the planet and burn quota for a list nobody wants.
AUTOCOMPLETE_MIN_CHARS = 3

#: Results are biased, not restricted, to a 20km circle around San Francisco.
#: Bias rather than restrict so an out-of-area address still resolves -- it
#: just ranks below local matches.
AUTOCOMPLETE_BIAS_LAT = 37.7749
AUTOCOMPLETE_BIAS_LNG = -122.4194
AUTOCOMPLETE_BIAS_RADIUS_M = 20000.0

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
            self._render_placeholder(canvas, tick)
            return
        self._render_result(canvas, result, tick)

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

    def _render_placeholder(self, canvas: Canvas, tick: int = 0) -> None:
        """Idle / error card. Icon on the left, two SMALL lines on the right.

        Matches the layout the loaded card uses so the transition on the
        first successful fetch is a content swap, not a layout jump.
        """
        mode = self.config.current_commute_mode()
        icon_color = DIM
        self._draw_icon(canvas, 0, (canvas.height - 16) // 2, mode, icon_color, tick)

        label, detail, detail_color = self._PLACEHOLDER_LABELS.get(
            self._error_state, self._PLACEHOLDER_LABELS["idle"]
        )
        right_x = _CONTENT_X
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

    def _render_result(self, canvas: Canvas, result: CommuteResult, tick: int = 0) -> None:
        """Loaded card: icon left, minutes-big + two SMALL context lines right."""
        mode = result.mode
        icon_color = _mode_icon_color(mode)
        self._draw_icon(canvas, 0, (canvas.height - 16) // 2, mode, icon_color, tick)

        right_x = _CONTENT_X

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
        # "FETCHED 11:59 AND STALE" does not fit: the content column is 100px
        # wide now that the icon column grew, and SMALL text spends ~5px a
        # character. Both variants below are 60px. The verb is what changes --
        # in the stale case "STALE" already implies the number is a past
        # reading, so "FETCHED" adds nothing.
        #
        # Panel strings stay ASCII. A middot here rendered as a 5px blank: the
        # Spleen bitmap fonts advance the cursor for a missing glyph and draw
        # nothing, so it looked like a stray double space rather than an error.
        # test_panel_text_is_ascii guards this.
        if age_seconds >= self.STALE_SECONDS:
            stamp = f"STALE {fetched_when}"
        else:
            stamp = f"FETCHED {fetched_when}"
        canvas.text(
            right_x, canvas.height - SMALL - 1, canvas.fit(stamp, canvas.width - right_x, SMALL), DIM, SMALL
        )

    def _draw_icon(
        self,
        canvas: Canvas,
        x: int,
        y: int,
        mode: str,
        color: tuple[int, int, int],
        tick: int = 0,
    ) -> None:
        """Blit a 24x16 mode icon at ``(x, y)`` in ``color``.

        Icons are bitmaps rather than font glyphs so they read as pictograms at
        panel scale (a Unicode 🚉 in Spleen renders as an unrecognisable smear
        at 8-12px). ``#`` lights a pixel.

        ``walking`` animates: the frame is chosen from *tick* rather than the
        wall clock, matching clock_text() and the earthquake flash, so the
        preview renderer can capture a loop and tests can assert on a specific
        frame. A dropped frame shifts the phase imperceptibly.
        """
        if mode == "walking":
            glyph = _walk_frame(tick, self.config.fps)
        else:
            glyph = _ICONS.get(mode, _ICONS["transit"])
        for row_index, row in enumerate(glyph):
            for col_index, pixel in enumerate(row):
                if pixel == "#":
                    canvas.fill_rect(x + col_index, y + row_index, 1, 1, color)


#: Left edge of the content column: icon width plus a 4px gutter.
_CONTENT_X = 28


def _walk_frame(tick: int, fps: int = 30) -> tuple[str, ...]:
    """Pick the walk pose for *tick*.

    Frame-based rather than clock-based so a captured loop is reproducible.
    """
    ticks_per_step = max(1, fps // _WALK_STEPS_PER_SECOND)
    step = (tick // ticks_per_step) % len(_WALK_SEQUENCE)
    return _WALK_FRAMES[_WALK_SEQUENCE[step]]


def _mode_icon_color(mode: str) -> tuple[int, int, int]:
    """Distinct color per mode so the icon doubles as a mode readout."""
    return {
        "transit":   BLUE,
        "driving":   AMBER,
        "walking":   GREEN,
        "bicycling": BROWN,
    }.get(mode, WHITE)


# Mode icons, 24 wide x 16 tall in a 28px left column.
#
# These are generated geometry, not hand-typed pixels: drawn as circles,
# polygons and thick strokes at 8x and downsampled. Hand-typing at this size
# produced blobs -- solid fills lose their silhouette, and the first pass at a
# car read as an insect. Stroke outlines with punched-out windows survive the
# downsample; solid masses do not.
#
# The column was 16px wide and grew to 24 because vehicles are ~2:1 objects: a
# bike needs two wheels side by side and a train needs a window band, and
# neither fits in 16. The pedestrian is the opposite shape and only uses ~10 of
# the 24, centred -- an icon set with one tall glyph among wide ones is normal,
# and a fixed column is what keeps the text to its right from shifting between
# modes.
_ICON_WIDTH = 24

# The walking figure animates. Three unique poses, cycled wide -> mid ->
# narrow -> mid, which is why the sequence indexes frame 1 twice.
#
# The poses are sampled away from the stride's zero crossings on purpose. A
# plain sine through zero puts the limbs flat against the torso, and at this
# size that frame reads as a vertical bar rather than a person -- the animation
# looked like a blinking stick. Clamping the angles away from zero keeps every
# frame legible as a walker.
_WALK_FRAMES: tuple[tuple[str, ...], ...] = (
    (
        "........................",
        "..........####..........",
        "..........####..........",
        "..........####..........",
        "...........##...........",
        "...........##...........",
        "..........####..........",
        ".........######.........",
        ".........#######........",
        ".........######.........",
        ".........##.##..........",
        "........##...##.........",
        ".......##....##.........",
        "......##.....##.........",
        "..............#.........",
        "........................",
    ),
    (
        "........................",
        "..........####..........",
        "..........####..........",
        "..........####..........",
        "...........##...........",
        "...........##...........",
        "..........####..........",
        "..........####..........",
        ".........######.........",
        ".........######.........",
        ".........#####..........",
        ".........##.##..........",
        "........##...#..........",
        ".......###...##.........",
        "........#....##.........",
        "........................",
    ),
    (
        "........................",
        "..........####..........",
        "..........####..........",
        "..........####..........",
        "...........##...........",
        "...........##...........",
        "..........###...........",
        "..........####..........",
        "..........####..........",
        ".........######.........",
        "..........####..........",
        ".........#####..........",
        ".........##.##..........",
        "........##..##..........",
        "........##..##..........",
        "........................",
    ),
)

#: Which frame to show at each step of the cycle.
_WALK_SEQUENCE = (0, 1, 2, 1)

#: Steps per second for the walk cycle. 5 gives a ~1.25 stride/sec pace at the
#: default 30fps, which is a walk; 10 looked like a panic.
_WALK_STEPS_PER_SECOND = 5

_ICONS: dict[str, tuple[str, ...]] = {
    "transit": (
        "........................",
        "........................",
        "...###################..",
        ".######################.",
        ".######################.",
        ".##....#...##...#...###.",
        ".##....#...##...#...###.",
        ".###..##...##..###..###.",
        ".######################.",
        ".######################.",
        "..####################..",
        "..#####################.",
        "...####........####.....",
        "...####........##.#.....",
        "...####.........###.....",
        "........................",
    ),
    "driving": (
        "........................",
        "........................",
        "........................",
        "........................",
        ".........######.........",
        "........########........",
        ".......####...#.........",
        ".......###....#.........",
        "..#################.....",
        ".######################.",
        ".######################.",
        "...##..##......##..##...",
        "...######......######...",
        "....####........####....",
        "........................",
        "........................",
    ),
    "bicycling": (
        "........................",
        "........................",
        "........................",
        "..............###.......",
        "........####..####......",
        "........###....##.......",
        "........###...###.......",
        "...########..##.#####...",
        "..#######.##.#..######..",
        ".##...###.####.####..##.",
        ".##..##.##.##..#..#..##.",
        ".#...########.##..#...#.",
        ".##.....##.....#.....##.",
        ".##....##......##....##.",
        "..######........######..",
        "....###..........###....",
    ),
    # walking is drawn from _WALK_FRAMES; this is the pose used when a still
    # frame is needed (and keeps every mode present in this map).
    "walking": _WALK_FRAMES[0],
}


class AutocompleteUnavailable(RuntimeError):
    """Autocomplete could not run. Carries a short reason for the UI.

    Separate from the mode's file-backed error states: this is a per-keystroke
    transient that must never block typing, so the caller shows the reason and
    lets the user finish the address by hand.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


#: Country suffixes Google appends that are dead weight here. Every suggestion
#: is biased to San Francisco and the panel is a commute clock, so ", USA" is
#: never the distinguishing part of an address -- but it is ~5 characters that
#: pushed each row in the dropdown onto a second line.
_COUNTRY_SUFFIXES = (", USA", ", United States")


def _trim_country(address: str) -> str:
    """Drop a trailing country from a formatted address.

    Safe for routing: Directions geocodes "181 Fremont Street, San Francisco,
    CA 94105" identically, because the state and ZIP already pin it.
    """
    for suffix in _COUNTRY_SUFFIXES:
        if address.endswith(suffix):
            return address[: -len(suffix)]
    return address


def autocomplete_addresses(
    query: str,
    api_key: str,
    opener=None,  # type: ignore[no-untyped-def]
    limit: int = AUTOCOMPLETE_LIMIT,
) -> list[str]:
    """Return up to *limit* address completions for *query*.

    Wraps Places Autocomplete (New). Returns the full formatted address text of
    each suggestion, which is what Directions wants -- no Place Details call and
    no session token.

    That is a deliberate cost decision. Session tokens only pay off when a
    session terminates in a Place Details request, which bundles the whole
    session into one billable unit. We never call Place Details: Directions
    geocodes the address string perfectly well, and a place ID would buy nothing
    but an extra billable request. Without that terminating call, a session
    bills per request either way, so the token would add complexity for no
    saving. Autocomplete requests bill per request under the Essentials tier
    with a monthly free allowance, and the field mask below keeps every request
    inside that tier -- asking for a field outside it silently upgrades the
    whole request to a more expensive SKU.

    Raises :class:`AutocompleteUnavailable` for anything the caller should
    surface as "type it yourself" rather than as a broken form.
    """
    text = query.strip()
    if len(text) < AUTOCOMPLETE_MIN_CHARS:
        return []
    key = api_key.strip()
    if not key:
        raise AutocompleteUnavailable("no_key", "no Google Maps API key configured")

    body = json.dumps(
        {
            "input": text,
            # Bias, not restrict -- see AUTOCOMPLETE_BIAS_* above.
            "locationBias": {
                "circle": {
                    "center": {
                        "latitude": AUTOCOMPLETE_BIAS_LAT,
                        "longitude": AUTOCOMPLETE_BIAS_LNG,
                    },
                    "radius": AUTOCOMPLETE_BIAS_RADIUS_M,
                }
            },
            "regionCode": "US",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        PLACES_AUTOCOMPLETE_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            # Request exactly the one field we render. The field mask is not
            # just a bandwidth trim: it selects the billing SKU.
            "X-Goog-FieldMask": "suggestions.placePrediction.text.text",
        },
    )
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=AUTOCOMPLETE_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # pragma: no cover - best-effort diagnostics only
            pass
        # 403 here almost always means Places API (New) is not enabled on the
        # project, or the API key's restriction list covers Directions only.
        # Both are one-time Cloud console fixes, so say which it is instead of
        # reporting a generic failure.
        reason = "not_enabled" if error.code in (403, 401) else "api"
        LOGGER.warning("commute: places autocomplete HTTP %s: %s", error.code, detail)
        raise AutocompleteUnavailable(reason, detail) from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        LOGGER.warning("commute: places autocomplete failed: %s", error)
        raise AutocompleteUnavailable("network", str(error)) from error
    except json.JSONDecodeError as error:
        LOGGER.warning("commute: places autocomplete payload not JSON: %s", error)
        raise AutocompleteUnavailable("api", str(error)) from error

    suggestions: list[str] = []
    for entry in payload.get("suggestions") or []:
        prediction = (entry or {}).get("placePrediction") or {}
        value = _trim_country(((prediction.get("text") or {}).get("text") or "").strip())
        # Dedupe: distinct place IDs can share a formatted address (a building
        # and a business inside it), and two identical rows look like a bug.
        if value and value not in suggestions:
            suggestions.append(value)
        if len(suggestions) >= limit:
            break
    return suggestions
