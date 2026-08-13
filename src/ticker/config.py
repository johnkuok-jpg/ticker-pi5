# MIT License — Copyright (c) 2026 John Kuok
"""Configuration and small file-backed control state for ticker-pi5."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_MODES = ("stocks", "news", "weather", "spotify")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _first_writable_state_dir() -> Path:
    """Return the persistent state directory, preferring /var/lib/ticker."""
    for candidate in (Path("/var/lib/ticker"), Path.home() / ".ticker"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise RuntimeError("No writable ticker state directory is available")


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime settings read from the repository's .env file."""

    width: int = 128
    height: int = 32
    addr_lines: int = 4
    brightness: float = 0.35
    fps: int = 30
    symbols: tuple[str, ...] = ("AAPL", "NVDA", "SPY", "BTC-USD")
    news_feed_url: str = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"
    news_source_name: str = "CNBC MARKETS"
    weather_lat: str = ""
    weather_lon: str = ""
    weather_user_agent: str = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"
    raspotify_log_path: Path = Path("/var/log/raspotify/raspotify.log")
    state_dir: Path = Path.home() / ".ticker"

    @property
    def mode_file(self) -> Path:
        return self.state_dir / "current_mode"

    @property
    def brightness_file(self) -> Path:
        return self.state_dir / "brightness"

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "renderer.pid"

    @property
    def logos_dir(self) -> Path:
        return PROJECT_ROOT / "src" / "ticker" / "web" / "static" / "logos"

    def current_mode(self) -> str:
        """Read the requested mode; create a safe default when absent/corrupt."""
        try:
            value = self.mode_file.read_text(encoding="utf-8").strip().lower()
        except OSError:
            value = ""
        if value not in VALID_MODES:
            self.set_mode("stocks")
            return "stocks"
        return value

    def set_mode(self, mode: str) -> None:
        """Persist a validated mode atomically enough for this single-file protocol."""
        mode = mode.lower()
        if mode not in VALID_MODES:
            raise ValueError(f"Unknown mode: {mode}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mode_file.write_text(f"{mode}\n", encoding="utf-8")

    def current_brightness(self) -> float:
        """Return an optionally web-adjusted brightness clamped to 0.05..1.0."""
        try:
            value = float(self.brightness_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            value = self.brightness
        return max(0.05, min(1.0, value))

    def set_brightness(self, brightness: float) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        value = max(0.05, min(1.0, float(brightness)))
        self.brightness_file.write_text(f"{value:.2f}\n", encoding="utf-8")


def load_config(env_file: Path | None = None) -> Config:
    """Load .env once and construct a typed configuration object."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=env_file is not None)
    symbols = tuple(
        symbol.strip().upper()
        for symbol in os.getenv("TICKER_SYMBOLS", "AAPL,NVDA,SPY,BTC-USD").split(",")
        if symbol.strip()
    )
    return Config(
        width=int(os.getenv("TICKER_WIDTH", "128")),
        height=int(os.getenv("TICKER_HEIGHT", "32")),
        addr_lines=int(os.getenv("TICKER_ADDR_LINES", "4")),
        brightness=float(os.getenv("TICKER_BRIGHTNESS", "0.35")),
        fps=max(1, int(os.getenv("TICKER_FPS", "30"))),
        symbols=symbols,
        news_feed_url=os.getenv("NEWS_FEED_URL", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
        news_source_name=os.getenv("NEWS_SOURCE_NAME", "CNBC MARKETS"),
        weather_lat=os.getenv("WEATHER_LAT", ""),
        weather_lon=os.getenv("WEATHER_LON", ""),
        weather_user_agent=os.getenv(
            "WEATHER_USER_AGENT", "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"
        ),
        raspotify_log_path=Path(os.getenv("RASPOTIFY_LOG_PATH", "/var/log/raspotify/raspotify.log")),
        state_dir=_first_writable_state_dir(),
    )
