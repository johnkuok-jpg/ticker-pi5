# MIT License — Copyright (c) 2026 John Kuok
"""Recent earthquakes from the USGS public feed.

USGS publishes a GeoJSON summary of every significant seismic event under
``/earthquakes/feed/v1.0/summary/``. The M4.5+ 24-hour file is the one that
matches a desk-panel's tempo: a handful of events any given day, enough that
the mode has something to show, few enough that each card lands on screen for
a moment worth reading. No key required, no rate limit for a hobby polling
cadence.

The mode is deliberately quiet-by-default: when the feed is empty (or briefly
unreachable) the panel shows a single "no significant quakes" card rather than
churning through a stale rotation. On the other hand, when a big one hits, the
card that surfaces it is the point of the whole mode -- so the fetch interval
is short enough (5 min) that "just now" reads as just now.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode
from ticker.quake_alert import QuakeAlert

LOGGER = logging.getLogger(__name__)

# USGS publishes several thresholds; 4.5+ is the sweet spot for "something
# happened somewhere in the world today" without spamming the panel with the
# constant low-mag chatter along the Ring of Fire. When the user lowers the
# display filter below 4.5 we switch to the M2.5+ daily feed instead, since
# the 4.5+ daily feed literally does not contain sub-4.5 events.
FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{threshold}_day.geojson"


def _feed_url_for(min_mag: float) -> str:
    """Pick the smallest USGS feed that covers the requested threshold.

    USGS publishes tiered summary feeds; when the user filters below M4.5
    we need the 2.5+ feed since the 4.5+ feed literally does not contain
    sub-4.5 events. Anything at or above 4.5 uses the smaller 4.5+ feed
    (faster fetch, smaller payload).
    """
    if min_mag < 4.5:
        return FEED_URL.format(threshold="2.5")
    return FEED_URL.format(threshold="4.5")
REQUEST_TIMEOUT = 8.0
USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

# Colour palette echoes the market/crypto amber-on-white idiom, with a red
# reserved for genuinely big events so a glance can distinguish "moderate" from
# "wake the neighbours".
AMBER = (255, 176, 0)
WHITE = (235, 240, 250)
DIM = (96, 108, 132)
RED = (255, 70, 70)
ORANGE = (255, 130, 40)

# Magnitude bands for text colour. A 6.0+ is roughly the threshold at which
# real structural damage becomes common in unreinforced buildings, so it earns
# the red; 5.0-5.9 is "widely felt, minor damage" and gets an orange step; the
# rest stays amber.
BIG_MAG = 6.0
NOTABLE_MAG = 5.0


@dataclass(frozen=True)
class Quake:
    """One row extracted from the USGS GeoJSON.

    ``time_ms`` is the event origin time in Unix ms as published by USGS; we
    keep it raw so age can be recomputed each frame without re-parsing.
    """

    magnitude: float
    place: str
    time_ms: int

    def color(self) -> tuple[int, int, int]:
        return _color_for_magnitude(self.magnitude)


def _color_for_magnitude(magnitude: float) -> tuple[int, int, int]:
    """Severity band -> colour. Extracted so the alert path can reuse it
    without dressing a scalar magnitude up as a Quake dataclass first."""
    if magnitude >= BIG_MAG:
        return RED
    if magnitude >= NOTABLE_MAG:
        return ORANGE
    return AMBER


def _short_region(region: str) -> str:
    """Abbreviate the region label for the alert header.

    California in particular has a well-known two-letter form; other US state
    names are also two letters. For anything else we uppercase and clip so a
    long string like ``"Pacific Northwest"`` still fits.
    """
    text = (region or "").strip()
    if not text:
        return "GLOBAL"
    if text.lower() == "california":
        return "CA"
    upper = text.upper()
    return upper if len(upper) <= 10 else upper[:10]


def _clean_place(place: str) -> str:
    """Trim USGS's "42 km N of Foo" phrasing so the important word fits first.

    USGS place strings come in two shapes:

    * ``"<distance> km <bearing> of <name>"`` -- e.g. ``"80 km N of Ruteng,
      Indonesia"``. The distance/bearing is context for a seismologist but
      not what a glance at a desk panel is asking; the place name is. We
      drop the prefix so the row reads ``"Ruteng, Indonesia"`` and clips (if
      it must) at the country instead of at the compass bearing.
    * plain ``"<name>"`` -- e.g. ``"Central California"``. Leave as-is.

    We also don't try to be clever with unit conversion: the km/mi debate is
    a rabbit hole and the panel doesn't have the pixels to spend on either.
    """
    text = (place or "").strip()
    # Case-insensitive match on " of " because occasional entries use "OF".
    lower = text.lower()
    idx = lower.rfind(" of ")
    if idx > 0:
        after = text[idx + 4 :].strip()
        if after:
            return after
    return text


def _relative_time(now_seconds: float, event_ms: int) -> str:
    """Render "12m ago" / "3h ago" / "2d ago" in as few chars as possible."""
    if event_ms <= 0:
        return ""
    delta = max(0.0, now_seconds - event_ms / 1000.0)
    if delta < 60:
        return "just now"
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


class EarthquakesMode(Mode):
    """Rotating cards of recent M4.5+ events; quiet when the feed is empty."""

    # USGS refreshes the summary feed once a minute; five minutes is plenty
    # fresh for a desk display and keeps the polling load negligible.
    CACHE_SECONDS = 300
    ERROR_BACKOFF_SECONDS = 120

    # How long each card sits on screen before rotating. Six seconds mirrors
    # the stocks/crypto card cadence, so a mode-cycle feels consistent.
    ROTATE_SECONDS = 6

    # Cap so a very active day (2011 Tohoku aftershock swarms did this) doesn't
    # produce a 40-card rotation that never lands on the one you noticed.
    MAX_ROWS = 6

    # Threshold segment of the URL. Kept as an instance attribute so a test or
    # a future config option can override without touching the class.
    FEED_THRESHOLD = "4.5"

    def __init__(self, config, opener=None, alert_source=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.quakes: list[Quake] = []
        self._opener = opener or urllib.request.urlopen
        # Far enough back that the first render always fetches.
        self._last_refresh = -1e9
        self._failed = False
        # Cache invalidation: remember which feed URL produced `self.quakes`
        # so a webapp filter edit that changes the feed (e.g. crossing the
        # M4.5 threshold) forces a re-fetch on the next frame.
        self._cached_feed_url: str | None = None
        # A callable that returns the current QuakeAlert (or None). The
        # renderer wires this to its long-lived QuakeAlertWatcher so the mode
        # doesn't own the polling; the mode is a pure display of whatever
        # alert state exists. Left as ``None`` in tests and manual selection
        # so the passive rotation renders unchanged.
        self._alert_source = alert_source

    # -- data ---------------------------------------------------------------

    def _fetch(self, feed_url: str) -> list[Quake] | None:
        request = urllib.request.Request(
            feed_url,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
            LOGGER.warning("earthquakes request failed: %s", error)
            return None
        features = payload.get("features")
        if not isinstance(features, list):
            return None

        quakes: list[Quake] = []
        for feature in features:
            try:
                props = feature["properties"]
                mag = float(props["mag"])
                place = str(props.get("place") or "")
                time_ms = int(props.get("time") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if not place or time_ms <= 0:
                continue
            quakes.append(Quake(magnitude=mag, place=_clean_place(place), time_ms=time_ms))

        # USGS ships oldest-first sometimes; sort newest-first so the card the
        # user is most likely to care about is the one that comes up first.
        quakes.sort(key=lambda q: q.time_ms, reverse=True)
        # Return the raw newest-first list; the caller applies user filter and
        # the MAX_ROWS cap so a filter change doesn't require a refetch when
        # the raw payload is still fresh in memory.
        return quakes

    def _apply_filter(
        self, quakes: list[Quake], min_mag: float, region: str
    ) -> list[Quake]:
        """Drop quakes below the mag floor or outside the region substring.

        Region uses the same case-insensitive substring match as the alert
        watcher's ``_in_region``, minus the California bbox fallback -- the
        display doesn't need the bbox because a user who typed "California"
        can just as easily type it into the dropdown.
        """
        region_lower = region.strip().lower()
        filtered = [
            q for q in quakes
            if q.magnitude >= min_mag
            and (not region_lower or region_lower in q.place.lower())
        ]
        return filtered[: self.MAX_ROWS]

    def _refresh(self, feed_url: str) -> None:
        quakes = self._fetch(feed_url)
        if quakes is None:
            self._failed = True
            self._last_refresh = time.monotonic()
            return
        # An empty list is a legitimate result -- it means the quiet-day
        # placeholder card should appear -- so it is not a failure.
        self.quakes = quakes
        self._cached_feed_url = feed_url
        self._failed = False
        self._last_refresh = time.monotonic()

    # -- render -------------------------------------------------------------

    def _draw_header(self, canvas: Canvas, min_mag: float, region: str) -> None:
        """Top-of-panel eyebrow: source + threshold + region abbreviation.

        The header echoes the current filter so a user who narrowed to
        California sees ``USGS  M3.5+  CA`` rather than the misleading fixed
        ``USGS 24H  M4.5+`` label. Time window drops out of the header when
        the filter is engaged because the panel then sources from the M2.5+
        hourly/daily feed rather than the M4.5+ 24-hour feed and the label
        would be a lie.
        """
        threshold = f"M{min_mag:g}+"
        region_label = _short_region(region)
        parts = ["USGS", threshold]
        if region_label:
            parts.append(region_label)
        canvas.text(1, 1, canvas.fit("  ".join(parts)), (95, 135, 195), SMALL)
        canvas.hline(11, (26, 36, 56))

    def _draw_alert_header(self, canvas: Canvas, tick: int) -> None:
        """Alerting header: draws attention without becoming a strobe.

        The header background flashes for the first two seconds by inverting
        (red text on the eyebrow line) and then settles to steady red. A
        continuous flash would be maddening on a desk panel; a brief attract
        beat and then a static bar is the tradeoff that reads as urgent
        without training the user to look away.
        """
        flash_on = (tick // max(1, self.config.fps // 4)) % 2 == 0
        flashing_window = tick < self.config.fps * 2
        color = RED if not (flashing_window and flash_on) else (255, 200, 200)
        # Header echoes the config: users who tune QUAKE_ALERT_MIN_MAG see
        # the current threshold on the alert bar. Region substring gets
        # abbreviated to the first eight chars so "California" -> "CALIFORN"
        # only if we really need to fit -- for the default "California" we
        # abbreviate manually to "CA" because that reads better.
        # Header labels reflect the *effective* settings (state file overlaid
        # on .env), not the .env defaults, so a webapp change shows up in the
        # ALERT header on the next frame.
        region_label = _short_region(self.config.current_quake_alert_region())
        threshold_label = f"M{self.config.current_quake_alert_min_mag():g}+"
        canvas.text(1, 1, canvas.fit(f"ALERT  {region_label}  {threshold_label}"), color, SMALL)
        canvas.hline(11, (90, 25, 25))

    def _draw_alert(self, canvas: Canvas, alert: QuakeAlert, tick: int) -> None:
        """Alerting card: magnitude MEDIUM + attract flash, place, ago.

        We compute a live-updating "3s ago" from wall clock and the USGS
        origin time so the panel reads as a real-time monitor while the
        alert holds. The rotation is intentionally paused for alerts -- the
        whole point is that this one card gets the panel to itself.
        """
        # First 3 seconds: magnitude flashes at 4 Hz to draw the eye. After
        # that it stays steady in its severity colour so a glance across the
        # room still reads the number.
        attract_beat = (tick // max(1, self.config.fps // 4)) % 2 == 0
        attract_window = tick < self.config.fps * 3
        mag_color = _color_for_magnitude(alert.magnitude)
        if attract_window and not attract_beat:
            mag_color = (60, 20, 20)  # "off" half of the strobe -- dim, not black
        mag_text = f"M{alert.magnitude:.1f}"
        canvas.text(1, 14, mag_text, mag_color, MEDIUM)
        mag_end = canvas.text_width(mag_text, MEDIUM) + 4

        # Place beside the magnitude. Same trim logic as passive rotation.
        place_budget = canvas.width - mag_end - 1
        canvas.text(
            mag_end,
            15,
            canvas.fit(_clean_place(alert.place), place_budget, SMALL),
            WHITE,
            SMALL,
        )

        rel = _relative_time(time.time(), alert.time_ms)
        if rel:
            canvas.text(1, 24, rel, DIM, SMALL)

    def _draw_quiet(self, canvas: Canvas, min_mag: float, region: str) -> None:
        """When the feed is up but has nothing above threshold today."""
        threshold = f"M{min_mag:g}+"
        if region:
            # Two lines: threshold on top, region on bottom. Region gets the
            # same short-region treatment as the header so "California" -> "CA".
            canvas.text(1, 15, canvas.fit(f"No {threshold} quakes"), WHITE, SMALL)
            canvas.text(1, 24, canvas.fit(f"in {_short_region(region)}"), DIM, SMALL)
        else:
            canvas.text(1, 15, canvas.fit(f"No {threshold} quakes"), WHITE, SMALL)
            canvas.text(1, 24, "in the last 24 hours.", DIM, SMALL)

    def _draw_error(self, canvas: Canvas, tick: int) -> None:
        """When the fetch itself failed. Keeps the header so it looks alive."""
        canvas.scroll_text(20, "QUAKES: FEED UNREACHABLE", DIM, tick * 2, SMALL)

    def _draw_quake(self, canvas: Canvas, quake: Quake) -> None:
        """One card: magnitude on the left, place scrolled or fit on the right,
        relative time on a second row underneath."""
        mag_text = f"M{quake.magnitude:.1f}"
        # Magnitude anchors the card in MEDIUM so it reads at a glance.
        canvas.text(1, 14, mag_text, quake.color(), MEDIUM)
        mag_end = canvas.text_width(mag_text, MEDIUM) + 4

        # Place on the same row as the magnitude, right of the divider. If it
        # doesn't fit in SMALL either, canvas.fit clips it with a trailing
        # ellipsis rather than truncating mid-word -- readable at a glance.
        place_budget = canvas.width - mag_end - 1
        canvas.text(
            mag_end,
            15,
            canvas.fit(quake.place, place_budget, SMALL),
            WHITE,
            SMALL,
        )

        # Relative time on the bottom row, dim so it reads as metadata.
        rel = _relative_time(time.time(), quake.time_ms)
        if rel:
            canvas.text(1, 24, rel, DIM, SMALL)

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()

        # Alert branch: when the watcher has flagged a fresh in-region shake,
        # pin the card and don't rotate. Alerts short-circuit the passive
        # feed entirely -- including the background fetch, because the M2.5+
        # hourly feed the watcher polls already covers everything the M4.5+
        # daily feed would show and then some.
        alert = self._alert_source() if self._alert_source else None
        if alert is not None:
            self._draw_alert_header(canvas, tick)
            self._draw_alert(canvas, alert, tick)
            return

        # Read the display filter live so a webapp edit takes effect on the
        # next frame -- no service restart, no waiting on the cache TTL.
        min_mag = self.config.current_quake_filter_min_mag()
        region = self.config.current_quake_filter_region()
        feed_url = _feed_url_for(min_mag)

        # Refetch if the cache is stale OR the required feed URL changed --
        # e.g. the user just crossed the M4.5 threshold and we now need the
        # M2.5+ feed. Feed switches invalidate immediately because the cached
        # list from the previous feed is a subset that would hide events the
        # new filter should show.
        age_limit = self.ERROR_BACKOFF_SECONDS if self._failed else self.CACHE_SECONDS
        stale = time.monotonic() - self._last_refresh >= age_limit
        feed_changed = self._cached_feed_url != feed_url
        if stale or feed_changed:
            self._refresh(feed_url)

        self._draw_header(canvas, min_mag, region)

        if self._failed and not self.quakes:
            self._draw_error(canvas, tick)
            return

        visible = self._apply_filter(self.quakes, min_mag, region)

        if not visible:
            self._draw_quiet(canvas, min_mag, region)
            return

        # Rotate through the *filtered* list. The last-good raw list stays in
        # memory through a failed refresh, so a temporary outage doesn't blank
        # the mode -- and the filter still applies to that stale data.
        frames = max(1, int(self.ROTATE_SECONDS * max(1, self.config.fps)))
        current = visible[(tick // frames) % len(visible)]
        self._draw_quake(canvas, current)
