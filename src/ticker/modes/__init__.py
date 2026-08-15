# MIT License — Copyright (c) 2026 John Kuok
"""Mode registry used by the renderer."""

from ticker.config import DEFAULT_MODE, Config
from ticker.modes.airquality import AirQualityMode
from ticker.modes.bart import BartMode
from ticker.modes.base import Mode
from ticker.modes.bikes import BikesMode
from ticker.modes.crypto import CryptoMode
from ticker.modes.flights import FlightsMode
from ticker.modes.focus import FocusMode
from ticker.modes.market import MarketMode
from ticker.modes.nametag import NametagMode
from ticker.modes.network import NetworkMode
from ticker.modes.news import NewsMode
from ticker.modes.pokemon import PokemonMode
from ticker.modes.spotify import SpotifyMode
from ticker.modes.stocks import StocksMode
from ticker.modes.weather import WeatherMode
from ticker.modes.worldclock import WorldClockMode
from ticker.modes.youtube import YouTubeMode

MODE_TYPES: dict[str, type[Mode]] = {
    "stocks": StocksMode,
    "news": NewsMode,
    "weather": WeatherMode,
    "flights": FlightsMode,
    "market": MarketMode,
    "crypto": CryptoMode,
    "bart": BartMode,
    "aqi": AirQualityMode,
    "bikes": BikesMode,
    "nametag": NametagMode,
    "spotify": SpotifyMode,
    "pokemon": PokemonMode,
    "focus": FocusMode,
    "net": NetworkMode,
    "worldclock": WorldClockMode,
    "youtube": YouTubeMode,
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
    "BikesMode",
    "NametagMode",
    "NetworkMode",
    "PokemonMode",
    "FocusMode",
    "SpotifyMode",
    "WorldClockMode",
    "YouTubeMode",
]
