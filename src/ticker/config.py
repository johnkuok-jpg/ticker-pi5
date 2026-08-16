# MIT License — Copyright (c) 2026 John Kuok
"""Configuration and small file-backed control state for ticker-pi5."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_MODES = ("stocks", "news", "weather", "flights", "market", "crypto", "currency", "quakes", "bart", "muni", "aqi", "bikes", "nametag", "spotify", "pokemon", "focus", "net", "worldclock", "youtube", "costco")

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


def _parse_float_env(name: str, default: float) -> float:
    """Read a float env var; malformed values fall back so a typo can't crash boot."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_int_env(name: str, default: int) -> int:
    """Read an int env var; malformed values fall back so a typo can't crash boot."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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

# Watchlist ceiling. The stocks mode gives each symbol a 6-second card, so a
# dozen symbols is already a 72-second trip round the list -- long enough that
# the symbol you want is rarely the one on screen. The cap is a usability limit
# first and a guard against a runaway state file second.
MAX_SYMBOLS = 12

# Currency mode shows up to four pairs on the panel. The classic (no-flag)
# layout still caps at three rows (SMALL font, MAX_ROWS in the mode), but the
# flag-column layout without the % change column packs four SMALL rows flush
# against the 32-row panel edges, and this cap has to allow at least that many.
MAX_CURRENCY_PAIRS = 4
# ISO 4217 codes are always three letters. Widening this would let a typo slip
# through as a mystery-lookup that the upstream endpoint would just reject.
_CURRENCY_CODE_LEN = 3

# Costco warehouses in the panel rotation. Each ~5s slide shows one warehouse's
# gas prices, so three is a full ~15s trip round the list -- enough to compare
# nearby stations without turning the panel into a scrolling menu. The cap is
# also what the panel can fit above the fold on the webapp card.
MAX_COSTCO_WAREHOUSES = 3
# Warehouse IDs on the Costco locator API are integers rendered as strings
# (e.g. "475", "1188"). They are opaque -- 3 digits today, could be 4 tomorrow
# -- so this bound just guards against a runaway state file.
_COSTCO_ID_MAX_LEN = 8


def _parse_costco_warehouses(raw: str) -> tuple[str, ...]:
    """Parse ``475,422,118`` into ``("475", "422", "118")``.

    Silently drops non-numeric tokens the same way ``_parse_currency_pairs``
    does: a typo in the state file must not knock the whole card offline. The
    caller falls back to a default list when the returned tuple is empty.
    """
    parsed: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token or not token.isdigit() or len(token) > _COSTCO_ID_MAX_LEN:
            continue
        if token not in parsed:
            parsed.append(token)
    return tuple(parsed)


def _parse_currency_pairs(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse ``USD/JPY,USD/EUR`` into ``(("USD","JPY"),("USD","EUR"))``.

    Silently drops malformed tokens the same way the .env loader does: an
    obscure typo in a state file must not knock the whole card offline. The
    caller falls back to a default list when the returned tuple is empty.
    """
    parsed: list[tuple[str, str]] = []
    for token in raw.split(","):
        token = token.strip().upper()
        if "/" not in token:
            continue
        base, _, quote = token.partition("/")
        base, quote = base.strip(), quote.strip()
        # Accept 3-4 char codes here for parity with the .env loader; the
        # setters below refuse anything but 3 so a webapp save is stricter
        # than the env-var fallback (which reads legacy files).
        if base.isalpha() and quote.isalpha() and 3 <= len(base) <= 4 and 3 <= len(quote) <= 4:
            parsed.append((base, quote))
    return tuple(parsed)

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
    # Foreign-exchange watchlist as (BASE, QUOTE) pairs. Defaults hit the
    # currencies John's family and partner-billing work touch: JPY (SBKK/
    # Japan), EUR (Telefónica/DT), CNY (personal/family). Anything past three
    # is silently ignored by the renderer -- the panel only has room for three
    # rows before the SMALL font kicks in and readability drops off.
    currency_pairs: tuple[tuple[str, str], ...] = (
        ("USD", "JPY"),
        ("USD", "EUR"),
        ("USD", "CNY"),
    )
    # Costco warehouse rotation for the gas-prices mode. Each entry is a
    # ``stlocID`` string from the Costco locator API. Default is the El Camino
    # store (South San Francisco, warehouse 475) -- closest Costco with gas
    # to John's SF apartment. Anything past MAX_COSTCO_WAREHOUSES is silently
    # ignored by the renderer.
    costco_warehouses: tuple[str, ...] = ("475",)
    # Quake-alert auto-switch. When enabled, the renderer polls USGS for a
    # small-region shake (California by default) and temporarily forces the
    # panel into quakes mode when one lands, then restores the user's chosen
    # mode after the dwell window. M3.0 in California is roughly once every
    # few days -- alerts land but do not spam. M3.5 is more like once every
    # week or two and is the current default. The region string is a
    # case-insensitive substring against the USGS ``place`` field; empty
    # means worldwide. The alerter also cross-checks longitude/latitude
    # against a California bounding box so the occasional feature USGS
    # publishes without "California" in the place still fires.
    quake_alert_enabled: bool = True
    quake_alert_min_mag: float = 3.5
    quake_alert_region: str = "California"
    quake_alert_dwell_seconds: int = 120
    bart_station: str = "EMBR"
    # Muni stopcode: the 5-digit number printed on every SF Muni shelter
    # sign. Empty by default so the mode nudges the user to the picker on
    # first run; the webapp writes the chosen code into the state file.
    muni_stop_code: str = ""
    bike_station_id: str = ""
    nametag_name: str = ""
    nametag_color: str = "#FFFFFF"
    nametag_font: str = "spleen"
    # Spotify developer app credentials. Empty by default; when either is
    # missing, the Spotify mode renders a helpful 'set SPOTIFY_CLIENT_ID'
    # placeholder instead of blowing up on a None token exchange.
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    # Finnhub API key -- gates real-time US-equity quotes. Get one free at
    # https://finnhub.io/register. Free tier is 60 req/min and rate-limits
    # (not billing) if you exceed it, so a committed default is fine for a
    # hobby ticker; anyone who wants their own quota can override with the
    # FINNHUB_API_KEY env var. If both are empty the stocks mode falls back
    # to yfinance (15-20 min delayed for US equities).
    finnhub_api_key: str = "d6jjqthr01qkvh5q7fd0d6jjqthr01qkvh5q7fdg"
    # Where Spotify should send the user back after they authorise. Must match
    # a redirect URI registered on the Spotify app dashboard exactly.
    #
    # Spotify rejects plain-http redirect URIs unless the host is the literal
    # loopback IP, so ticker.local is not allowed here. The consequence is that
    # a phone completing the flow gets redirected to its own 127.0.0.1 and sees
    # a connection error; the webapp's /spotify/paste route exists to finish
    # the exchange from the code in that dead URL.
    spotify_redirect_uri: str = "http://127.0.0.1:8080/spotify/callback"
    weather_lat: str = ""
    weather_lon: str = ""
    # Optional ZIP seed. When set and no state file exists, the weather and
    # air-quality modes resolve it to coordinates on first use, so a fresh
    # Pi can be aimed with WEATHER_ZIP=94103 instead of a coordinate pair.
    weather_zip: str = ""
    weather_user_agent: str = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"
    timezone: str = ""
    clock_24h: bool = False
    state_dir: Path = Path.home() / ".ticker"

    def now(self):  # noqa: ANN201 - datetime, kept loose to avoid a module-level import cycle
        """Current local time, honouring TICKER_TIMEZONE when it is set and valid.

        Always returns a timezone-aware datetime. When TICKER_TIMEZONE is unset
        or unparseable we fall back to the system's local timezone rather than
        a naive value, because downstream callers (notably ``market.session_state``)
        relabel naive datetimes as America/New_York without converting — which
        would silently misreport market hours by whatever offset the Pi actually
        sits at (e.g. 3 hours off in the Bay Area).
        """
        from datetime import datetime

        if self.timezone:
            try:
                from zoneinfo import ZoneInfo

                return datetime.now(ZoneInfo(self.timezone))
            except Exception:
                pass  # fall through to the system clock
        return datetime.now().astimezone()

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
    def muni_stop_file(self) -> Path:
        return self.state_dir / "muni_stop"

    @property
    def bike_station_file(self) -> Path:
        return self.state_dir / "bike_station"

    @property
    def weather_location_file(self) -> Path:
        """Tab-separated ``zip<TAB>lat<TAB>lon<TAB>city<TAB>state``.

        Stores the resolved coordinates next to the ZIP that produced them
        so the weather mode never has to geocode at render time -- the
        lookup happens once, in the web request that set the ZIP.
        """
        return self.state_dir / "weather_location"

    @property
    def symbols_file(self) -> Path:
        return self.state_dir / "symbols"

    @property
    def currency_pairs_file(self) -> Path:
        """Comma-separated BASE/QUOTE pairs (e.g. ``USD/JPY,USD/EUR,USD/CNY``).

        Falls back to ``CURRENCY_PAIRS`` when the file is missing or empty --
        same convention as the stocks watchlist. Kept alongside ``symbols``
        because both are user-picked lists that the webapp rewrites live.
        """
        return self.state_dir / "currency_pairs"

    @property
    def currency_show_change_file(self) -> Path:
        """Toggle for the 24-hour change column on the currency card.

        Written as ``on`` or ``off``. Missing file = on (matches the historical
        behavior before this toggle existed, so a first upgrade is invisible).
        """
        return self.state_dir / "currency_show_change"

    @property
    def currency_flag_mode_file(self) -> Path:
        """Toggle for the two-row flag layout on the currency card.

        Written as ``on`` or ``off``. Missing file = off, so a fresh install
        keeps the historical three-row rate board and only opts in when the
        user asks for it.
        """
        return self.state_dir / "currency_flag_mode"

    @property
    def costco_warehouses_file(self) -> Path:
        """Comma-separated Costco warehouse IDs (e.g. ``475,422,118``).

        Falls back to ``costco_warehouses`` on the dataclass when the file is
        missing or empty -- same convention as the stocks watchlist. Kept as a
        list of opaque IDs rather than city names so the fetcher can hit the
        Costco locator API directly without a name-to-ID lookup step.
        """
        return self.state_dir / "costco_warehouses"

    @property
    def youtube_playlist_file(self) -> Path:
        """Selected YouTube category or a custom playlist URL.

        Stores either a category key (e.g. ``nature``) or a full playlist URL
        (starts with ``http``). The mode resolves the key to a URL on load.
        """
        return self.state_dir / "youtube_playlist"

    @property
    def youtube_skip_file(self) -> Path:
        """Monotonic counter the YouTube mode watches to advance to the next video.

        The webapp increments this counter on the /youtube/next endpoint; the
        renderer polls the file each tick and, when it sees a higher number
        than the last one it observed, skips to the next video. A counter
        (rather than a boolean flag) lets us handle rapid taps correctly.
        """
        return self.state_dir / "youtube_skip"

    @property
    def stocks_lock_symbol_file(self) -> Path:
        """When present + non-empty, the stocks card pins on this symbol
        instead of rotating through the whole watchlist. Written by the web
        app so a user can dwell on one ticker; deleting the file (or writing
        an empty string) goes back to the rotation."""
        return self.state_dir / "stocks_lock_symbol"

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
    def quake_filter_file(self) -> Path:
        """JSON blob for the *display* filter on the passive quakes mode.

        Distinct from ``quake_alert_settings_file`` because these two features
        are semantically different: alert is "when should the panel interrupt
        me?", filter is "while I'm looking at quakes, what should I see?".
        Keeping them separate means a user can, say, filter the display to
        California while still keeping worldwide alerts on.

        Schema: ``{"min_mag": 3.5, "region": "California"}``. Both keys are
        optional; missing keys fall back to env defaults.
        """
        return self.state_dir / "quake_filter.json"

    @property
    def quake_alert_settings_file(self) -> Path:
        """JSON blob for quake-alert settings (min_mag / region / dwell).

        One file because these three knobs are always edited together in the
        webapp settings card, and a single atomic write keeps them consistent.
        Missing / corrupt file falls back to the .env defaults so a bad edit
        can't disable alerts silently.
        """
        return self.state_dir / "quake_alert_settings.json"

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
        """Read the requested mode; return the default when absent/corrupt.

        Kept read-only on purpose. The renderer runs as root and calls this on
        every frame; the web app runs as pi. If this function wrote a default
        on read, an invalid file would flip to a root-owned file the web app
        could no longer overwrite, silently breaking mode switching from the
        UI until someone chowned the file. Falling back to the default in
        memory keeps behaviour identical without leaving a permission mine.
        """
        try:
            value = self.mode_file.read_text(encoding="utf-8").strip().lower()
        except OSError:
            value = ""
        if value not in VALID_MODES:
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

    def current_weather_location(self) -> tuple[str, str, str, str]:
        """Weather target as ``(zip, lat, lon, label)``; empty strings if unset.

        Resolution order:

        1. The state file written by the web app's ZIP picker. This is the
           live, user-facing setting and always wins.
        2. ``WEATHER_LAT``/``WEATHER_LON`` from ``.env``, for panels that
           were set up before the picker existed and for anyone who wants
           to aim at a point that has no ZIP (a ridge, a lake, a park).

        The ZIP and label are best-effort: a panel configured purely from
        lat/lon has coordinates but no ZIP, and that is a valid state --
        callers render the coordinates and leave the label blank.
        """
        try:
            raw = self.weather_location_file.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if raw:
            parts = raw.split("\t")
            # Guard against a hand-edited or half-written file: coordinates
            # are the only fields the API actually needs, so require those
            # two to parse as floats before trusting the record.
            if len(parts) >= 3:
                zip_code, lat, lon = parts[0].strip(), parts[1].strip(), parts[2].strip()
                city = parts[3].strip() if len(parts) > 3 else ""
                state = parts[4].strip() if len(parts) > 4 else ""
                try:
                    float(lat)
                    float(lon)
                except ValueError:
                    pass
                else:
                    label = f"{city}, {state}" if city and state else (city or state)
                    return zip_code, lat, lon, label

        lat, lon = self.weather_lat.strip(), self.weather_lon.strip()
        if lat and lon:
            return "", lat, lon, ""

        # No state file and no coordinates, but a WEATHER_ZIP seed exists:
        # resolve it once and write the state file, so this costs one lookup
        # on the first render after setup rather than one per render. A
        # failed lookup falls through to "unset" and the mode shows its
        # "set weather location" prompt, same as before.
        seed = self.weather_zip.strip()
        if seed:
            try:
                location = self.set_weather_zip(seed)
            except (ValueError, OSError):
                location = None
            if location is not None:
                return (
                    location.zip_code,
                    f"{location.lat:.4f}",
                    f"{location.lon:.4f}",
                    location.label,
                )

        return "", "", "", ""

    def current_weather_coords(self) -> tuple[str, str]:
        """Just the ``(lat, lon)`` the weather APIs need. Empty when unset."""
        _, lat, lon, _ = self.current_weather_location()
        return lat, lon

    def current_weather_zip(self) -> str:
        """The ZIP currently aiming the weather modes, or "" if aimed by lat/lon."""
        zip_code, _, _, _ = self.current_weather_location()
        return zip_code or self.weather_zip.strip()

    def set_weather_zip(self, zip_code: str):  # noqa: ANN201 - ZipLocation, avoids an import cycle
        """Resolve a US ZIP and persist it as the weather target.

        Returns the resolved ``ZipLocation`` so the caller can echo the city
        name back to the user. Raises ValueError when the ZIP is malformed or
        the geocoder does not recognise it -- in that case nothing is written,
        so a typo leaves the panel pointed where it already was rather than
        blanking the forecast.

        Passing an empty string clears the override and falls the modes back
        to ``WEATHER_LAT``/``WEATHER_LON``.
        """
        from ticker import zipcode

        raw = str(zip_code or "").strip()
        if not raw:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.weather_location_file.write_text("", encoding="utf-8")
            return None

        normalized = zipcode.normalize(raw)
        if not normalized:
            raise ValueError("enter a 5-digit US ZIP code")

        location = zipcode.lookup(normalized)
        if location is None:
            raise ValueError(f"could not find ZIP {normalized}")

        self.state_dir.mkdir(parents=True, exist_ok=True)
        record = "\t".join(
            [
                location.zip_code,
                f"{location.lat:.4f}",
                f"{location.lon:.4f}",
                location.city,
                location.state,
            ]
        )
        self.weather_location_file.write_text(f"{record}\n", encoding="utf-8")
        return location

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

    def _read_quake_alert_overrides(self) -> dict:
        """Return whatever's in the settings file, or {} on any error.

        The watcher polls once a second; a bad JSON payload here would flap the
        panel between alert on/off, so we're deliberate about swallowing errors
        and letting the .env defaults win in that case.
        """
        try:
            raw = self.quake_alert_settings_file.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def current_quake_alert_min_mag(self) -> float:
        """Threshold for firing an alert. Web-app setting wins over .env."""
        override = self._read_quake_alert_overrides().get("min_mag")
        try:
            value = float(override) if override is not None else self.quake_alert_min_mag
        except (TypeError, ValueError):
            return self.quake_alert_min_mag
        # Clamp to the same sane range the setter uses, so a hand-edited file
        # can't put the watcher into a state the webapp can't get out of.
        return max(2.5, min(9.9, value))

    def current_quake_alert_region(self) -> str:
        """Region substring / preset. Web-app setting wins over .env."""
        override = self._read_quake_alert_overrides().get("region")
        if override is None:
            return self.quake_alert_region
        try:
            return str(override).strip()
        except Exception:  # pragma: no cover - defensive
            return self.quake_alert_region

    def current_quake_alert_dwell_seconds(self) -> int:
        """Dwell window in seconds. Web-app setting wins over .env."""
        override = self._read_quake_alert_overrides().get("dwell_seconds")
        try:
            value = int(override) if override is not None else self.quake_alert_dwell_seconds
        except (TypeError, ValueError):
            return self.quake_alert_dwell_seconds
        # Same 15s floor and 15-minute ceiling the setter enforces.
        return max(15, min(900, value))

    def set_quake_alert_settings(
        self,
        *,
        min_mag: float | None = None,
        region: str | None = None,
        dwell_seconds: int | None = None,
    ) -> None:
        """Merge new values into the settings file. Any None arg leaves that field alone.

        Validation matches the getters so a value that lands on disk is always
        one a getter would return unchanged. An out-of-range value raises; the
        webapp turns that into a 400 rather than a silent clamp so the user
        knows what they typed was rejected.
        """
        overrides = self._read_quake_alert_overrides()
        if min_mag is not None:
            try:
                mag = float(min_mag)
            except (TypeError, ValueError) as error:
                raise ValueError("minimum magnitude must be a number") from error
            if not 2.5 <= mag <= 9.9:
                raise ValueError("minimum magnitude must be between 2.5 and 9.9")
            overrides["min_mag"] = round(mag, 1)
        if region is not None:
            text = str(region).strip()
            if len(text) > 80:
                raise ValueError("region string is unreasonably long")
            overrides["region"] = text
        if dwell_seconds is not None:
            try:
                dwell = int(dwell_seconds)
            except (TypeError, ValueError) as error:
                raise ValueError("dwell must be an integer number of seconds") from error
            if not 15 <= dwell <= 900:
                raise ValueError("dwell must be between 15 and 900 seconds")
            overrides["dwell_seconds"] = dwell
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.quake_alert_settings_file.write_text(
            json.dumps(overrides, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    # --- Quakes display filter --------------------------------------------
    #
    # Parallel structure to the alert settings above, but scoped to the passive
    # display: which quakes should appear when the user manually opens the
    # quakes mode. Deliberately not sharing state with the alert settings --
    # some people want "alert me on California only" but "show me global
    # quakes when I open the mode" (or vice-versa).

    def _read_quake_filter_overrides(self) -> dict:
        try:
            raw = self.quake_filter_file.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def current_quake_filter_min_mag(self) -> float:
        """Display floor for the quakes mode. Falls back to 4.5 (the classic feed)."""
        override = self._read_quake_filter_overrides().get("min_mag")
        try:
            value = float(override) if override is not None else 4.5
        except (TypeError, ValueError):
            return 4.5
        # Clamp to the same range the setter uses so a hand-edited file can't
        # push the display below the underlying M2.5+ feed floor.
        return max(2.5, min(9.9, value))

    def current_quake_filter_region(self) -> str:
        """Region substring for the display. Empty string = worldwide."""
        override = self._read_quake_filter_overrides().get("region")
        if override is None:
            return ""
        try:
            return str(override).strip()
        except Exception:  # pragma: no cover - defensive
            return ""

    def set_quake_filter(
        self,
        *,
        min_mag: float | None = None,
        region: str | None = None,
    ) -> None:
        """Merge new values into the display filter file.

        Ranges mirror the alert setter so the same UI validation applies:
        magnitude 2.5-9.9, region <=80 chars.
        """
        overrides = self._read_quake_filter_overrides()
        if min_mag is not None:
            try:
                mag = float(min_mag)
            except (TypeError, ValueError) as error:
                raise ValueError("minimum magnitude must be a number") from error
            if not 2.5 <= mag <= 9.9:
                raise ValueError("minimum magnitude must be between 2.5 and 9.9")
            overrides["min_mag"] = round(mag, 1)
        if region is not None:
            text = str(region).strip()
            if len(text) > 80:
                raise ValueError("region string is unreasonably long")
            overrides["region"] = text
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.quake_filter_file.write_text(
            json.dumps(overrides, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def current_symbols(self) -> tuple[str, ...]:
        """Watchlist for the stocks mode: state file wins, else ``TICKER_SYMBOLS``.

        Read live on every refresh rather than captured at construction, so an
        edit in the web app lands on the panel at the next quote refresh without
        restarting the renderer.

        An empty state file is not the same as a missing one: clearing the list
        down to nothing is a deliberate act, but a panel with no symbols has
        nothing to draw, so the env-var default is restored instead.
        """
        try:
            raw = self.symbols_file.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        stored = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
        return stored or self.symbols

    def set_symbols(self, symbols: Iterable[str]) -> None:
        """Persist the whole watchlist, de-duplicated, in the order given.

        Every ticker is validated for shape. The universe of real symbols is not
        knowable offline -- exchange suffixes, index carets and crypto pairs are
        all legitimate -- so this checks the character set rather than trying to
        keep a list of every symbol that exists.
        """
        cleaned: list[str] = []
        for symbol in symbols:
            value = "".join(str(symbol).split()).upper()
            if not value:
                continue
            if len(value) > 12:
                raise ValueError(f"Symbol is unreasonably long: {symbol!r}")
            if not all(char.isalnum() or char in ".-^=" for char in value):
                raise ValueError(f"Not a valid ticker symbol: {symbol!r}")
            if value not in cleaned:
                cleaned.append(value)
        if len(cleaned) > MAX_SYMBOLS:
            raise ValueError(f"Watchlist is limited to {MAX_SYMBOLS} symbols")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.symbols_file.write_text(",".join(cleaned) + "\n", encoding="utf-8")

    def add_symbol(self, symbol: str) -> tuple[str, ...]:
        """Append one symbol to the watchlist and return the new list.

        Adding a symbol already on the list is a no-op rather than an error: the
        user's intent ("I want to see this") is already satisfied, and a red
        error for a duplicate tap would be noise.
        """
        current = list(self.current_symbols())
        current.append(symbol)
        self.set_symbols(current)
        return self.current_symbols()

    def remove_symbol(self, symbol: str) -> tuple[str, ...]:
        """Drop one symbol from the watchlist and return the new list.

        Removing the last remaining symbol is refused. An empty list would fall
        back to the env-var default, so the delete button would appear to undo
        itself and repopulate four symbols the user never asked for; a plain
        "keep at least one" is easier to understand than that.
        """
        target = "".join(str(symbol).split()).upper()
        remaining = [item for item in self.current_symbols() if item != target]
        if not remaining:
            raise ValueError("Keep at least one symbol on the watchlist")
        self.set_symbols(remaining)
        # If the removed symbol was the locked one, drop the lock too so the
        # card doesn't get stuck showing the last known quote for a symbol
        # that no longer refreshes.
        if target and self.current_stocks_lock_symbol() == target:
            self.set_stocks_lock_symbol("")
        return self.current_symbols()

    # -- currency ------------------------------------------------------------

    def current_currency_pairs(self) -> tuple[tuple[str, str], ...]:
        """Pairs for the currency mode: state file wins, else ``CURRENCY_PAIRS``.

        Same read-live-on-every-refresh convention as ``current_symbols`` so a
        webapp edit lands on the panel at the next fetch cycle. An empty stored
        list falls back to the env-var default: a currency card with no pairs
        has nothing to draw.
        """
        try:
            raw = self.currency_pairs_file.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        stored = _parse_currency_pairs(raw)
        return stored or self.currency_pairs

    def set_currency_pairs(self, pairs: Iterable[tuple[str, str] | str]) -> None:
        """Persist the pair list, de-duplicated, in the order given.

        Accepts either ``("USD", "JPY")`` tuples or ``"USD/JPY"`` strings; the
        webapp uses strings, the tests use tuples. Both codes must be three
        letters -- ISO 4217 is fixed-width and anything else would be a typo
        that the upstream endpoint would just reject silently.
        """
        cleaned: list[tuple[str, str]] = []
        for item in pairs:
            if isinstance(item, str):
                if "/" not in item:
                    raise ValueError(f"Pair must look like BASE/QUOTE: {item!r}")
                base, quote = item.split("/", 1)
            else:
                base, quote = item
            base = "".join(str(base).split()).upper()
            quote = "".join(str(quote).split()).upper()
            if len(base) != _CURRENCY_CODE_LEN or not base.isalpha():
                raise ValueError(f"Not a valid currency code: {base!r}")
            if len(quote) != _CURRENCY_CODE_LEN or not quote.isalpha():
                raise ValueError(f"Not a valid currency code: {quote!r}")
            if base == quote:
                raise ValueError(f"A pair needs two different codes: {base}/{quote}")
            pair = (base, quote)
            if pair not in cleaned:
                cleaned.append(pair)
        if len(cleaned) > MAX_CURRENCY_PAIRS:
            raise ValueError(
                f"Currency mode is limited to {MAX_CURRENCY_PAIRS} pairs"
            )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        text = ",".join(f"{base}/{quote}" for base, quote in cleaned)
        self.currency_pairs_file.write_text(text + "\n", encoding="utf-8")

    def add_currency_pair(self, pair: tuple[str, str] | str) -> tuple[tuple[str, str], ...]:
        """Append one pair to the list and return the new list.

        Adding a pair already on the list is a no-op -- the user's intent
        ("show me this") is already satisfied, and an error on a duplicate tap
        would be noise.
        """
        current = list(self.current_currency_pairs())
        current.append(pair)  # type: ignore[arg-type]
        self.set_currency_pairs(current)
        return self.current_currency_pairs()

    def remove_currency_pair(self, pair: tuple[str, str] | str) -> tuple[tuple[str, str], ...]:
        """Drop one pair from the list and return the new list.

        Removing the last pair is refused: an empty state file falls back to
        the env-var default, so the delete button would appear to undo itself.
        Mirrors the stocks watchlist's "keep at least one" rule.
        """
        if isinstance(pair, str):
            if "/" not in pair:
                raise ValueError(f"Pair must look like BASE/QUOTE: {pair!r}")
            base, quote = pair.split("/", 1)
        else:
            base, quote = pair
        target = (base.strip().upper(), quote.strip().upper())
        remaining = [item for item in self.current_currency_pairs() if item != target]
        if not remaining:
            raise ValueError("Keep at least one currency pair")
        self.set_currency_pairs(remaining)
        return self.current_currency_pairs()

    def current_currency_show_change(self) -> bool:
        """Whether the currency card renders the 24-hour change column.

        Missing file returns True so the first upgrade after this toggle lands
        looks identical to today's panel. Only the exact string ``off`` (any
        case, ignoring surrounding whitespace) turns it off.
        """
        try:
            raw = self.currency_show_change_file.read_text(encoding="utf-8")
        except OSError:
            return True
        return raw.strip().lower() != "off"

    def set_currency_show_change(self, enabled: bool) -> bool:
        """Persist the change-column toggle; returns the effective state."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.currency_show_change_file.write_text(
            ("on" if enabled else "off") + "\n", encoding="utf-8"
        )
        return self.current_currency_show_change()

    def current_currency_flag_mode(self) -> bool:
        """Whether the currency card renders the two-row flag layout.

        Missing file returns False so an upgrade lands on the old three-row
        rate board; only the exact string ``on`` (any case, ignoring
        surrounding whitespace) opts in.
        """
        try:
            raw = self.currency_flag_mode_file.read_text(encoding="utf-8")
        except OSError:
            return False
        return raw.strip().lower() == "on"

    def set_currency_flag_mode(self, enabled: bool) -> bool:
        """Persist the flag-mode toggle; returns the effective state."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.currency_flag_mode_file.write_text(
            ("on" if enabled else "off") + "\n", encoding="utf-8"
        )
        return self.current_currency_flag_mode()

    @property
    def currency_flag_grid_file(self) -> Path:
        """State file for the flag-layout arrangement toggle.

        Content is either ``stack`` (four rows top to bottom, the default) or
        ``grid`` (2x2 quadrants). This only takes effect when flag mode is on
        and there are three or four pairs -- one and two pair configs render
        identically in both arrangements.
        """
        return self.state_dir / "currency_flag_grid"

    def current_currency_flag_grid(self) -> bool:
        """Whether the flag layout uses the 2x2 grid arrangement.

        Missing file returns False so an upgrade lands on the existing
        stacked layout; only the exact string ``on`` (any case, ignoring
        surrounding whitespace) opts in.
        """
        try:
            raw = self.currency_flag_grid_file.read_text(encoding="utf-8")
        except OSError:
            return False
        return raw.strip().lower() == "on"

    def set_currency_flag_grid(self, enabled: bool) -> bool:
        """Persist the flag-grid toggle; returns the effective state."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.currency_flag_grid_file.write_text(
            ("on" if enabled else "off") + "\n", encoding="utf-8"
        )
        return self.current_currency_flag_grid()

    # -- costco --------------------------------------------------------------

    def current_costco_warehouses(self) -> tuple[str, ...]:
        """Warehouse IDs the Costco card should rotate through.

        State file wins, else the dataclass default. Read live on every
        refresh (same convention as ``current_symbols``) so a webapp edit
        lands on the panel at the next fetch cycle. An empty stored list
        falls back to the default -- a Costco card with zero warehouses has
        nothing to draw.
        """
        try:
            raw = self.costco_warehouses_file.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        stored = _parse_costco_warehouses(raw)
        return stored or self.costco_warehouses

    def set_costco_warehouses(self, warehouses: Iterable[str]) -> None:
        """Persist the warehouse list, de-duplicated, in the order given.

        Each ID must be a positive integer written as a string. The universe
        of real warehouse IDs isn't knowable offline (Costco keeps adding
        stores), so this validates shape rather than a fixed list.
        """
        cleaned: list[str] = []
        for warehouse in warehouses:
            value = "".join(str(warehouse).split())
            if not value:
                continue
            if not value.isdigit():
                raise ValueError(f"Not a valid Costco warehouse id: {warehouse!r}")
            if len(value) > _COSTCO_ID_MAX_LEN:
                raise ValueError(f"Warehouse id is unreasonably long: {warehouse!r}")
            if value not in cleaned:
                cleaned.append(value)
        if len(cleaned) > MAX_COSTCO_WAREHOUSES:
            raise ValueError(
                f"Costco mode is limited to {MAX_COSTCO_WAREHOUSES} warehouses"
            )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.costco_warehouses_file.write_text(",".join(cleaned) + "\n", encoding="utf-8")

    def add_costco_warehouse(self, warehouse: str) -> tuple[str, ...]:
        """Append one warehouse ID to the list and return the new list.

        Adding a warehouse already on the list is a no-op -- the user's
        intent ("show me this station") is already satisfied, and an error
        on a duplicate tap would be noise.
        """
        current = list(self.current_costco_warehouses())
        current.append(warehouse)
        self.set_costco_warehouses(current)
        return self.current_costco_warehouses()

    def remove_costco_warehouse(self, warehouse: str) -> tuple[str, ...]:
        """Drop one warehouse ID from the list and return the new list.

        Removing the last warehouse is refused: an empty state file falls
        back to the dataclass default, so the delete button would appear to
        undo itself. Mirrors the stocks watchlist's "keep at least one" rule.
        """
        target = "".join(str(warehouse).split())
        remaining = [item for item in self.current_costco_warehouses() if item != target]
        if not remaining:
            raise ValueError("Keep at least one Costco warehouse")
        self.set_costco_warehouses(remaining)
        return self.current_costco_warehouses()

    def current_stocks_lock_symbol(self) -> str:
        """Symbol the stocks card is pinned to, or empty for the normal rotation."""
        try:
            raw = self.stocks_lock_symbol_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return raw.upper()

    def set_stocks_lock_symbol(self, symbol: str) -> str:
        """Persist a stocks lock. Empty string clears the lock.

        Validated against the current watchlist to avoid pinning on a symbol
        that isn't being refreshed -- the card would just render "WAITING FOR
        PRICES" indefinitely and it wouldn't be obvious why.
        """
        value = "".join(str(symbol).split()).upper()
        if value:
            watched = self.current_symbols()
            if value not in watched:
                raise ValueError(f"{value} is not on the watchlist")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Write empty string as an empty file rather than deleting; both mean
        # the same thing to current_stocks_lock_symbol() and the write is
        # atomic-ish either way.
        self.stocks_lock_symbol_file.write_text(value + "\n", encoding="utf-8")
        return value

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

    def current_muni_stop(self) -> str:
        """Muni stopcode to show: the web app's pick, otherwise .env.

        Unlike BART's closed roster this is validated by shape only (4-6
        digits), because Muni's ~3,700 stops are not enumerated at boot;
        an unknown code just returns empty predictions and the mode shows
        the "NO ARRIVALS" state, which is the honest render for it.
        """
        try:
            chosen = self.muni_stop_file.read_text(encoding="utf-8").strip()
        except OSError:
            chosen = ""
        return chosen or self.muni_stop_code.strip()

    def set_muni_stop(self, stop_code: str) -> None:
        """Persist the chosen Muni stopcode; an empty string clears it.

        Shape check only: 4-6 digits. That covers every Muni stopcode
        printed on a shelter sign today (all five-digit) without hard-
        coding the assumption that SFMTA won't ever renumber.
        """
        value = "".join(str(stop_code).split())
        if value and not (value.isdigit() and 4 <= len(value) <= 6):
            raise ValueError(f"Muni stopcode must be 4-6 digits: {stop_code}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.muni_stop_file.write_text(f"{value}\n", encoding="utf-8")

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

    # ------------------------------------------------------------------
    # YouTube skip counter
    #
    # A monotonic counter the webapp bumps on "next video" taps. The renderer
    # (in the youtube mode) polls this on each frame and, when it sees a
    # higher value than it last observed, advances to the next video. Using
    # a counter rather than a boolean flag lets us handle rapid taps.
    def current_youtube_skip(self) -> int:
        try:
            return int(self.youtube_skip_file.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def bump_youtube_skip(self) -> int:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        current = self.current_youtube_skip()
        nxt = current + 1
        self.youtube_skip_file.write_text(f"{nxt}\n", encoding="utf-8")
        return nxt

    # ------------------------------------------------------------------
    # YouTube playlist selection (category key OR full URL)
    def current_youtube_playlist(self) -> str:
        """Return whatever the user picked (category key or URL), or ''."""
        try:
            return self.youtube_playlist_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def set_youtube_playlist(self, value: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.youtube_playlist_file.write_text(value.strip() + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # Focus timer state
    #
    # Persisted as a single JSON blob under ``focus.json`` so the mode's
    # state machine (idle / running / paused / done) can be updated
    # atomically. Every field is read live on each render frame; nothing
    # is cached, so a webapp write is visible on the LED next frame.
    # ------------------------------------------------------------------

    @property
    def focus_file(self) -> Path:
        return self.state_dir / "focus.json"

    def _focus_defaults(self) -> dict:
        return {
            # "idle" | "running" | "paused"
            # "done" is derived at render time from a running timer whose
            # remaining seconds have gone <= 0; it is not persisted here.
            "mode": "idle",
            # Wall-time epoch when the current running window started. On a
            # pause+resume we advance start_epoch so ``now - start_epoch``
            # stays the elapsed time and the digits keep counting from where
            # the user paused.
            "start_epoch": 0.0,
            # Total requested session length. When mode==idle this is the
            # duration that Start will use if you tap it with no changes.
            "duration_sec": 25 * 60,
            # Elapsed carry from prior pause segments. When paused, we
            # snapshot elapsed_at_pause into carry_sec so that on resume
            # start_epoch is set to now() and elapsed = (now - start_epoch)
            # + carry_sec reconstructs the true elapsed time.
            "carry_sec": 0.0,
            # Last selected preset in minutes (drives the idle-state chip).
            "last_preset_min": 25,
            # User-supplied label that scrolls under the digits.
            "label": "",
        }

    def focus_state(self) -> dict:
        """Read the current focus-timer state, filling in defaults on miss.

        The state file is optional: a fresh Pi has never touched the focus
        mode and returns to defaults transparently. A malformed file (from
        a partial write during a power cut, say) also falls back to
        defaults rather than crashing the renderer.
        """
        try:
            raw = self.focus_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("focus state is not a dict")
        except (OSError, ValueError, json.JSONDecodeError):
            return self._focus_defaults()
        merged = self._focus_defaults()
        for key in merged:
            if key in data:
                merged[key] = data[key]
        # Coerce types defensively.
        try:
            merged["start_epoch"] = float(merged["start_epoch"])
            merged["duration_sec"] = max(60, int(merged["duration_sec"]))
            merged["carry_sec"] = max(0.0, float(merged["carry_sec"]))
            merged["last_preset_min"] = max(1, int(merged["last_preset_min"]))
            merged["label"] = str(merged["label"])[:80]
            if merged["mode"] not in {"idle", "running", "paused"}:
                merged["mode"] = "idle"
        except (TypeError, ValueError):
            return self._focus_defaults()
        return merged

    def _write_focus_state(self, state: dict) -> None:
        """Atomically write the focus state blob.

        Uses a temp-file + rename so a crashed webapp mid-write can never
        leave the file half-written. The renderer reads this file every
        frame; a torn read would flicker the mode.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.focus_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.focus_file)

    def focus_start(self, duration_sec: int, label: str = "") -> dict:
        """Start a new timer. Overwrites any existing running/paused state.

        Called from the webapp when the user hits Start. Also called on
        preset taps that follow a completed session, so a user can go
        straight from "DONE" to their next 25-minute block with one tap.
        """
        duration = max(60, min(int(duration_sec), 8 * 60 * 60))
        state = self.focus_state()
        state.update({
            "mode": "running",
            "start_epoch": self.now().timestamp(),
            "duration_sec": duration,
            "carry_sec": 0.0,
            "last_preset_min": max(1, duration // 60),
            "label": str(label or "")[:80],
        })
        self._write_focus_state(state)
        return state

    def focus_pause(self) -> dict:
        """Pause a running timer; freeze the current elapsed into carry."""
        state = self.focus_state()
        if state["mode"] != "running":
            return state
        now = self.now().timestamp()
        elapsed = (now - state["start_epoch"]) + state["carry_sec"]
        state["mode"] = "paused"
        state["carry_sec"] = max(0.0, elapsed)
        state["start_epoch"] = now  # not used while paused, but kept fresh
        self._write_focus_state(state)
        return state

    def focus_resume(self) -> dict:
        """Resume a paused timer; start_epoch becomes now so elapsed picks up."""
        state = self.focus_state()
        if state["mode"] != "paused":
            return state
        state["mode"] = "running"
        state["start_epoch"] = self.now().timestamp()
        self._write_focus_state(state)
        return state

    def focus_reset(self) -> dict:
        """Stop and return to idle, keeping the last preset and label."""
        state = self.focus_state()
        state["mode"] = "idle"
        state["start_epoch"] = 0.0
        state["carry_sec"] = 0.0
        self._write_focus_state(state)
        return state

    # Alias for the renderer's self-transition when the DONE hold expires.
    focus_reset_to_idle = focus_reset

    def focus_nudge(self, delta_sec: int) -> dict:
        """Add or subtract time from the currently running/paused timer.

        Positive delta extends the current session; negative shortens. The
        timer never goes below 60 s total (so a user can't accidentally
        press -5 four times and produce a negative timer).
        """
        state = self.focus_state()
        if state["mode"] not in {"running", "paused"}:
            return state
        new_duration = max(60, state["duration_sec"] + int(delta_sec))
        state["duration_sec"] = new_duration
        self._write_focus_state(state)
        return state

    def focus_set_label(self, label: str) -> dict:
        state = self.focus_state()
        state["label"] = str(label or "")[:80]
        self._write_focus_state(state)
        return state

    def focus_last_preset_min(self) -> int:
        return self.focus_state()["last_preset_min"]

    # ------------------------------------------------------------------
    # World clock
    # ------------------------------------------------------------------
    #
    # Persisted as ``worldclock.json`` -- a list of ``{"label": str, "tz": str}``
    # entries. The renderer only ever consumes the first three; the JSON list
    # is kept flexible so the webapp can round-trip whatever the user typed
    # without silent truncation on save.

    # View mode: 'analog' shows the G3 layout (big amber dial + 2 small dials);
    # 'digital' shows the H4 layout (three big HH:MM readouts with A/P suffix).
    # Persisted alongside the city list so it survives a service restart.
    VALID_WORLDCLOCK_VIEWS = ("analog", "digital")

    @property
    def worldclock_file(self) -> Path:
        return self.state_dir / "worldclock.json"

    @property
    def worldclock_view_file(self) -> Path:
        return self.state_dir / "worldclock_view.txt"

    def current_worldclock_view(self) -> str:
        """Return the persisted view mode, defaulting to 'analog'.

        Kept in a separate tiny text file rather than embedded in
        worldclock.json so a bad edit to the city list cannot flip the view
        (and vice versa). The read is defensive: unknown value -> default.
        """
        try:
            value = self.worldclock_view_file.read_text(encoding="utf-8").strip()
        except OSError:
            return "analog"
        return value if value in self.VALID_WORLDCLOCK_VIEWS else "analog"

    def set_worldclock_view(self, view: str) -> str:
        """Persist the view mode. Raises ValueError on anything unknown."""
        if view not in self.VALID_WORLDCLOCK_VIEWS:
            raise ValueError(
                f"worldclock view must be one of {self.VALID_WORLDCLOCK_VIEWS}"
            )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.worldclock_view_file.with_suffix(".txt.tmp")
        tmp.write_text(view, encoding="utf-8")
        os.replace(tmp, self.worldclock_view_file)
        return view

    # ------------------------------------------------------------------
    # Hidden modes
    #
    # Users can hide modules they never use so the webapp mode grid and its
    # per-mode config cards don't clutter the phone view. Hidden here means
    # "don't show in the webapp" -- the panel itself just displays whichever
    # mode is currently selected, so a hidden mode that is somehow already the
    # active mode still renders. Stored as a plain newline-delimited text file
    # instead of JSON so a hand-edit on the Pi is trivial.
    # ------------------------------------------------------------------

    @property
    def hidden_modes_file(self) -> Path:
        return self.state_dir / "hidden_modes.txt"

    def current_hidden_modes(self) -> list[str]:
        """Return the persisted list of hidden modes, filtered to known modes.

        Unknown entries (from a stale config after a mode is renamed) are
        silently dropped instead of raising, so a rename doesn't brick the
        settings page.
        """
        try:
            raw = self.hidden_modes_file.read_text(encoding="utf-8")
        except OSError:
            return []
        seen: list[str] = []
        for line in raw.splitlines():
            name = line.strip()
            if name in VALID_MODES and name not in seen:
                seen.append(name)
        return seen

    def visible_modes(self) -> list[str]:
        """Return VALID_MODES minus the hidden ones, preserving order."""
        hidden = set(self.current_hidden_modes())
        return [m for m in VALID_MODES if m not in hidden]

    def set_hidden_modes(self, modes: list[str]) -> list[str]:
        """Persist the hidden-mode list. Rejects unknown names and prevents
        hiding every single mode (which would leave an empty mode grid).
        """
        cleaned: list[str] = []
        for m in modes:
            if not isinstance(m, str):
                raise ValueError("hidden modes must be strings")
            name = m.strip()
            if name not in VALID_MODES:
                raise ValueError(f"unknown mode: {m!r}")
            if name not in cleaned:
                cleaned.append(name)
        if len(cleaned) >= len(VALID_MODES):
            raise ValueError("at least one mode must stay visible")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.hidden_modes_file.with_suffix(".txt.tmp")
        tmp.write_text("\n".join(cleaned), encoding="utf-8")
        os.replace(tmp, self.hidden_modes_file)
        return cleaned

    def _worldclock_defaults(self) -> list[dict]:
        # Kept in sync with modes.worldclock.DEFAULT_CITIES; duplicated here so
        # the config file has no import dependency on the modes package (which
        # would create a circular import at startup).
        return [
            {"label": "SF", "tz": "America/Los_Angeles"},
            {"label": "NYC", "tz": "America/New_York"},
            {"label": "LON", "tz": "Europe/London"},
        ]

    def current_worldclock_cities(self) -> list[dict]:
        """Return the persisted world-clock city list, or the defaults.

        Falls back to defaults on any read/parse error so a corrupted state
        file cannot blank out the mode -- consistent with focus_state's
        behaviour.
        """
        try:
            raw = self.worldclock_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("worldclock state is not a list")
        except (OSError, ValueError, json.JSONDecodeError):
            return self._worldclock_defaults()
        cleaned: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label", "")).strip()[:6]
            tz = str(entry.get("tz", "")).strip()[:64]
            if not label or not tz:
                continue
            cleaned.append({"label": label, "tz": tz})
        return cleaned or self._worldclock_defaults()

    def set_worldclock_cities(self, cities: list[dict]) -> list[dict]:
        """Persist the world-clock city list. Caps at 3 saved entries.

        Validates each entry has a non-empty label and timezone. The label is
        clamped to 6 characters -- the panel can render up to 6 SMALL chars in
        a 42-px slot, and anything longer would clip regardless of aesthetic
        intent. Bogus timezone strings are accepted here and dealt with at
        render time (the mode falls back to the local system clock so the
        panel keeps rendering).
        """
        if not isinstance(cities, list):
            raise ValueError("cities payload must be a list")
        normalised: list[dict] = []
        for entry in cities[:3]:
            if not isinstance(entry, dict):
                raise ValueError("each city must be an object")
            label = str(entry.get("label", "")).strip()
            tz = str(entry.get("tz", "")).strip()
            if not label:
                raise ValueError("city label is required")
            if not tz:
                raise ValueError("city timezone is required")
            if len(label) > 6:
                raise ValueError("city label is too long (max 6 chars)")
            if len(tz) > 64:
                raise ValueError("city timezone is unreasonably long")
            normalised.append({"label": label, "tz": tz})
        if not normalised:
            raise ValueError("at least one city is required")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.worldclock_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(normalised), encoding="utf-8")
        os.replace(tmp, self.worldclock_file)
        return normalised


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
    # CURRENCY_PAIRS accepts a comma-separated list of BASE/QUOTE tokens, e.g.
    # ``USD/JPY,USD/EUR,USD/CNY``. Malformed tokens (missing slash, non-alpha)
    # are silently dropped rather than raised: an obscure typo must not knock
    # the panel offline, and the currency mode already handles an empty list
    # by falling back to its class defaults via the dataclass.
    _raw_pairs = os.getenv("CURRENCY_PAIRS", "USD/JPY,USD/EUR,USD/CNY")
    currency_pairs = _parse_currency_pairs(_raw_pairs) or (
        ("USD", "JPY"),
        ("USD", "EUR"),
        ("USD", "CNY"),
    )
    # Default seeds the El Camino warehouse (South San Francisco, id 475), the
    # closest Costco with a gas station to John's SF apartment. A malformed
    # env var falls back to the same default rather than an empty rotation.
    _raw_costco = os.getenv("COSTCO_WAREHOUSES", "475")
    costco_warehouses = _parse_costco_warehouses(_raw_costco) or ("475",)
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
        currency_pairs=currency_pairs,
        costco_warehouses=costco_warehouses,
        quake_alert_enabled=os.getenv("QUAKE_ALERT_ENABLED", "true").strip().lower() in {"1", "true", "yes"},
        quake_alert_min_mag=_parse_float_env("QUAKE_ALERT_MIN_MAG", 3.5),
        quake_alert_region=os.getenv("QUAKE_ALERT_REGION", "California").strip(),
        quake_alert_dwell_seconds=max(15, _parse_int_env("QUAKE_ALERT_DWELL_SECONDS", 120)),
        bart_station=os.getenv("BART_STATION", "EMBR").strip().upper() or "EMBR",
        muni_stop_code=os.getenv("MUNI_STOP_CODE", "").strip(),
        bike_station_id=os.getenv("BIKE_STATION_ID", "").strip(),
        nametag_name=os.getenv("NAMETAG_NAME", "").strip(),
        nametag_color=(os.getenv("NAMETAG_COLOR", "").strip() or _DEFAULT_NAMETAG_HEX),
        nametag_font=(os.getenv("NAMETAG_FONT", "").strip().lower() or "spleen"),
        finnhub_api_key=(
            os.getenv("FINNHUB_API_KEY", "").strip()
            or "d6jjqthr01qkvh5q7fd0d6jjqthr01qkvh5q7fdg"
        ),
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
        spotify_redirect_uri=(
            os.getenv("SPOTIFY_REDIRECT_URI", "").strip()
            or "http://127.0.0.1:8080/spotify/callback"
        ),
        weather_lat=os.getenv("WEATHER_LAT", ""),
        weather_lon=os.getenv("WEATHER_LON", ""),
        weather_zip=os.getenv("WEATHER_ZIP", "").strip(),
        weather_user_agent=os.getenv(
            "WEATHER_USER_AGENT", "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"
        ),
        timezone=os.getenv("TICKER_TIMEZONE", ""),
        clock_24h=os.getenv("TICKER_CLOCK_24H", "false").strip().lower() in {"1", "true", "yes"},
        state_dir=_first_writable_state_dir(),
    )
