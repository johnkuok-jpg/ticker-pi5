# MIT License — Copyright (c) 2026 John Kuok
"""US National Weather Service forecast mode with a wall clock."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from ticker import icons
from ticker.canvas import HUGE, LARGE, SMALL, Canvas
from ticker.modes.base import Mode

ICON_SIZE = 12


@dataclass(slots=True)
class Forecast:
    temperature: int
    unit: str
    condition: str
    high: int
    low: int
    wind: str
    is_daytime: bool = True


def _shorten_wind(wind: str) -> str:
    """Turn NWS wording like '12 to 16 mph' into a panel-sized '12-16mph'."""
    return wind.replace(" to ", "-").replace(" mph", "mph").strip()


# NWS shortForecast strings are written for a webpage, not a 16-character
# column: "Chance Rain Showers" and "Scattered Thunderstorms" both overflow and
# get chopped mid-word. The probability qualifier is already implied by the
# glyph, so drop it and abbreviate the longest weather nouns.
_QUALIFIERS = (
    "slight chance",
    "chance",
    "likely",
    "isolated",
    "scattered",
    "numerous",
    "widespread",
    "areas of",
    "patchy",
    "periods of",
)

_ABBREVIATIONS = (
    ("thunderstorms", "TSTORMS"),
    ("thunderstorm", "TSTORM"),
    ("precipitation", "PRECIP"),
    ("partly", "PT"),
    ("mostly", "MST"),
)


def _shorten_condition(condition: str, limit: int) -> str:
    """Compress an NWS forecast phrase until it fits *limit* characters."""
    text = (condition or "").lower().strip()
    # "Sunny then Chance Rain Showers" - only the current half matters.
    text = text.split(" then ")[0]
    for qualifier in _QUALIFIERS:
        text = text.replace(qualifier, " ")
    text = " ".join(text.split()).upper()
    if len(text) <= limit:
        return text
    for long_form, short_form in _ABBREVIATIONS:
        text = text.replace(long_form.upper(), short_form)
        if len(text) <= limit:
            break
    return text


class WeatherMode(Mode):
    """Fetch an NWS grid forecast through the documented points endpoint."""

    CACHE_SECONDS = 600

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.forecast: Forecast | None = None
        self._last_refresh = 0.0

    def _refresh(self) -> None:
        lat, lon = self.config.current_weather_coords()
        if not lat or not lon:
            self._last_refresh = time.monotonic()
            return
        try:
            headers = {"User-Agent": self.config.weather_user_agent, "Accept": "application/geo+json"}
            points = requests.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers=headers,
                timeout=10,
            )
            points.raise_for_status()
            forecast_url = points.json()["properties"]["forecast"]
            forecast_response = requests.get(forecast_url, headers=headers, timeout=10)
            forecast_response.raise_for_status()
            periods = forecast_response.json()["properties"]["periods"]
            current = periods[0]
            daytime = next((p for p in periods if p.get("isDaytime")), current)
            nighttime = next((p for p in periods if not p.get("isDaytime")), current)
            self.forecast = Forecast(
                temperature=int(current["temperature"]),
                unit=str(current.get("temperatureUnit", "F")),
                condition=str(current.get("shortForecast", "")),
                high=int(daytime["temperature"]),
                low=int(nighttime["temperature"]),
                wind=str(current.get("windSpeed", "? mph")),
                is_daytime=bool(current.get("isDaytime", True)),
            )
        except Exception:
            pass
        finally:
            self._last_refresh = time.monotonic()

    def render(self, canvas: Canvas, tick: int) -> None:
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()
        canvas.clear()

        clock = self.clock_text(tick)

        lat, lon = self.config.current_weather_coords()
        if not lat or not lon:
            canvas.text_centered(6, clock, (255, 210, 50), SMALL)
            canvas.text_centered(18, "SET WEATHER ZIP", (255, 150, 60), SMALL)
            return

        if not self.forecast:
            canvas.text_centered(6, clock, (255, 210, 50), SMALL)
            canvas.text_centered(18, "LOADING WEATHER", (130, 180, 255), SMALL)
            return

        forecast = self.forecast
        amber = (255, 205, 40)

        # Hero temperature, drawn bold. Three-digit readings (or a minus sign)
        # step down a size so the columns to its right keep their room.
        temp_text = f"{forecast.temperature}"
        temp_font = HUGE if len(temp_text) <= 2 else LARGE
        temp_y = 3 if temp_font == HUGE else 8
        canvas.text_bold(0, temp_y, temp_text, amber, temp_font)
        temp_width = canvas.text_bold_width(temp_text, temp_font)
        canvas.degree(temp_width + 1, temp_y + 1, amber)

        # Condition glyph, vertically centred between temperature and text.
        icon_x = temp_width + 5
        canvas.sprite(icon_x, (canvas.height - ICON_SIZE) // 2,
                      icons.for_condition(forecast.condition, forecast.is_daytime), icons.PALETTE)

        # Three stacked 8px rows: clock, sky, then the numbers.
        right_x = icon_x + ICON_SIZE + 4
        column = canvas.width - right_x
        canvas.text(right_x, 2, canvas.fit(clock, column), (235, 240, 250), SMALL)
        condition = _shorten_condition(forecast.condition, canvas.max_chars_in(column))
        canvas.text(right_x, 12, canvas.fit(condition, column), (170, 205, 255), SMALL)
        detail = f"H{forecast.high} L{forecast.low} {_shorten_wind(forecast.wind)}".upper()
        canvas.text(right_x, 22, canvas.fit(detail, column), (255, 145, 105), SMALL)
