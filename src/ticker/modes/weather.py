# MIT License — Copyright (c) 2026 John Kuok
"""US National Weather Service forecast mode with a wall clock."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from ticker.canvas import HUGE, SMALL, Canvas
from ticker.modes.base import Mode


@dataclass(slots=True)
class Forecast:
    temperature: int
    unit: str
    condition: str
    high: int
    low: int
    wind: str


def _shorten_wind(wind: str) -> str:
    """Turn NWS wording like '12 to 16 mph' into a panel-sized '12-16mph'."""
    return wind.replace(" to ", "-").replace(" mph", "mph").strip()


class WeatherMode(Mode):
    """Fetch an NWS grid forecast through the documented points endpoint."""

    CACHE_SECONDS = 600

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.forecast: Forecast | None = None
        self._last_refresh = 0.0

    def _refresh(self) -> None:
        if not self.config.weather_lat or not self.config.weather_lon:
            self._last_refresh = time.monotonic()
            return
        try:
            headers = {"User-Agent": self.config.weather_user_agent, "Accept": "application/geo+json"}
            points = requests.get(
                f"https://api.weather.gov/points/{self.config.weather_lat},{self.config.weather_lon}",
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
            )
        except Exception:
            pass
        finally:
            self._last_refresh = time.monotonic()

    def render(self, canvas: Canvas, tick: int) -> None:
        if time.monotonic() - self._last_refresh >= self.CACHE_SECONDS:
            self._refresh()
        canvas.clear()

        clock = self.config.clock_text()

        if not self.config.weather_lat or not self.config.weather_lon:
            canvas.text_centered(6, clock, (255, 210, 50), SMALL)
            canvas.text_centered(18, "SET WEATHER LAT/LON", (255, 150, 60), SMALL)
            return

        if not self.forecast:
            canvas.text_centered(6, clock, (255, 210, 50), SMALL)
            canvas.text_centered(18, "LOADING WEATHER", (130, 180, 255), SMALL)
            return

        forecast = self.forecast

        # Left third: the hero temperature, vertically centred.
        temp_text = f"{forecast.temperature}"
        canvas.text(0, 3, temp_text, (255, 205, 40), HUGE)
        temp_width = canvas.text_width(temp_text, HUGE)
        canvas.degree(temp_width + 1, 4, (255, 205, 40))

        # Right two thirds: three stacked 8px rows — clock, sky, then the numbers.
        right_x = temp_width + 8
        column = canvas.width - right_x
        canvas.text(right_x, 2, canvas.fit(clock, column), (235, 240, 250), SMALL)
        canvas.text(right_x, 12, canvas.fit(forecast.condition.upper(), column), (170, 205, 255), SMALL)
        detail = f"H{forecast.high} L{forecast.low} {_shorten_wind(forecast.wind)}".upper()
        canvas.text(right_x, 22, canvas.fit(detail, column), (255, 145, 105), SMALL)
