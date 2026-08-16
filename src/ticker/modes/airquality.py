# MIT License — Copyright (c) 2026 John Kuok
"""US air quality index for the weather location.

Open-Meteo's air-quality endpoint needs no key and computes the US EPA index
directly, so the panel does not have to derive an AQI from raw concentrations -
that conversion has pollutant-specific breakpoints and a "worst pollutant wins"
rule that is easy to get subtly wrong.

The same request returns hourly values, which the trailing 24-hour chart uses.
Its baseline is drawn at 50, the Good/Moderate boundary, so a flat clean day
still shows how much headroom it has rather than an anonymous straight line.

Categories, names and colours are the EPA's own, from the AQI Technical
Assistance Document (May 2026):
https://document.airnow.gov/technical-assistance-document-for-the-reporting-of-daily-air-quailty.pdf
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

API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
REQUEST_TIMEOUT = 8.0
USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

WHITE = (235, 240, 250)
DIM = (108, 122, 148)
ERROR = (255, 150, 60)

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

# The official palette, in the EPA's standard RGB values.
CATEGORIES: tuple[tuple[int, str, tuple[int, int, int]], ...] = (
    (50, "GOOD", (0, 228, 0)),
    (100, "MODERATE", (255, 255, 0)),
    # "Unhealthy for Sensitive Groups" is 30 characters and will not fit beside
    # a three-digit reading at any font this panel has.
    (150, "SENSITIVE", (255, 126, 0)),
    (200, "UNHEALTHY", (255, 0, 0)),
    (300, "V UNHEALTHY", (143, 63, 151)),
    (10**6, "HAZARDOUS", (126, 0, 35)),
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


def classify(aqi: int) -> tuple[str, tuple[int, int, int]]:
    """Category name and panel-safe colour for an AQI value."""
    for limit, name, color in CATEGORIES:
        if aqi <= limit:
            return name, panel_color(color)
    name, color = CATEGORIES[-1][1], CATEGORIES[-1][2]
    return name, panel_color(color)


@dataclass(frozen=True)
class AirQuality:
    aqi: int
    pm2_5: float | None
    trend: tuple[float, ...]


class AirQualityMode(Mode):
    """Fetch and draw the current US AQI with a trailing 24-hour trend."""

    # The index is published hourly, so polling faster only spends someone
    # else's bandwidth to redraw the same number.
    CACHE_SECONDS = 900
    ERROR_BACKOFF_SECONDS = 120

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.reading: AirQuality | None = None
        self._last_refresh = -1e9
        self._failed = False

    def _fetch(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.read()

    def _refresh(self) -> None:
        lat, lon = self.config.current_weather_coords()
        query = urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "current": "us_aqi,pm2_5",
                "hourly": "us_aqi",
                "past_days": 1,
                "forecast_days": 1,
                "timezone": self.config.timezone or "auto",
            }
        )
        try:
            payload = json.loads(self._fetch(f"{API_URL}?{query}").decode("utf-8", "replace"))
            current = payload["current"]
            aqi = current.get("us_aqi")
            if aqi is None:
                raise ValueError("no us_aqi in response")
            pm2_5 = current.get("pm2_5")
            self.reading = AirQuality(
                aqi=int(round(float(aqi))),
                pm2_5=float(pm2_5) if pm2_5 is not None else None,
                trend=self._trend(payload, str(current.get("time", ""))),
            )
            self._failed = False
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
            LOGGER.debug("air quality refresh failed: %s", error)
            self._failed = True
        finally:
            self._last_refresh = time.monotonic()

    def _trend(self, payload: dict, current_time: str) -> tuple[float, ...]:
        """The 24 hours ending now.

        The response also carries forecast hours, which must not be charted as
        history: the request spans a past day and a forecast day, so the series
        is cut at the hour the current reading belongs to.
        """
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        values = hourly.get("us_aqi") or []
        if not values:
            return ()
        try:
            end = times.index(current_time)
        except ValueError:
            end = len(values) - 1
        window = values[max(0, end - TREND_HOURS + 1) : end + 1]
        return tuple(float(value) for value in window if value is not None)

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()

        lat, lon = self.config.current_weather_coords()
        if not lat or not lon:
            canvas.text_centered(6, "AIR QUALITY", ERROR, SMALL)
            canvas.text_centered(18, "SET WEATHER ZIP", ERROR, SMALL)
            return

        due = self.CACHE_SECONDS if not self._failed else self.ERROR_BACKOFF_SECONDS
        if time.monotonic() - self._last_refresh >= due:
            self._refresh()

        if self.reading is None:
            canvas.text_centered(6, "AIR QUALITY", (130, 180, 255), SMALL)
            canvas.text_centered(18, "LOADING", (130, 180, 255), SMALL)
            return

        reading = self.reading
        category, color = classify(reading.aqi)

        # Hero number, sized so three digits still leave the right column room.
        value_text = str(reading.aqi)
        value_font = HUGE if len(value_text) <= 2 else LARGE
        value_y = 4 if value_font == HUGE else 9
        # Centre a single digit in the hero column rather than letting it hug the
        # bezel with a hole beside it.
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
