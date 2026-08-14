# MIT License — Copyright (c) 2026 John Kuok
"""Mode registry used by the renderer."""

from ticker.config import DEFAULT_MODE, Config
from ticker.modes.airquality import AirQualityMode
from ticker.modes.bart import BartMode
from ticker.modes.base import Mode
from ticker.modes.crypto import CryptoMode
from ticker.modes.flights import FlightsMode
from ticker.modes.market import MarketMode
from ticker.modes.network import NetworkMode
from ticker.modes.news import NewsMode
from ticker.modes.stocks import StocksMode
from ticker.modes.weather import WeatherMode

MODE_TYPES: dict[str, type[Mode]] = {
    "stocks": StocksMode,
    "news": NewsMode,
    "weather": WeatherMode,
    "flights": FlightsMode,
    "market": MarketMode,
    "crypto": CryptoMode,
    "bart": BartMode,
    "aqi": AirQualityMode,
    "net": NetworkMode,
}


def build_mode(name: str, config: Config) -> Mode:
    """Instantiate a registered mode, falling back to the default on a bad name."""
    return MODE_TYPES.get(name, MODE_TYPES[DEFAULT_MODE])(config)


__all__ = [
    "Mode",
    "MODE_TYPES",
    "build_mode",
    "StocksMode",
    "NewsMode",
    "WeatherMode",
    "FlightsMode",
    "MarketMode",
    "CryptoMode",
    "BartMode",
    "AirQualityMode",
    "NetworkMode",
]
