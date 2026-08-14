# MIT License — Copyright (c) 2026 John Kuok
"""Configuration and small file-backed control state for ticker-pi5."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_MODES = ("stocks", "news", "weather", "flights", "market", "crypto", "bart", "aqi", "bikes", "nametag", "spotify", "net")

# Text color for the nametag mode when the wearer hasn't picked one yet.
_DEFAULT_NAMETAG_HEX = "#FFFFFF"


def _parse_hex_color(value: str) -> tuple[int, int, int]:
    """'#RRGGBB' or 'RRGGBB' -> (R, G, B). Any parse error falls back to white."""
    if not value:
        return (255, 255, 255)
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)  # '#FA0' -> 'FFAA00'
    if len(text) != 6:
        return (255, 255, 255)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (255, 255, 255)


def _canonical_hex_color(value: str) -> str:
    """Validate + normalize a color to '#RRGGBB'. Raises ValueError on garbage."""
    if not isinstance(value, str):
        raise ValueError("nametag color must be a string")
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError("nametag color must be a 3- or 6-digit hex string")
    try:
        int(text, 16)
    except ValueError as exc:
        raise ValueError("nametag color is not valid hex") from exc
    return f"#{text.upper()}"

# What the panel shows on a cold boot, or if the mode file is missing or corrupt.
DEFAULT_MODE = "weather"

_DAY_NUMBERS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_GROUPS = {
    "all": frozenset(range(7)),
    "daily": frozenset(range(7)),
    "weekday": frozenset(range(5)),
    "weekdays": frozenset(range(5)),
    "weekend": frozenset({5, 6}),
    "weekends": frozenset({5, 6}),
}


@dataclass(frozen=True)
class BrightnessStep:
    """A brightness level that takes effect at a wall-clock time on given days."""

    days: frozenset[int]  # 0 = Monday, matching datetime.weekday()
    minute: int  # minutes since midnight
    level: float  # 0.0 (panel dark) to 1.0


def _parse_days(token: str) -> frozenset[int]:
    """Accept 'mon', 'mon-fri', 'weekday', 'all'. Ranges may wrap, so fri-mon works."""
    token = token.strip().lower()
    if token in _DAY_GROUPS:
        return _DAY_GROUPS[token]
    if "-" in token:
        start_name, _, end_name = token.partition("-")
        start, end = _DAY_NUMBERS[start_name.strip()], _DAY_NUMBERS[end_name.strip()]
        span = (end - start) % 7
        return frozenset((start + offset) % 7 for offset in range(span + 1))
    return frozenset({_DAY_NUMBERS[token]})


def _parse_level(token: str) -> float:
    """Accept 'off', a 0-1 fraction, or a 0-100 percentage."""
    token = token.strip().lower()
    if token in {"off", "dark", "none"}:
        return 0.0
    value = float(token)
    if value > 1.0:  # written as a percentage
        value /= 100.0
    return max(0.0, min(1.0, value))


def parse_brightness_schedule(text: str) -> tuple[BrightnessStep, ...]:
    """Parse 'mon-fri 07:00=55, 22:00=off' into ordered steps.

    Entries are separated by commas, so a day list cannot itself contain one:
    write ``sat-sun`` or ``weekend`` rather than ``sat,sun``. A malformed entry
    is skipped rather than raising, because a typo in .env must not stop the
    display from coming up at all - a ticker stuck dark is worse than a ticker
    ignoring one line of its schedule.
    """
    steps: list[BrightnessStep] = []
    for raw in text.split(","):
        entry = raw.strip()
        if not entry:
            continue
        try:
            spec, _, level_token = entry.rpartition("=")
            if not spec:
                continue
            parts = spec.split()
            time_token = parts[-1]
            days = _parse_days(" ".join(parts[:-1])) if len(parts) > 1 else _DAY_GROUPS["all"]
            hour_token, _, minute_token = time_token.partition(":")
            hour, minute = int(hour_token), int(minute_token or 0)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                continue
            steps.append(BrightnessStep(days, hour * 60 + minute, _parse_level(level_token)))
        except (KeyError, ValueError, IndexError):
            continue
    return tuple(sorted(steps, key=lambda step: step.minute))


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


def _parse_channel_order(text: str) -> str:
    """Accept any permutation of "rgb", ignoring anything else.

    A typo here would otherwise raise inside the render loop on a headless box
    with no display to report it, so an unusable value falls back to the driver
    default rather than taking the ticker down.
    """
    value = "".join(text.split()).lower()
    if sorted(value) == ["b", "g", "r"]:
        return value
    return "rgb"


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime settings read from the repository's .env file."""

    width: int = 128
    height: int = 32
    addr_lines: int = 4
    # Some HUB75 panels wire the color channels in a different order than the
    # driver assumes, which shows up as swapped colors rather than as an error.
    # Any permutation of "rgb"; see scripts/check_colors.py to identify yours.
    channel_order: str = "rgb"
    brightness: float = 0.35
    brightness_schedule: tuple[BrightnessStep, ...] = ()
    fps: int = 30
    symbols: tuple[str, ...] = ("AAPL", "NVDA", "SPY", "BTC-USD")
    stocks_layout: str = "card"  # card | scroll
    news_feed_url: str = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"
    news_source_name: str = "CNBC MARKETS"
    flight_number: str = ""
    flight_airport: str = ""
    crypto_symbols: tuple[str, ...] = ("BTC", "ETH")
    bart_station: str = "EMBR"
    bike_station_id: str = ""
    nametag_name: str = ""
    nametag_color: str = "#FFFFFF"
    nametag_font: str = "spleen"
    # Spotify developer app credentials. Empty by default; when either is
    # missing, the Spotify mode renders a helpful 'set SPOTIFY_CLIENT_ID'
    # placeholder instead of blowing up on a None token exchange.
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    # Where Spotify should send the user back after they authorise. Must match
    # a redirect URI registered on the Spotify app dashboard exactly.
    spotify_redirect_uri: str = ""
    weather_lat: str = ""
    weather_lon: str = ""
    weather_user_agent: str = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"
    timezone: str = ""
    clock_24h: bool = False
    state_dir: Path = Path.home() / ".ticker"

    def now(self):  # noqa: ANN201 - datetime, kept loose to avoid a module-level import cycle
        """Current local time, honouring TICKER_TIMEZONE when it is set and valid."""
        from datetime import datetime

        if self.timezone:
            try:
                from zoneinfo import ZoneInfo

                return datetime.now(ZoneInfo(self.timezone))
            except Exception:
                pass  # fall through to the system clock
        return datetime.now()

    def clock_text(self) -> str:
        """Formatted wall clock, e.g. '1:07 PM' or '13:07'. No leading zero on 12h."""
        stamp = self.now()
        if self.clock_24h:
            return stamp.strftime("%H:%M")
        hour = stamp.hour % 12 or 12
        return f"{hour}:{stamp.strftime('%M %p')}"

    @property
    def mode_file(self) -> Path:
        return self.state_dir / "current_mode"

    @property
    def brightness_file(self) -> Path:
        return self.state_dir / "brightness"

    @property
    def flight_file(self) -> Path:
        return self.state_dir / "flight"

    @property
    def flight_airport_file(self) -> Path:
        return self.state_dir / "flight_airport"

    @property
    def bart_station_file(self) -> Path:
        return self.state_dir / "bart_station"

    @property
    def bike_station_file(self) -> Path:
        return self.state_dir / "bike_station"

    @property
    def nametag_name_file(self) -> Path:
        return self.state_dir / "nametag_name"

    @property
    def nametag_font_file(self) -> Path:
        return self.state_dir / "nametag_font"

    @property
    def nametag_color_file(self) -> Path:
        return self.state_dir / "nametag_color"

    @property
    def spotify_token_file(self) -> Path:
        """Where the refresh + access tokens land after OAuth.

        Kept inside :attr:`state_dir` next to the other mode state so a single
        ``rm -rf ~/.ticker`` truly wipes the device. Permissions are tightened
        to 0600 on write; see :meth:`SpotifyAuth._save`.
        """
        return self.state_dir / "spotify_tokens.json"

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "renderer.pid"

    @property
    def network_notice_file(self) -> Path:
        """Where the Wi-Fi fallback daemon parks a message for the panel.

        A file rather than a query: the render loop must not shell out to nmcli
        thirty times a second, and this state is only interesting when it changes.
        The daemon owns the file, and the renderer only reads it.
        """
        return self.state_dir / "network_notice"

    def network_notice(self) -> dict[str, str]:
        """Read the Wi-Fi notice, or an empty dict when there is nothing to say."""
        try:
            raw = self.network_notice_file.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def set_network_notice(self, payload: dict[str, str] | None) -> None:
        """Publish or clear the Wi-Fi notice."""
        if not payload:
            self.network_notice_file.unlink(missing_ok=True)
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.network_notice_file.write_text(json.dumps(payload), encoding="utf-8")

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
            self.set_mode(DEFAULT_MODE)
            return DEFAULT_MODE
        return value

    def set_mode(self, mode: str) -> None:
        """Persist a validated mode atomically enough for this single-file protocol."""
        mode = mode.lower()
        if mode not in VALID_MODES:
            raise ValueError(f"Unknown mode: {mode}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mode_file.write_text(f"{mode}\n", encoding="utf-8")

    def scheduled_brightness(self, now=None):  # noqa: ANN001, ANN201 - datetime kept loose
        """Active scheduled level and the moment it took effect, or None if unscheduled.

        Steps are points, not ranges: the level in force is the most recent step
        at or before now. That means the schedule always has an answer, with no
        gaps to define behaviour for, but it does require looking back past
        midnight - at 03:00 on a Saturday the level still in force may have been
        set by Friday evening's step. The search walks back a bounded eight days
        so a schedule listing only 'mon' cannot loop forever.
        """
        if not self.brightness_schedule:
            return None

        from datetime import timedelta

        now = now or self.now()
        minute_now = now.hour * 60 + now.minute

        for days_back in range(8):
            weekday = (now.weekday() - days_back) % 7
            candidates = [step for step in self.brightness_schedule if weekday in step.days]
            if days_back == 0:
                candidates = [step for step in candidates if step.minute <= minute_now]
            if not candidates:
                continue
            step = max(candidates, key=lambda item: item.minute)
            effective = (now - timedelta(days=days_back)).replace(
                hour=step.minute // 60, minute=step.minute % 60, second=0, microsecond=0
            )
            return step.level, effective
        return None

    def next_brightness_change(self, now=None):  # noqa: ANN001, ANN201 - datetime kept loose
        """Level and moment of the next scheduled step, or None if unscheduled.

        Used only to tell the user what is coming; the renderer never needs it.
        """
        if not self.brightness_schedule:
            return None

        from datetime import timedelta

        now = now or self.now()
        minute_now = now.hour * 60 + now.minute

        for days_ahead in range(8):
            weekday = (now.weekday() + days_ahead) % 7
            candidates = [step for step in self.brightness_schedule if weekday in step.days]
            if days_ahead == 0:
                candidates = [step for step in candidates if step.minute > minute_now]
            if not candidates:
                continue
            step = min(candidates, key=lambda item: item.minute)
            when = (now + timedelta(days=days_ahead)).replace(
                hour=step.minute // 60, minute=step.minute % 60, second=0, microsecond=0
            )
            return step.level, when
        return None

    def _manual_brightness(self) -> tuple[float, float] | None:
        """The web slider's level and when it was set, or None if never set."""
        try:
            parts = self.brightness_file.read_text(encoding="utf-8").split()
            # Files written before the schedule existed hold a bare number. Treating
            # their timestamp as 0 lets the schedule take over immediately, which is
            # the right call for a value of unknown age.
            return float(parts[0]), (float(parts[1]) if len(parts) > 1 else 0.0)
        except (OSError, ValueError, IndexError):
            return None

    def current_flight(self) -> str:
        """Flight number to track: the web app's value if set, otherwise .env."""
        try:
            typed = self.flight_file.read_text(encoding="utf-8").strip()
        except OSError:
            typed = ""
        return typed or self.flight_number

    def set_flight(self, flight: str) -> None:
        """Persist the tracked flight number; an empty string clears it.

        Only length is checked, not shape: airlines issue numbers that no tidy
        pattern covers, and rejecting an odd-looking one here would only mean a
        real flight the panel refuses to try.

        Also clears any watched airport. The two are alternative ways to choose
        what the flights screen shows, and holding both would leave the panel
        deciding silently between them -- so the most recent instruction wins.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        value = "".join(str(flight).split()).upper()[:12]
        self.flight_file.write_text(f"{value}\n", encoding="utf-8")
        if value:
            self.flight_airport_file.write_text("\n", encoding="utf-8")

    def current_flight_airport(self) -> str:
        """Airport whose arrivals to pick from, or "" when a number is tracked."""
        try:
            chosen = self.flight_airport_file.read_text(encoding="utf-8").strip().upper()
        except OSError:
            chosen = ""
        return chosen or self.flight_airport

    def set_flight_airport(self, airport: str) -> None:
        """Watch arrivals into an airport instead of one flight number.

        IATA is three letters and ICAO is four, so anything outside that is a
        typo rather than an airport, and rejecting it here means the panel can
        say so straight away instead of drawing an empty board later. Clears the
        tracked flight number for the reason given in :meth:`set_flight`.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        value = "".join(str(airport).split()).upper()
        if value and not (value.isalpha() and 3 <= len(value) <= 4):
            raise ValueError(f"not an airport code: {airport!r}")
        self.flight_airport_file.write_text(f"{value}\n", encoding="utf-8")
        if value:
            self.flight_file.write_text("\n", encoding="utf-8")

    def current_bart_station(self) -> str:
        """Station whose departures to show: the web app's pick, otherwise .env."""
        from ticker.bart import is_station

        try:
            chosen = self.bart_station_file.read_text(encoding="utf-8").strip().upper()
        except OSError:
            chosen = ""
        if is_station(chosen):
            return chosen
        return self.bart_station.strip().upper()

    def current_bike_station(self) -> str:
        """Bay Wheels station id: the web app's pick, otherwise .env.

        Unlike BART, station ids come from a live feed rather than a fixed
        list, so this is not validated against a closed set here. The feed
        client returns None for an unknown id and the renderer shows a
        "Not in feed" panel when that happens.
        """
        try:
            chosen = self.bike_station_file.read_text(encoding="utf-8").strip()
        except OSError:
            chosen = ""
        return chosen or self.bike_station_id

    def set_bike_station(self, station_id: str) -> None:
        """Persist the chosen Bay Wheels station id; an empty string clears it.

        A station id is opaque (a UUID-ish string in the Bay Wheels feed), so
        the only sanity check is length: an obviously oversized value is
        rejected to avoid stuffing garbage into a state file.
        """
        value = str(station_id).strip()
        if len(value) > 64:
            raise ValueError("bike station id is unreasonably long")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.bike_station_file.write_text(f"{value}\n", encoding="utf-8")

    def current_nametag_name(self) -> str:
        """Name to display on the nametag mode: state file wins, else .env."""
        try:
            chosen = self.nametag_name_file.read_text(encoding="utf-8").strip()
        except OSError:
            chosen = ""
        return chosen or self.nametag_name

    def set_nametag_name(self, name: str) -> None:
        """Persist the desk-plate name. Trims whitespace, caps length at 40 chars.

        The renderer's own auto-fit ladder clips at 21 characters in MEDIUM;
        this 40-char cap here is a durability guard against pathological input
        (someone pasting a paragraph into the field), not a display limit.
        """
        value = str(name).strip()
        if len(value) > 40:
            raise ValueError("nametag name is unreasonably long")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.nametag_name_file.write_text(f"{value}\n", encoding="utf-8")

    def current_nametag_color(self) -> tuple[int, int, int]:
        """Chosen text color as an (R, G, B) tuple; falls back to white on any error."""
        try:
            chosen = self.nametag_color_file.read_text(encoding="utf-8").strip()
        except OSError:
            chosen = ""
        return _parse_hex_color(chosen or self.nametag_color)

    def current_nametag_font(self) -> str:
        """Font family for the nametag mode: state file wins, else .env, else default.

        The renderer validates this against its own family list, so an unknown
        value here quietly falls back to the default rather than crashing.
        """
        try:
            chosen = self.nametag_font_file.read_text(encoding="utf-8").strip()
        except OSError:
            chosen = ""
        return chosen or self.nametag_font

    def set_nametag_font(self, family: str) -> None:
        """Persist the chosen nametag font family. Only known families are accepted."""
        # Kept in sync with nametag.VALID_FAMILIES; duplicated here to avoid a
        # circular import (config is imported by nametag).
        valid = ("spleen", "terminus", "scientifica")
        value = str(family).strip().lower()
        if value not in valid:
            raise ValueError(f"Unknown nametag font family: {family!r}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.nametag_font_file.write_text(f"{value}\n", encoding="utf-8")

    def set_nametag_color(self, hex_color: str) -> None:
        """Persist the chosen text color. Value is stored in canonical '#RRGGBB' form.

        Validated up front so a bad string never reaches disk. The renderer
        must never crash on a color read, so the on-disk value is guaranteed
        parseable by the same rules used here.
        """
        canonical = _canonical_hex_color(hex_color)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.nametag_color_file.write_text(f"{canonical}\n", encoding="utf-8")

    def set_bart_station(self, station: str) -> None:
        """Persist the chosen BART station.

        Validated here, unlike the flight number: the station list is a closed
        set of fifty known abbreviations, so a bad value is a bug rather than an
        obscure-but-real code the panel ought to try anyway.
        """
        from ticker.bart import is_station

        value = "".join(str(station).split()).upper()
        if not is_station(value):
            raise ValueError(f"Unknown BART station: {station}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.bart_station_file.write_text(f"{value}\n", encoding="utf-8")

    def current_brightness(self) -> float:
        """Resolve schedule and manual override into one level.

        A manual adjustment wins until the next scheduled step, then the schedule
        resumes. Without this the slider would be undone within a second by a
        schedule that never yields, and a permanent override would make the
        schedule pointless; expiring at the next step gives up neither.
        """
        manual = self._manual_brightness()
        scheduled = self.scheduled_brightness()

        if scheduled is None:
            value = manual[0] if manual else self.brightness
            return max(0.05, min(1.0, value))

        level, since = scheduled
        if manual and manual[1] > since.timestamp():
            return max(0.05, min(1.0, manual[0]))
        # Only the schedule may take the panel fully dark; the slider floors at 5%
        # so that dragging it down cannot leave a black panel with no way back.
        return max(0.0, min(1.0, level))

    def set_brightness(self, brightness: float) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        value = max(0.05, min(1.0, float(brightness)))
        stamp = self.now().timestamp()
        self.brightness_file.write_text(f"{value:.2f} {stamp:.0f}\n", encoding="utf-8")


def load_config(env_file: Path | None = None) -> Config:
    """Load .env once and construct a typed configuration object."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=env_file is not None)
    symbols = tuple(
        symbol.strip().upper()
        for symbol in os.getenv("TICKER_SYMBOLS", "AAPL,NVDA,SPY,BTC-USD").split(",")
        if symbol.strip()
    )
    crypto_symbols = tuple(
        symbol.strip().upper()
        for symbol in os.getenv("CRYPTO_SYMBOLS", "BTC,ETH").split(",")
        if symbol.strip()
    ) or ("BTC", "ETH")
    return Config(
        width=int(os.getenv("TICKER_WIDTH", "128")),
        height=int(os.getenv("TICKER_HEIGHT", "32")),
        addr_lines=int(os.getenv("TICKER_ADDR_LINES", "4")),
        channel_order=_parse_channel_order(os.getenv("TICKER_CHANNEL_ORDER", "rgb")),
        brightness=float(os.getenv("TICKER_BRIGHTNESS", "0.35")),
        brightness_schedule=parse_brightness_schedule(os.getenv("TICKER_BRIGHTNESS_SCHEDULE", "")),
        fps=max(1, int(os.getenv("TICKER_FPS", "30"))),
        symbols=symbols,
        stocks_layout=os.getenv("STOCKS_LAYOUT", "card").strip().lower(),
        news_feed_url=os.getenv("NEWS_FEED_URL", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
        news_source_name=os.getenv("NEWS_SOURCE_NAME", "CNBC MARKETS"),
        flight_number="".join(os.getenv("FLIGHT_NUMBER", "").split()).upper(),
        flight_airport=os.getenv("FLIGHT_AIRPORT", "").strip().upper(),
        crypto_symbols=crypto_symbols,
        bart_station=os.getenv("BART_STATION", "EMBR").strip().upper() or "EMBR",
        bike_station_id=os.getenv("BIKE_STATION_ID", "").strip(),
        nametag_name=os.getenv("NAMETAG_NAME", "").strip(),
        nametag_color=(os.getenv("NAMETAG_COLOR", "").strip() or _DEFAULT_NAMETAG_HEX),
        nametag_font=(os.getenv("NAMETAG_FONT", "").strip().lower() or "spleen"),
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
        spotify_redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "").strip(),
        weather_lat=os.getenv("WEATHER_LAT", ""),
        weather_lon=os.getenv("WEATHER_LON", ""),
        weather_user_agent=os.getenv(
            "WEATHER_USER_AGENT", "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"
        ),
        timezone=os.getenv("TICKER_TIMEZONE", ""),
        clock_24h=os.getenv("TICKER_CLOCK_24H", "false").strip().lower() in {"1", "true", "yes"},
        state_dir=_first_writable_state_dir(),
    )
