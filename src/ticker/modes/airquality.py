# MIT License — Copyright (c) 2026 John Kuok
"""US air quality, UV index, and pollen for the weather location.

Three panels rotate through one mode so the AQI card doesn't lose its identity
just because UV and pollen were bolted on. Each panel keeps the same left-hand
hero column and right-hand label + trend layout that the original AQI card set,
so the eye already knows where to look when the slide swaps.

- AQI + UV both come from Open-Meteo's air-quality endpoint (one call, no key).
  Open-Meteo returns the US EPA index directly and hourly, so the panel doesn't
  have to derive an AQI from raw concentrations - that conversion has
  pollutant-specific breakpoints and a "worst pollutant wins" rule that's easy
  to get subtly wrong.
- Pollen for the US comes from Google's Pollen API. Open-Meteo carries pollen
  too, but only for Europe (CAMS coverage). Google's key is the same
  GOOGLE_MAPS_API_KEY the commute mode already uses -- just enable Pollen API
  on the same Cloud project. If the key is missing, the pollen panel drops out
  of the rotation instead of showing an error slide the user can't fix.

Categories, names and colours for AQI are the EPA's own, from the AQI Technical
Assistance Document (May 2026):
https://document.airnow.gov/technical-assistance-document-for-the-reporting-of-daily-air-quailty.pdf

UV colours follow WHO/EPA UV Index bands (Green 0-2, Yellow 3-5, Orange 6-7,
Red 8-10, Violet 11+).

Pollen index uses Google's 0-5 UPI (Universal Pollen Index) with their band
colours, mapped to the panel's readable palette.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ticker.canvas import HUGE, LARGE, SMALL, Canvas
from ticker.modes.base import Mode

LOGGER = logging.getLogger(__name__)

AQ_API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
POLLEN_API_URL = "https://pollen.googleapis.com/v1/forecast:lookup"
REQUEST_TIMEOUT = 8.0
USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

WHITE = (235, 240, 250)
DIM = (108, 122, 148)
ERROR = (255, 150, 60)
LOADING = (130, 180, 255)

TREND_HOURS = 24
GOOD_LIMIT = 50

# The rightmost column sits against the panel's bezel, where a glyph edge or a
# chart's last bar is easy to lose. Every mode leaves it dark.
RIGHT_INSET = 1
# Two HUGE digits and three LARGE digits are both 24px wide, so the hero column
# can be fixed. A pinned column keeps the label, PM2.5 row and chart from
# sliding sideways when the reading crosses 10 or 100.
HERO_WIDTH = 24
COLUMN_X = HERO_WIDTH + 5

# Six seconds per panel is long enough to actually read the number and its
# trailing detail, short enough that the rotation feels responsive when a user
# taps AQI wanting a specific reading. Matches the pacing weather uses.
SLIDE_SECONDS = 6

# The official AQI palette, in the EPA's standard RGB values.
AQI_CATEGORIES: tuple[tuple[int, str, tuple[int, int, int]], ...] = (
    (50, "GOOD", (0, 228, 0)),
    (100, "MODERATE", (255, 255, 0)),
    # "Unhealthy for Sensitive Groups" is 30 characters and will not fit beside
    # a three-digit reading at any font this panel has.
    (150, "SENSITIVE", (255, 126, 0)),
    (200, "UNHEALTHY", (255, 0, 0)),
    (300, "V UNHEALTHY", (143, 63, 151)),
    (10**6, "HAZARDOUS", (126, 0, 35)),
)

# WHO / EPA UV Index bands, with the same palette lift the AQI colours get.
UV_CATEGORIES: tuple[tuple[float, str, tuple[int, int, int]], ...] = (
    (2.0, "LOW", (0, 228, 0)),
    (5.0, "MODERATE", (255, 255, 0)),
    (7.0, "HIGH", (255, 126, 0)),
    (10.0, "V HIGH", (255, 0, 0)),
    (10**6, "EXTREME", (143, 63, 151)),
)

# Google Pollen's Universal Pollen Index runs 0-5. The category labels below
# match Google's UPI category names, shortened where necessary.
POLLEN_CATEGORIES: tuple[tuple[int, str, tuple[int, int, int]], ...] = (
    (0, "NONE", (108, 122, 148)),
    (1, "V LOW", (0, 228, 0)),
    (2, "LOW", (170, 220, 0)),
    (3, "MODERATE", (255, 255, 0)),
    (4, "HIGH", (255, 126, 0)),
    (5, "V HIGH", (255, 0, 0)),
)

# Perceptual luminance floor for text on this panel, the same threshold the
# other modes' colours are checked against.
LUMINANCE_FLOOR = 62.0


def _luminance(color: tuple[int, int, int]) -> float:
    red, green, blue = color
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def panel_color(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """Lift a colour to the panel's legibility floor, changing it as little as possible.

    The EPA colours are specified for print. Two of the six are too dark to read
    as glyphs on an LED panel running at a third of full brightness: pure red
    scores 54 against a floor of 62, and hazardous maroon only 29. Blending in
    white a tenth at a time keeps the hue and stops at the first value that
    clears the floor, so red stays unmistakably red and maroon stays maroon
    rather than being swapped for some brighter substitute. The other four
    categories already pass and come through untouched.
    """
    lifted = color
    for _ in range(10):
        if _luminance(lifted) >= LUMINANCE_FLOOR:
            break
        lifted = tuple(min(255, round(channel + (255 - channel) * 0.10)) for channel in lifted)  # type: ignore[assignment]
    return lifted


def classify_aqi(aqi: int) -> tuple[str, tuple[int, int, int]]:
    """Category name and panel-safe colour for an AQI value."""
    for limit, name, color in AQI_CATEGORIES:
        if aqi <= limit:
            return name, panel_color(color)
    name, color = AQI_CATEGORIES[-1][1], AQI_CATEGORIES[-1][2]
    return name, panel_color(color)


# Keep the legacy name so any external callers or tests can still import it.
classify = classify_aqi


def classify_uv(uv: float) -> tuple[str, tuple[int, int, int]]:
    """Category name and panel-safe colour for a UV index value."""
    for limit, name, color in UV_CATEGORIES:
        if uv <= limit:
            return name, panel_color(color)
    name, color = UV_CATEGORIES[-1][1], UV_CATEGORIES[-1][2]
    return name, panel_color(color)


def classify_pollen(upi: int) -> tuple[str, tuple[int, int, int]]:
    """Category name and panel-safe colour for a UPI value (0-5).

    Google publishes UPI as an integer 0-5; anything outside that range is
    clamped rather than escalated to a fabricated "off-scale" band, so a bad
    API response can't paint the panel a colour the API never authorised.
    """
    clamped = max(0, min(5, int(upi)))
    _, name, color = POLLEN_CATEGORIES[clamped]
    return name, panel_color(color)


@dataclass(frozen=True)
class AirQuality:
    aqi: int
    pm2_5: float | None
    trend: tuple[float, ...]


@dataclass(frozen=True)
class UvReading:
    now: float
    trend: tuple[float, ...]  # 24h historical, hourly


@dataclass(frozen=True)
class PollenReading:
    """Google Pollen UPIs for the three plant types.

    The heroed value is the max of the three, which matches how allergy apps
    present a single daily number, and the detail row spells out which plant
    is driving it so the number isn't ambiguous.
    """

    tree: int | None
    grass: int | None
    weed: int | None

    def overall(self) -> int:
        candidates = [v for v in (self.tree, self.grass, self.weed) if v is not None]
        return max(candidates) if candidates else 0

    def dominant(self) -> str:
        pairs = [
            ("TREE", self.tree),
            ("GRASS", self.grass),
            ("WEED", self.weed),
        ]
        best_name, best_value = "", -1
        for name, value in pairs:
            if value is None:
                continue
            if value > best_value:
                best_name, best_value = name, value
        return best_name


class AirQualityMode(Mode):
    """Rotate AQI, UV, and pollen slides for the current weather location."""

    # The AQI/UV index is published hourly, so polling faster only spends
    # someone else's bandwidth to redraw the same number.
    CACHE_SECONDS = 900
    # Google Pollen is daily, and each call spends a paid unit. Cache 6 hours:
    # a user tapping the panel twice in an afternoon shouldn't cost two calls,
    # but a full workday of the panel being live should still refresh before
    # the evening.
    POLLEN_CACHE_SECONDS = 6 * 3600
    ERROR_BACKOFF_SECONDS = 120

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.reading: AirQuality | None = None
        self.uv: UvReading | None = None
        self.pollen: PollenReading | None = None
        self._last_refresh = -1e9
        self._last_pollen_refresh = -1e9
        self._failed = False
        self._pollen_failed = False

    # -- network ---------------------------------------------------------------

    def _fetch(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read()

    def _refresh(self) -> None:
        """One Open-Meteo call gives us AQI, PM2.5, UV, and the AQI/UV trend."""
        lat, lon = self.config.current_weather_coords()
        query = urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "current": "us_aqi,pm2_5,uv_index",
                "hourly": "us_aqi,uv_index",
                "past_days": 1,
                "forecast_days": 1,
                "timezone": self.config.timezone or "auto",
            }
        )
        try:
            payload = json.loads(self._fetch(f"{AQ_API_URL}?{query}").decode("utf-8", "replace"))
            current = payload["current"]
            aqi = current.get("us_aqi")
            if aqi is None:
                raise ValueError("no us_aqi in response")
            pm2_5 = current.get("pm2_5")
            current_time = str(current.get("time", ""))
            self.reading = AirQuality(
                aqi=int(round(float(aqi))),
                pm2_5=float(pm2_5) if pm2_5 is not None else None,
                trend=self._series(payload, current_time, "us_aqi"),
            )
            uv_now = current.get("uv_index")
            if uv_now is not None:
                self.uv = UvReading(
                    now=float(uv_now),
                    trend=self._series(payload, current_time, "uv_index"),
                )
            else:
                self.uv = None
            self._failed = False
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
            LOGGER.debug("air quality refresh failed: %s", error)
            self._failed = True
        finally:
            self._last_refresh = time.monotonic()

    def _refresh_pollen(self) -> None:
        """Ask Google for today's pollen; skip silently if the API key is unset."""
        key = self.config.google_maps_api_key.strip()
        if not key:
            self.pollen = None
            self._pollen_failed = False
            self._last_pollen_refresh = time.monotonic()
            return
        lat, lon = self.config.current_weather_coords()
        query = urllib.parse.urlencode(
            {
                "location.latitude": lat,
                "location.longitude": lon,
                "days": 1,
                # Plant descriptions add hundreds of KB per response and the
                # panel never renders them.
                "plantsDescription": "false",
                "key": key,
            }
        )
        try:
            payload = json.loads(self._fetch(f"{POLLEN_API_URL}?{query}").decode("utf-8", "replace"))
            self.pollen = _extract_pollen(payload)
            self._pollen_failed = False
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
            LOGGER.debug("pollen refresh failed: %s", error)
            self._pollen_failed = True
        finally:
            self._last_pollen_refresh = time.monotonic()

    def _series(self, payload: dict, current_time: str, key: str) -> tuple[float, ...]:
        """The 24 hours ending now for *key* in the hourly block.

        The response also carries forecast hours, which must not be charted as
        history: the request spans a past day and a forecast day, so the series
        is cut at the hour the current reading belongs to.
        """
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        values = hourly.get(key) or []
        if not values:
            return ()
        try:
            end = times.index(current_time)
        except ValueError:
            end = len(values) - 1
        window = values[max(0, end - TREND_HOURS + 1) : end + 1]
        return tuple(float(value) for value in window if value is not None)

    # -- panel selection -------------------------------------------------------

    def _panels(self) -> list[str]:
        """Which slides are eligible right now.

        AQI is always in the rotation once we have a reading. UV joins if the
        response carried a uv_index (Open-Meteo returns it globally, but a
        network failure or a stale cache can leave uv unset). Pollen joins if
        the Pollen API key is configured -- with no key we hide the slide
        entirely rather than show a persistent error card the user can't fix.
        """
        panels: list[str] = []
        if self.reading is not None:
            panels.append("aqi")
        if self.uv is not None:
            panels.append("uv")
        if self.pollen is not None:
            panels.append("pollen")
        return panels

    # -- render ----------------------------------------------------------------

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()

        lat, lon = self.config.current_weather_coords()
        if not lat or not lon:
            canvas.text_centered(6, "AIR QUALITY", ERROR, SMALL)
            canvas.text_centered(18, "SET WEATHER ZIP", ERROR, SMALL)
            return

        aq_due = self.CACHE_SECONDS if not self._failed else self.ERROR_BACKOFF_SECONDS
        if time.monotonic() - self._last_refresh >= aq_due:
            self._refresh()

        pollen_due = self.POLLEN_CACHE_SECONDS if not self._pollen_failed else self.ERROR_BACKOFF_SECONDS
        if time.monotonic() - self._last_pollen_refresh >= pollen_due:
            self._refresh_pollen()

        panels = self._panels()
        if not panels:
            canvas.text_centered(6, "AIR QUALITY", LOADING, SMALL)
            canvas.text_centered(18, "LOADING", LOADING, SMALL)
            return

        ticks_per_slide = max(1, int(SLIDE_SECONDS * self.config.fps))
        slide = panels[(tick // ticks_per_slide) % len(panels)]

        if slide == "aqi":
            self._render_aqi(canvas)
        elif slide == "uv":
            self._render_uv(canvas)
        elif slide == "pollen":
            self._render_pollen(canvas)

    def _render_aqi(self, canvas: Canvas) -> None:
        assert self.reading is not None
        reading = self.reading
        category, color = classify_aqi(reading.aqi)

        value_text = str(reading.aqi)
        value_font = HUGE if len(value_text) <= 2 else LARGE
        value_y = 4 if value_font == HUGE else 9
        value_x = max(0, (HERO_WIDTH - canvas.text_bold_width(value_text, value_font)) // 2)
        canvas.text_bold(value_x, value_y, value_text, color, value_font)

        column_x = COLUMN_X
        column = canvas.width - column_x - RIGHT_INSET

        label = canvas.fit("AQI", column, SMALL)
        canvas.text(column_x, 1, label, DIM, SMALL)
        name_x = column_x + canvas.text_width("AQI ", SMALL)
        canvas.text(name_x, 1, canvas.fit(category, canvas.width - name_x - RIGHT_INSET, SMALL), color, SMALL)

        if reading.pm2_5 is not None:
            detail = f"PM2.5 {reading.pm2_5:.1f}"
        else:
            detail = "PM2.5 --"
        canvas.text(column_x, 11, canvas.fit(detail, column, SMALL), DIM, SMALL)

        if len(reading.trend) >= 2:
            fill = tuple(round(channel * 0.30) for channel in color)
            canvas.area_chart(
                column_x,
                21,
                column,
                11,
                reading.trend,
                color,
                fill,  # type: ignore[arg-type]
                baseline=GOOD_LIMIT,
                baseline_color=DIM,
            )

    def _render_uv(self, canvas: Canvas) -> None:
        assert self.uv is not None
        uv = self.uv
        category, color = classify_uv(uv.now)

        # UV is a single-digit-plus-tenth reading almost always. A whole-number
        # hero reads faster and matches the rounding on every UV app; a value
        # of 7.6 hides an entire category boundary in the ".6" that no one
        # squints at on a 32px panel. Round.
        value_text = str(int(round(uv.now)))
        value_font = HUGE
        value_y = 4
        value_x = max(0, (HERO_WIDTH - canvas.text_bold_width(value_text, value_font)) // 2)
        canvas.text_bold(value_x, value_y, value_text, color, value_font)

        column_x = COLUMN_X
        column = canvas.width - column_x - RIGHT_INSET

        label = "UV"
        canvas.text(column_x, 1, label, DIM, SMALL)
        name_x = column_x + canvas.text_width("UV ", SMALL)
        canvas.text(name_x, 1, canvas.fit(category, canvas.width - name_x - RIGHT_INSET, SMALL), color, SMALL)

        # Peak line: today's max in the past 24h so a morning glance shows how
        # the number is trending toward the day's ceiling, not just where it
        # sits right now.
        peak = max(uv.trend) if uv.trend else uv.now
        detail = f"PEAK {peak:.0f}"
        canvas.text(column_x, 11, canvas.fit(detail, column, SMALL), DIM, SMALL)

        if len(uv.trend) >= 2:
            fill = tuple(round(channel * 0.30) for channel in color)
            canvas.area_chart(
                column_x,
                21,
                column,
                11,
                uv.trend,
                color,
                fill,  # type: ignore[arg-type]
                # 3 is the WHO's "protection needed" threshold - the same
                # meaning the AQI's 50 baseline has, one line away from
                # "fine" into "act".
                baseline=3.0,
                baseline_color=DIM,
            )

    def _render_pollen(self, canvas: Canvas) -> None:
        assert self.pollen is not None
        pollen = self.pollen
        upi = pollen.overall()
        category, color = classify_pollen(upi)

        value_text = str(upi)
        value_font = HUGE
        value_y = 4
        value_x = max(0, (HERO_WIDTH - canvas.text_bold_width(value_text, value_font)) // 2)
        canvas.text_bold(value_x, value_y, value_text, color, value_font)

        column_x = COLUMN_X
        column = canvas.width - column_x - RIGHT_INSET

        canvas.text(column_x, 1, "POLLEN", DIM, SMALL)
        name_x = column_x + canvas.text_width("POLLEN ", SMALL)
        # Category won't fit next to "POLLEN" for the long band names on this
        # column, so drop the category on line 2 instead.
        canvas.text(column_x, 11, canvas.fit(category, column, SMALL), color, SMALL)

        # Per-type numbers on the third line, in the plant order Google reports
        # (tree, grass, weed). "--" for any species the API didn't return.
        def _cell(value: int | None) -> str:
            return "-" if value is None else str(value)

        detail = f"T{_cell(pollen.tree)} G{_cell(pollen.grass)} W{_cell(pollen.weed)}"
        # Highlight the dominant plant by drawing its cell in the category
        # colour. Anything else stays dim so the eye lands on the driver.
        dominant = pollen.dominant()
        cells = [
            ("T", pollen.tree, "TREE"),
            ("G", pollen.grass, "GRASS"),
            ("W", pollen.weed, "WEED"),
        ]
        cx = column_x
        for i, (prefix, value, plant_name) in enumerate(cells):
            token = f"{prefix}{_cell(value)}"
            if i > 0:
                canvas.text(cx, 22, " ", DIM, SMALL)
                cx += canvas.text_width(" ", SMALL)
            cell_color = color if plant_name == dominant else DIM
            canvas.text(cx, 22, token, cell_color, SMALL)
            cx += canvas.text_width(token, SMALL)


def _extract_pollen(payload: dict) -> PollenReading:
    """Pull today's UPIs out of a Google Pollen forecast response.

    The response is `dailyInfo` -> `[{ pollenTypeInfo: [...] }]`, one entry
    per day. We asked for one day so we take the first; the type codes are
    TREE / GRASS / WEED. Species-level (`plantInfo`) is finer-grained but
    the panel only has room for the top-level index.
    """
    daily = (payload.get("dailyInfo") or [None])[0]
    if not daily:
        raise ValueError("pollen response had no dailyInfo")
    values: dict[str, int | None] = {"TREE": None, "GRASS": None, "WEED": None}
    for entry in daily.get("pollenTypeInfo") or []:
        code = str(entry.get("code", "")).upper()
        if code not in values:
            continue
        index_info = entry.get("indexInfo") or {}
        raw = index_info.get("value")
        if raw is None:
            continue
        try:
            values[code] = int(raw)
        except (TypeError, ValueError):
            continue
    return PollenReading(tree=values["TREE"], grass=values["GRASS"], weed=values["WEED"])
