# MIT License — Copyright (c) 2026 John Kuok
"""Mode registry used by the renderer."""

from ticker.config import DEFAULT_MODE, Config
from ticker.modes.airquality import AirQualityMode
from ticker.modes.bart import BartMode
from ticker.modes.base import Mode
from ticker.modes.bikes import BikesMode
from ticker.modes.commute import CommuteMode
from ticker.modes.costco import CostcoMode
from ticker.modes.crypto import CryptoMode
from ticker.modes.currency import CurrencyMode
from ticker.modes.earthquakes import EarthquakesMode
from ticker.modes.flights import FlightsMode
from ticker.modes.focus import FocusMode
from ticker.modes.market import MarketMode
from ticker.modes.mlb import MLBMode
from ticker.modes.muni import MuniMode
from ticker.modes.nametag import NametagMode
from ticker.modes.nba import NBAMode
from ticker.modes.network import NetworkMode
from ticker.modes.news import NewsMode
from ticker.modes.nfl import NFLMode
from ticker.modes.nhl import NHLMode
from ticker.modes.pihealth import PiHealthMode
from ticker.modes.pixeltest import PixelTestMode
from ticker.modes.pokemon import PokemonMode
from ticker.modes.spotify import SpotifyMode
from ticker.modes.sports import SportsMode
from ticker.modes.stocks import StocksMode
from ticker.modes.vibes import VibesMode
from ticker.modes.weather import WeatherMode
from ticker.modes.worldclock import WorldClockMode
from ticker.modes.youtube import YouTubeMode

MODE_TYPES: dict[str, type[Mode]] = {
    "stocks": StocksMode,
    "news": NewsMode,
    "weather": WeatherMode,
    "flights": FlightsMode,
    "market": MarketMode,
    "costco": CostcoMode,
    "commute": CommuteMode,
    "crypto": CryptoMode,
    "currency": CurrencyMode,
    "quakes": EarthquakesMode,
    "bart": BartMode,
    "muni": MuniMode,
    "aqi": AirQualityMode,
    "bikes": BikesMode,
    "nametag": NametagMode,
    "spotify": SpotifyMode,
    "pokemon": PokemonMode,
    "focus": FocusMode,
    "net": NetworkMode,
    "pihealth": PiHealthMode,
    "pixeltest": PixelTestMode,
    "worldclock": WorldClockMode,
    "youtube": YouTubeMode,
    "vibes": VibesMode,
    "sports": SportsMode,
    # Per-league constructors aren't separate top-level mode slots (the
    # "sports" umbrella above is the only one a user picks from the
    # rotation/settings UI) -- kept here so tests and any future direct
    # construction don't need a private import path.
    "mlb": MLBMode,
    "nhl": NHLMode,
    "nfl": NFLMode,
    "nba": NBAMode,
}


def build_mode(name: str, config: Config, *, alert_source=None) -> Mode:  # type: ignore[no-untyped-def]
    """Instantiate a registered mode, falling back to the default on a bad name.

    ``alert_source`` is a callable ``() -> QuakeAlert | None`` that the quakes
    mode uses to render an active auto-alert instead of the passive M4.5+ 24h
    rotation. It's the only per-mode capability we thread through the builder
    right now; adding more should follow the same pattern (typed kwarg,
    forwarded only to the modes that accept it).
    """
    cls = MODE_TYPES.get(name, MODE_TYPES[DEFAULT_MODE])
    if cls is EarthquakesMode and alert_source is not None:
        return cls(config, alert_source=alert_source)
    return cls(config)


__all__ = [
    "Mode",
    "MODE_TYPES",
    "build_mode",
    "StocksMode",
    "NewsMode",
    "WeatherMode",
    "FlightsMode",
    "MarketMode",
    "CommuteMode",
    "CryptoMode",
    "CurrencyMode",
    "EarthquakesMode",
    "BartMode",
    "MuniMode",
    "AirQualityMode",
    "BikesMode",
    "NametagMode",
    "NetworkMode",
    "PiHealthMode",
    "PixelTestMode",
    "PokemonMode",
    "FocusMode",
    "SpotifyMode",
    "SportsMode",
    "MLBMode",
    "NHLMode",
    "NFLMode",
    "NBAMode",
    "VibesMode",
    "WorldClockMode",
    "YouTubeMode",
]
