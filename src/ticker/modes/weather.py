# MIT License — Copyright (c) 2026 John Kuok
"""US National Weather Service forecast mode."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from ticker.canvas import Canvas
from ticker.modes.base import Mode


@dataclass(slots=True)
class Forecast:
    temperature: int
    unit: str
    condition: str
    high: int
    low: int
    wind: str


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
        if not self.config.weather_lat or not self.config.weather_lon:
            canvas.text(2, 11, "Set WEATHER_LAT/LON", (255, 180, 60), 8)
            return
        if not self.forecast:
            canvas.text(20, 11, "Loading weather...", (130, 180, 255), 8)
            return
        forecast = self.forecast
        canvas.text(1, -3, f"{forecast.temperature}°", (255, 210, 50), 18)
        canvas.text(48, 1, forecast.condition[:18], (200, 220, 255), 7)
        canvas.text(48, 11, f"H {forecast.high}°  L {forecast.low}°", (255, 140, 100), 8)
        canvas.text(1, 23, f"Wind {forecast.wind}", (120, 190, 255), 8)
