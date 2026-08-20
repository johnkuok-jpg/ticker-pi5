# MIT License — Copyright (c) 2026 John Kuok
"""US air quality, UV index, and pollen for the weather location.

All three metrics live on one static panel split into three columns so a
glance shows every reading and its category at once. Rotation was tried in
an earlier revision and lost - a user watching for "is it safe to run right
now" doesn't want to wait six seconds for the UV slide to come back.

- AQI + UV both come from Open-Meteo's air-quality endpoint (one call, no key).
  Open-Meteo returns the US EPA index directly and hourly, so the panel doesn't
  have to derive an AQI from raw concentrations - that conversion has
  pollutant-specific breakpoints and a "worst pollutant wins" rule that's easy
  to get subtly wrong.
- Pollen for the US comes from Google's Pollen API. Open-Meteo carries pollen
  too, but only for Europe (CAMS coverage). Google's key is the same
  GOOGLE_MAPS_API_KEY the commute mode already uses -- just enable Pollen API
  on the same Cloud project. If the key is missing, the pollen column collapses
  to "--" instead of showing an error the user can't fix.

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

# The rightmost column sits against the panel's bezel, where a glyph edge is
# easy to lose. Every mode leaves it dark.
RIGHT_INSET = 1

# 128px canvas / 3 cells = 42.67, so cells are 43 / 43 / 42 and share a hairline
# separator that sits INSIDE the right-hand cell. Fixed widths mean AQI 88 and
# UV 6 don't jitter sideways when one climbs to three digits.
CELL_WIDTH = 128 // 3  # 42
CELL_1_X = 0
CELL_2_X = CELL_WIDTH
CELL_3_X = CELL_WIDTH * 2

# Kept as a module constant so external callers that historically read the
# rotation cadence keep working. Not used inside the module anymore.
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
        """Which metrics have data right now.

        The panel is static (three cells shown at once) but the list still
        drives the loading state - if none are ready we show LOADING, and
        callers/tests can introspect what data has come back.
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

        self._render_combined(canvas)

    def _render_combined(self, canvas: Canvas) -> None:
        """Three-cell layout: AQI | UV | POLLEN, static, always all three.

        Per cell (top to bottom):
          - dim label ("AQI" / "UV" / "POL") centred at y=1
          - LARGE value in the metric's category colour, centred at y=10
          - SMALL category name ("MODERATE" / "HIGH" / etc), category
            colour, centred at y=23, `fit()`-truncated to cell width

        A cell whose data isn't loaded yet still draws label + "--" so the
        column doesn't collapse when one API is slow.
        """
        # Faint separators between cells - not a full divider, just enough to
        # cue the eye that these are three distinct readings, not one row of
        # numbers.
        for x in (CELL_2_X - 1, CELL_3_X - 1):
            for y in range(3, 29):
                canvas.pixel(x, y, (30, 34, 46))

        # AQI cell (left)
        if self.reading is not None:
            aqi_cat, aqi_col = classify_aqi(self.reading.aqi)
            self._draw_cell(canvas, CELL_1_X, CELL_WIDTH - 1, "AQI", str(self.reading.aqi), aqi_col, aqi_cat)
        else:
            self._draw_cell(canvas, CELL_1_X, CELL_WIDTH - 1, "AQI", "--", DIM, "...")

        # UV cell (middle). UV is almost always a single-digit-plus-tenth;
        # round to a whole number - matches every UV app and doesn't force
        # the .5 to hide behind a decimal at this pixel size. Classify the
        # ROUNDED value, not the raw float, so "2 MODERATE" (which is the
        # yellow band's start) never renders next to a UV that would round
        # to 2 - the number and its category must agree at a glance.
        if self.uv is not None:
            uv_display = int(round(self.uv.now))
            uv_cat, uv_col = classify_uv(float(uv_display))
            self._draw_cell(canvas, CELL_2_X, CELL_WIDTH - 1, "UV", str(uv_display), uv_col, uv_cat)
        else:
            self._draw_cell(canvas, CELL_2_X, CELL_WIDTH - 1, "UV", "--", DIM, "...")

        # Pollen cell (right). Overall UPI (max of tree/grass/weed) - the same
        # "worst wins" logic AQI uses for pollutants. Right cell has one fewer
        # pixel of usable width because of RIGHT_INSET.
        if self.pollen is not None:
            upi = self.pollen.overall()
            pol_cat, pol_col = classify_pollen(upi)
            self._draw_cell(canvas, CELL_3_X, CELL_WIDTH - RIGHT_INSET, "POL", str(upi), pol_col, pol_cat)
        else:
            # No pollen data means either the API key is missing or the
            # request is still in-flight. Either way "--" is honest.
            self._draw_cell(canvas, CELL_3_X, CELL_WIDTH - RIGHT_INSET, "POL", "--", DIM, "...")

    def _draw_cell(
        self,
        canvas: Canvas,
        x0: int,
        width: int,
        label: str,
        value: str,
        color: tuple[int, int, int],
        category: str,
    ) -> None:
        """Paint one AQI/UV/POL cell inside its horizontal slice.

        Centres every element to `width` so a 1- vs 2-digit value doesn't
        push the layout around. Category is `fit()`-truncated because
        "UNHEALTHY" is 9 characters and the cell only holds 8 at SMALL.
        """
        # Label
        lw = canvas.text_width(label, SMALL)
        canvas.text(x0 + max(0, (width - lw) // 2), 1, label, DIM, SMALL)
        # Big value in category colour
        vw = canvas.text_bold_width(value, LARGE)
        canvas.text_bold(x0 + max(0, (width - vw) // 2), 10, value, color, LARGE)
        # Category, truncated to cell width
        cat_fit = canvas.fit(category, width, SMALL) if category else ""
        cw = canvas.text_width(cat_fit, SMALL)
        canvas.text(x0 + max(0, (width - cw) // 2), 23, cat_fit, color, SMALL)


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
