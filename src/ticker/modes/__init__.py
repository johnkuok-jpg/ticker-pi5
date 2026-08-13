# MIT License — Copyright (c) 2026 John Kuok
"""Mode registry used by the renderer."""

from ticker.config import Config
from ticker.modes.base import Mode
from ticker.modes.news import NewsMode
from ticker.modes.spotify import SpotifyMode
from ticker.modes.stocks import StocksMode
from ticker.modes.weather import WeatherMode

MODE_TYPES: dict[str, type[Mode]] = {
    "stocks": StocksMode,
    "news": NewsMode,
    "weather": WeatherMode,
    "spotify": SpotifyMode,
}


def build_mode(name: str, config: Config) -> Mode:
    """Instantiate a registered mode, with stocks as the defensive fallback."""
    return MODE_TYPES.get(name, StocksMode)(config)


__all__ = ["Mode", "MODE_TYPES", "build_mode", "StocksMode", "NewsMode", "WeatherMode", "SpotifyMode"]
