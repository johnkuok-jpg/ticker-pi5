# MIT License — Copyright (c) 2026 John Kuok
"""Minimal Flask app for selecting ticker modes and brightness."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from ticker import bart, baywheels, muni, net, spotify as spotify_client
from ticker.config import (
    MAX_COSTCO_WAREHOUSES,
    MAX_CURRENCY_PAIRS,
    MAX_SYMBOLS,
    VALID_MODES,
    load_config,
)
from ticker.modes.youtube import (
    CATEGORIES as YT_CATEGORIES,
    DEFAULT_CATEGORY as YT_DEFAULT_CATEGORY,
)
from ticker.modes.worldclock_cities import (
    ALIASES as WORLDCLOCK_ALIASES,
    CITIES as WORLDCLOCK_CITY_INDEX,
)


# Acronyms and multi-word display names for the mode grid + settings page.
# Anything not listed falls through to a plain capitalize() in the templates,
# which is fine for single-word modes like "stocks" or "news".
# Vibe key -> display name for the picker on the Vibes card. Derived once
# at import time so a fresh import doesn't cost a module round-trip on every
# request. Populated lazily inside the function so ``ticker.modes.vibes`` is
# imported alongside the rest of the mode registry, not before.
def _load_vibe_labels() -> dict[str, str]:
    from ticker.modes.vibes import vibe_labels
    return vibe_labels()


_VIBE_LABELS_FOR_TEMPLATE = _load_vibe_labels()


MODE_LABELS = {
    "bart": "BART",
    "muni": "Muni",
    "aqi": "AQI",
    "bikes": "Bikes",
    "currency": "Currency",
    "costco": "Costco Gas",
    "quakes": "Quakes",
    "nametag": "Name Tag",
    "spotify": "Spotify",
    "pokemon": "Pok\u00e9mon",
    "focus": "Focus",
    "net": "Wi-Fi",
    "pihealth": "Pi Health",
    "worldclock": "World Clock",
    "youtube": "YouTube",
    "vibes": "Vibes",
    "sports": "Sports",
}


def _renderer_status(pid_file: Path) -> tuple[int | None, bool]:
    """Report the renderer's PID and whether the process is still alive.

    ``os.kill(pid, 0)`` is the standard "does this process exist?" probe, but
    the webapp runs as ``pi`` while the renderer runs as ``root``. An
    unprivileged user cannot signal a root process, so ``os.kill`` raises
    ``PermissionError`` (``errno.EPERM``) even when the PID is very much
    alive. That would show up as ``Renderer offline`` in the webapp banner
    while the LEDs are happily rendering.

    Distinguish the cases:
      * ``ESRCH`` — no such process; PID file is stale, renderer really is down.
      * ``EPERM`` — process exists, we just cannot signal it; treat as alive.
      * anything else — unreadable/missing PID file; treat as offline.
    """
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None, False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # ESRCH: PID file points at a process that no longer exists.
        return None, False
    except PermissionError:
        # EPERM: process exists but we lack permission to signal it (webapp
        # is `pi`, renderer is `root`). Existence is what we care about.
        return pid, True
    except OSError:
        return None, False
    return pid, True


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Format an (R, G, B) tuple as an uppercase #RRGGBB string for the panel."""
    r, g, b = rgb
    return f"#{r:02X}{g:02X}{b:02X}"


def _spotify_auth(config) -> spotify_client.SpotifyAuth:  # noqa: ANN001 - Config, avoiding an import cycle
    """Build a :class:`SpotifyAuth` from the current config.

    Rebuilt per request rather than cached because Flask may run under a
    threaded server and Config is cheap to construct; anything more elaborate
    would need its own locking.
    """
    return spotify_client.SpotifyAuth(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
        redirect_uri=config.spotify_redirect_uri,
        token_file=config.spotify_token_file,
    )


def _spotify_result_page(message: str, ok: bool) -> str:
    """Minimal HTML shown to the user after the OAuth callback.

    Inlined rather than a template because the flow lands on this page exactly
    once per connect and there is no shared header/nav. The auto-close hint
    only fires on success so a failing callback stays visible for reading.
    """
    tone = "#34C759" if ok else "#FF3C3C"
    home_link = "<p><a href=\"/\" style=\"color:#78b6ff\">← back to the ticker</a></p>"
    return (
        "<!doctype html><html><head><meta name=\"viewport\" "
        "content=\"width=device-width,initial-scale=1\">"
        "<title>Spotify · ticker</title>"
        "<style>body{background:#0d1117;color:#eaf1ff;font-family:system-ui,sans-serif;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;"
        "padding:20px;text-align:center}"
        ".card{max-width:420px}h1{color:" + tone + ";margin:0 0 12px;font-size:1.4rem}"
        "p{margin:12px 0;line-height:1.5}</style></head><body><div class=\"card\">"
        "<h1>" + ("Connected" if ok else "Could not connect") + "</h1>"
        "<p>" + message + "</p>" + home_link + "</div></body></html>"
    )


def _extract_spotify_code(raw: str) -> str:
    """Pull the ``code`` parameter out of a pasted OAuth callback URL.

    Tolerant on purpose: users paste a full URL, a URL missing its scheme, or
    occasionally just the code. Returns "" when nothing usable is found.
    """
    text = raw.strip().strip("<>\"'")
    if "code=" in text:
        # Take everything after the first code= and stop at the next separator.
        fragment = text.split("code=", 1)[1]
        for sep in ("&", "#", " "):
            fragment = fragment.split(sep, 1)[0]
        return fragment.strip()
    # A bare code: Spotify codes are long, URL-safe, and contain no scheme or
    # slashes. Reject anything that looks like a URL so a mis-paste is caught.
    if "://" in text or "/" in text or len(text) < 20:
        return ""
    return text


def _describe_schedule(config) -> str:  # noqa: ANN001 - Config, avoiding an import cycle
    """One line telling the user what the schedule is doing, or '' if unscheduled.

    The schedule is otherwise invisible: the panel simply dims on its own and the
    slider appears to snap back later. Stating the next step makes both explainable.
    """
    upcoming = config.next_brightness_change()
    if upcoming is None:
        return ""
    level, when = upcoming
    label = "off" if level <= 0 else f"{round(level * 100)}%"
    clock = when.strftime("%-I:%M %p").lower()
    day = "" if when.date() == config.now().date() else when.strftime(" %a")
    manual = config._manual_brightness()
    active = config.scheduled_brightness()
    overridden = bool(manual and active and manual[1] > active[1].timestamp())
    prefix = "Set by hand, schedule resumes" if overridden else "Scheduled"
    return f"{prefix}: {label} at {clock}{day}"


def _asset_fingerprint(path: Path) -> str:
    """Short content hash of a static file, or "0" if it can't be read.

    Used to version static URLs. The page's markup and its stylesheet are a
    matched pair -- the JS adds a `mode-visible` class that only the current
    stylesheet knows how to render -- so serving fresh HTML against a cached
    old stylesheet hides every settings card. This is not hypothetical: the
    ticker is reachable through a Cloudflare tunnel, and both the edge and the
    browser will happily hold `style.css` from a previous deploy while fetching
    the HTML fresh.

    Hashing content rather than mtime means a `git pull` that rewrites a file
    without changing it does not needlessly bust the cache, and a restored
    older file gets its old URL back.
    """
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        # A missing or unreadable asset is the static handler's problem to
        # report; don't take the page down over a cache-busting query string.
        return "0"
    return digest[:12]


def create_app() -> Flask:
    """Build the web app without any in-process dependency on the renderer."""
    app = Flask(__name__)

    # Fingerprints are computed per request but memoised on (path, mtime_ns,
    # size), so the common case is a stat() rather than a re-hash. Recomputing
    # per request matters on the Pi: `update.sh` rewrites static files under a
    # running service, and a fingerprint captured at import time would keep
    # pointing at the previous file until someone restarted the webapp.
    static_root = Path(app.static_folder or "")
    fingerprint_cache: dict[tuple[str, int, int], str] = {}

    def static_url(filename: str) -> str:
        """`url_for('static', ...)` plus a content fingerprint query."""
        path = static_root / filename
        try:
            stat = path.stat()
            key = (filename, stat.st_mtime_ns, stat.st_size)
        except OSError:
            return url_for("static", filename=filename)
        version = fingerprint_cache.get(key)
        if version is None:
            version = _asset_fingerprint(path)
            # Keep this from growing without bound if a file is rewritten
            # repeatedly; there are only a handful of static assets.
            if len(fingerprint_cache) > 64:
                fingerprint_cache.clear()
            fingerprint_cache[key] = version
        return url_for("static", filename=filename, v=version)

    app.jinja_env.globals["static_url"] = static_url

    @app.get("/")
    def index():  # type: ignore[no-untyped-def]
        config = load_config()
        current = config.current_mode()
        weather_zip, weather_lat, weather_lon, weather_label = config.current_weather_location()
        return render_template(
            "index.html",
            modes=config.visible_modes(),
            mode_labels=MODE_LABELS,
            current_mode=current,
            # Precomputed display label so the readout matches the grid on
            # first paint, before /api/status has a chance to fill it in.
            current_mode_label=MODE_LABELS.get(current, current),
            brightness=round(config.current_brightness() * 100),
            schedule_note=_describe_schedule(config),
            flight=config.current_flight(),
            flight_airport=config.current_flight_airport(),
            stations=bart.STATIONS,
            station=config.current_bart_station(),
            muni_stop=config.current_muni_stop(),
            symbols=config.current_symbols(),
            max_symbols=MAX_SYMBOLS,
            stocks_lock=config.current_stocks_lock_symbol(),
            sports_favorites={
                "mlb": config.current_favorite_team_mlb(),
                "nhl": config.current_favorite_team_nhl(),
                "nfl": config.current_favorite_team_nfl(),
                "nba": config.current_favorite_team_nba(),
            },
            nametag_name=config.current_nametag_name(),
            nametag_color=_rgb_to_hex(config.current_nametag_color()),
            nametag_font=config.current_nametag_font(),
            spotify_configured=bool(config.spotify_client_id and config.spotify_client_secret and config.spotify_redirect_uri),
            spotify_connected=_spotify_auth(config).connected,
            focus=config.focus_state(),
            youtube_categories=YT_CATEGORIES,
            youtube_default_category=YT_DEFAULT_CATEGORY,
            youtube_selection=config.current_youtube_playlist(),
            worldclock_cities=config.current_worldclock_cities(),
            worldclock_view=config.current_worldclock_view(),
            worldclock_city_index=WORLDCLOCK_CITY_INDEX,
            worldclock_city_aliases=WORLDCLOCK_ALIASES,
            vibe=config.current_vibe(),
            vibe_labels=_VIBE_LABELS_FOR_TEMPLATE,
            quake_alert_enabled=config.quake_alert_enabled,
            quake_min_mag=config.current_quake_alert_min_mag(),
            quake_region=config.current_quake_alert_region(),
            quake_dwell_seconds=config.current_quake_alert_dwell_seconds(),
            quake_filter_min_mag=config.current_quake_filter_min_mag(),
            quake_filter_region=config.current_quake_filter_region(),
            currency_pairs=[f"{b}/{q}" for b, q in config.current_currency_pairs()],
            max_currency_pairs=MAX_CURRENCY_PAIRS,
            currency_show_change=config.current_currency_show_change(),
            currency_flag_mode=config.current_currency_flag_mode(),
            currency_flag_grid=config.current_currency_flag_grid(),
            costco_warehouses=list(config.current_costco_warehouses()),
            max_costco_warehouses=MAX_COSTCO_WAREHOUSES,
            commute_origin=config.current_commute_origin(),
            commute_destination=config.current_commute_destination(),
            commute_mode=config.current_commute_mode(),
            commute_travel_modes=("transit", "driving", "walking", "bicycling"),
            commute_api_key_present=bool(config.google_maps_api_key.strip()),
            weather_zip=weather_zip,
            weather_label=weather_label,
            weather_lat=weather_lat,
            weather_lon=weather_lon,
        )

    @app.route("/mode/<name>", methods=["GET", "POST"])
    def set_mode(name: str):  # type: ignore[no-untyped-def]
        if name not in VALID_MODES:
            return jsonify(error="unknown mode", valid_modes=VALID_MODES), 404
        config = load_config()
        config.set_mode(name)
        return jsonify(current_mode=name)

    @app.post("/brightness")
    def set_brightness():  # type: ignore[no-untyped-def]
        try:
            requested = float(request.get_json(silent=True).get("brightness", request.form.get("brightness")))
        except (AttributeError, TypeError, ValueError):
            return jsonify(error="brightness must be a number between 5 and 100"), 400
        config = load_config()
        config.set_brightness(requested / 100 if requested > 1 else requested)
        return jsonify(brightness=round(config.current_brightness() * 100))

    @app.post("/youtube/playlist")
    def youtube_playlist():  # type: ignore[no-untyped-def]
        """Change the YouTube video source.

        Accepts either a known category key (from ``YT_CATEGORIES``) or a full
        playlist URL (must start with ``http``). Anything else is rejected so
        we never persist garbage that would fall back to the default silently.
        """
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get("value", request.form.get("value", ""))).strip()
        if raw.startswith("http"):
            value = raw
        elif raw in YT_CATEGORIES:
            value = raw
        else:
            return jsonify(error="unknown category or invalid URL"), 400
        config = load_config()
        config.set_youtube_playlist(value)
        # Bump the skip counter so the mode drops any in-progress download
        # for the old category and starts fresh.
        config.bump_youtube_skip()
        config.set_mode("youtube")
        return jsonify(selection=value, current_mode=config.current_mode())

    @app.post("/youtube/next")
    def youtube_next():  # type: ignore[no-untyped-def]
        """Advance the YouTube mode to the next video.

        Bumps a monotonic counter on disk; the renderer polls the counter and
        skips whenever it changes. Switches into youtube mode too, so tapping
        "next" from any screen jumps to youtube and skips in one action.
        """
        config = load_config()
        counter = config.bump_youtube_skip()
        config.set_mode("youtube")
        return jsonify(skip=counter, current_mode=config.current_mode())

    @app.post("/flight")
    def set_flight():  # type: ignore[no-untyped-def]
        """Set the tracked flight number, and switch to the flights mode.

        Typing a flight number is an unambiguous request to see that flight, so
        the mode changes too: without this the user sets a number and nothing
        visible happens.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("flight", request.form.get("flight", ""))
        config = load_config()
        config.set_flight(str(requested))
        flight = config.current_flight()
        if flight:
            config.set_mode("flights")
        return jsonify(flight=flight, current_mode=config.current_mode())

    @app.post("/flight-airport")
    def set_flight_airport():  # type: ignore[no-untyped-def]
        """Watch a random arrival into an airport, and switch to flights mode.

        Same reasoning as the flight form: choosing what to watch is a request
        to see it. The pick itself happens in the renderer rather than here, so
        that every switch into the mode lands on a different aircraft.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("airport", request.form.get("airport", ""))
        config = load_config()
        try:
            config.set_flight_airport(str(requested))
        except ValueError:
            return jsonify(
                error="airport must be a 3 or 4 letter code",
                flight_airport=config.current_flight_airport(),
            ), 400
        airport = config.current_flight_airport()
        if airport:
            config.set_mode("flights")
        return jsonify(
            flight_airport=airport,
            flight=config.current_flight(),
            current_mode=config.current_mode(),
        )

    @app.get("/api/bikes/search")
    def bikes_search():  # type: ignore[no-untyped-def]
        """Return up to 20 Bay Wheels stations matching *q* (case-insensitive substring).

        Runs against the operator's GBFS feed. Failing quietly (empty list)
        rather than 500-ing keeps the picker responsive even when GBFS is
        briefly unreachable.
        """
        query = request.args.get("q", "").strip()
        try:
            hits = baywheels.search_stations(query, limit=20)
        except Exception:
            hits = []
        return jsonify(
            stations=[
                {
                    "id": station.station_id,
                    "name": station.name,
                    "capacity": station.capacity,
                    "lat": station.lat,
                    "lon": station.lon,
                }
                for station in hits
            ]
        )

    @app.get("/api/bikes/nearest")
    def bikes_nearest():  # type: ignore[no-untyped-def]
        """Return the Bay Wheels station closest to (lat, lon).

        The web page's browser can hand over its own geolocation, but a
        fallback to the ticker's configured weather coordinates is useful for
        the common case of setting up from a desk laptop that would otherwise
        prompt for permission.
        """
        config = load_config()
        try:
            lat = float(request.args.get("lat") or config.weather_lat)
            lon = float(request.args.get("lon") or config.weather_lon)
        except ValueError:
            return jsonify(error="lat and lon must be numbers"), 400
        try:
            station = baywheels.nearest_station(lat, lon)
        except Exception:
            station = None
        if station is None:
            return jsonify(error="no stations available"), 503
        return jsonify(
            station={
                "id": station.station_id,
                "name": station.name,
                "capacity": station.capacity,
                "lat": station.lat,
                "lon": station.lon,
            }
        )

    @app.post("/bikes")
    def set_bike_station():  # type: ignore[no-untyped-def]
        """Pick the Bay Wheels station, and switch to the bikes mode.

        Matches the flight/bart pattern: choosing what to watch is a request
        to watch it, so the panel follows rather than waiting for a tap.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("station", request.form.get("station", ""))
        config = load_config()
        try:
            config.set_bike_station(str(requested))
        except ValueError as error:
            return jsonify(error=str(error), bike_station=config.current_bike_station()), 400
        chosen = config.current_bike_station()
        if chosen:
            config.set_mode("bikes")
        return jsonify(bike_station=chosen, current_mode=config.current_mode())

    # -- SF Muni arrivals ----------------------------------------------------
    #
    # Muni stops are keyed by the 5-digit stopcode printed on every shelter
    # sign, so the fastest path is a direct code entry. For riders who don't
    # already know their code the picker exposes a name search and a nearest-
    # stop lookup that both resolve to the same code the mode ultimately reads.

    @app.get("/api/muni/search")
    def muni_search():  # type: ignore[no-untyped-def]
        """Return up to 20 Muni stops matching the name substring *q*.

        Fails quietly on network trouble because the picker still has the
        code-entry field as a fallback and a 503 here would strand the user
        who typed a search but has a printed shelter code in front of them.
        """
        query = request.args.get("q", "").strip()
        try:
            hits = muni.search_stops(query, limit=20)
        except Exception:
            hits = []
        return jsonify(
            stops=[
                {"code": stop.code, "name": stop.name, "lat": stop.lat, "lon": stop.lon}
                for stop in hits
            ]
        )

    @app.get("/api/muni/nearest")
    def muni_nearest():  # type: ignore[no-untyped-def]
        """Return the Muni stop closest to (lat, lon).

        Same fallback ladder as the Bay Wheels equivalent: the browser can
        hand over geolocation, and the ticker's configured weather coords
        pinch-hit when the browser refuses or the user is setting up from a
        desk laptop.
        """
        config = load_config()
        try:
            lat = float(request.args.get("lat") or config.weather_lat)
            lon = float(request.args.get("lon") or config.weather_lon)
        except ValueError:
            return jsonify(error="lat and lon must be numbers"), 400
        try:
            stop = muni.nearest_stop(lat, lon)
        except Exception:
            stop = None
        if stop is None:
            return jsonify(error="no stops available"), 503
        return jsonify(
            stop={"code": stop.code, "name": stop.name, "lat": stop.lat, "lon": stop.lon}
        )

    @app.post("/muni")
    def set_muni_stop():  # type: ignore[no-untyped-def]
        """Persist the chosen Muni stopcode and switch to the muni mode.

        Follows the flight/bart/bikes contract: picking what to watch is a
        request to watch it, so the panel switches modes as part of the
        same tap rather than making the user hunt for the mode grid.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("stop", request.form.get("stop", ""))
        config = load_config()
        try:
            config.set_muni_stop(str(requested))
        except ValueError as error:
            return jsonify(error=str(error), muni_stop=config.current_muni_stop()), 400
        chosen = config.current_muni_stop()
        if chosen:
            config.set_mode("muni")
        return jsonify(muni_stop=chosen, current_mode=config.current_mode())

    # -- Stocks watchlist ----------------------------------------------------
    #
    # The one mode whose settings used to live only in TICKER_SYMBOLS, which
    # meant an SSH session and a service restart to change a symbol. Both routes
    # return the whole resulting list, so the page never has to guess what the
    # state file ended up holding.

    @app.post("/stocks/add")
    def add_symbol():  # type: ignore[no-untyped-def]
        """Add a ticker to the watchlist, and switch to the stocks mode.

        Same reasoning as the station pickers: adding a symbol is a request to
        see it, so the panel follows rather than waiting for a second tap.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("symbol", request.form.get("symbol", ""))
        config = load_config()
        try:
            symbols = config.add_symbol(str(requested))
        except ValueError as error:
            return jsonify(error=str(error), symbols=list(config.current_symbols())), 400
        config.set_mode("stocks")
        return jsonify(symbols=list(symbols), current_mode=config.current_mode())

    @app.post("/stocks/remove")
    def remove_symbol():  # type: ignore[no-untyped-def]
        """Drop a ticker from the watchlist.

        Deliberately does not touch the mode: removing something is not a
        request to go and look at the panel.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("symbol", request.form.get("symbol", ""))
        config = load_config()
        try:
            symbols = config.remove_symbol(str(requested))
        except ValueError as error:
            return jsonify(error=str(error), symbols=list(config.current_symbols())), 400
        return jsonify(symbols=list(symbols), stocks_lock=config.current_stocks_lock_symbol())

    @app.post("/stocks/lock")
    def set_stocks_lock():  # type: ignore[no-untyped-def]
        """Pin the stocks card on one symbol, or clear the pin.

        Body: ``{"symbol": "AAPL"}`` to lock, ``{"symbol": ""}`` to unlock.
        Also switches to the stocks mode when a symbol is being locked, since
        the user's intent is to go look at it now.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("symbol", request.form.get("symbol", ""))
        config = load_config()
        try:
            value = config.set_stocks_lock_symbol(str(requested))
        except ValueError as error:
            return jsonify(error=str(error), stocks_lock=config.current_stocks_lock_symbol()), 400
        if value:
            config.set_mode("stocks")
        return jsonify(stocks_lock=value, current_mode=config.current_mode())

    _SPORTS_FAVORITE_SETTERS = {
        "mlb": ("set_favorite_team_mlb", "current_favorite_team_mlb"),
        "nhl": ("set_favorite_team_nhl", "current_favorite_team_nhl"),
        "nfl": ("set_favorite_team_nfl", "current_favorite_team_nfl"),
        "nba": ("set_favorite_team_nba", "current_favorite_team_nba"),
    }

    @app.post("/sports/favorite/<league>")
    def set_sports_favorite(league: str):  # type: ignore[no-untyped-def]
        """Pin one league's card on one team, or clear the pin.

        ``league`` is one of mlb/nhl/nfl/nba. Body: ``{"team": "SF"}`` to
        favorite, ``{"team": ""}`` to clear. Also switches to the sports
        mode when a team is being set -- same rationale as
        ``/stocks/lock``: tapping a team is a request to go look at their
        game now, not on the next rotation.
        """
        setters = _SPORTS_FAVORITE_SETTERS.get(league)
        if setters is None:
            return jsonify(error=f"unknown league {league!r}"), 404
        setter_name, getter_name = setters
        payload = request.get_json(silent=True) or {}
        requested = payload.get("team", request.form.get("team", ""))
        config = load_config()
        getter = getattr(config, getter_name)
        try:
            value = getattr(config, setter_name)(str(requested))
        except ValueError as error:
            return jsonify(error=str(error), sports_favorite=getter()), 400
        if value:
            config.set_mode("sports")
        return jsonify(sports_favorite=value, league=league, current_mode=config.current_mode())

    @app.post("/currency/add")
    def add_currency_pair():  # type: ignore[no-untyped-def]
        """Add a BASE/QUOTE pair to the currency list, switch to currency mode.

        Same reasoning as ``/stocks/add``: adding is a request to see it, so
        the panel follows the tap rather than waiting for a second one.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("pair", request.form.get("pair", ""))
        config = load_config()
        try:
            pairs = config.add_currency_pair(str(requested))
        except ValueError as error:
            return (
                jsonify(
                    error=str(error),
                    pairs=[f"{b}/{q}" for b, q in config.current_currency_pairs()],
                ),
                400,
            )
        config.set_mode("currency")
        return jsonify(
            pairs=[f"{b}/{q}" for b, q in pairs],
            current_mode=config.current_mode(),
        )

    @app.post("/currency/remove")
    def remove_currency_pair():  # type: ignore[no-untyped-def]
        """Drop a pair from the currency list. Does not switch modes.

        Mirrors ``/stocks/remove``: removing something is not a request to go
        and look at the panel, only a request to stop showing it there.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("pair", request.form.get("pair", ""))
        config = load_config()
        try:
            pairs = config.remove_currency_pair(str(requested))
        except ValueError as error:
            return (
                jsonify(
                    error=str(error),
                    pairs=[f"{b}/{q}" for b, q in config.current_currency_pairs()],
                ),
                400,
            )
        return jsonify(pairs=[f"{b}/{q}" for b, q in pairs])

    @app.post("/costco/add")
    def add_costco_warehouse():  # type: ignore[no-untyped-def]
        """Add a Costco warehouse ID to the gas-price rotation.

        Same tap-follows pattern as ``/currency/add``: adding a warehouse is
        a "show it to me" gesture, so we switch to costco mode on success.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("warehouse", request.form.get("warehouse", ""))
        config = load_config()
        try:
            warehouses = config.add_costco_warehouse(str(requested))
        except ValueError as error:
            return (
                jsonify(
                    error=str(error),
                    warehouses=list(config.current_costco_warehouses()),
                ),
                400,
            )
        config.set_mode("costco")
        return jsonify(
            warehouses=list(warehouses),
            current_mode=config.current_mode(),
        )

    @app.post("/costco/remove")
    def remove_costco_warehouse():  # type: ignore[no-untyped-def]
        """Drop a warehouse from the Costco rotation. Does not switch modes."""
        payload = request.get_json(silent=True) or {}
        requested = payload.get("warehouse", request.form.get("warehouse", ""))
        config = load_config()
        try:
            warehouses = config.remove_costco_warehouse(str(requested))
        except ValueError as error:
            return (
                jsonify(
                    error=str(error),
                    warehouses=list(config.current_costco_warehouses()),
                ),
                400,
            )
        return jsonify(warehouses=list(warehouses))

    @app.post("/currency/show-change")
    def set_currency_show_change():  # type: ignore[no-untyped-def]
        """Toggle the 24-hour change column on the currency card.

        Body: ``{"enabled": true|false}``. Does NOT switch to currency mode:
        the setting affects what the card looks like when you open it, but
        toggling the setting itself is not usually a "go look at it now"
        gesture. Symmetric with ``/quakes/filter``, which does switch because
        the intent there is to see the newly-filtered list.
        """
        payload = request.get_json(silent=True) or {}
        enabled = bool(payload.get("enabled", True))
        config = load_config()
        value = config.set_currency_show_change(enabled)
        return jsonify(currency_show_change=value)

    @app.post("/currency/flag-mode")
    def set_currency_flag_mode():  # type: ignore[no-untyped-def]
        """Toggle the two-row flag layout on the currency card.

        Body: ``{"enabled": true|false}``. Same non-switching philosophy as
        ``/currency/show-change`` -- flipping the layout is a config edit,
        not necessarily an intent to look at the panel right now, so this
        endpoint just persists the setting.
        """
        payload = request.get_json(silent=True) or {}
        enabled = bool(payload.get("enabled", True))
        config = load_config()
        value = config.set_currency_flag_mode(enabled)
        return jsonify(currency_flag_mode=value)

    @app.post("/currency/flag-grid")
    def set_currency_flag_grid():  # type: ignore[no-untyped-def]
        """Toggle the 2x2 arrangement of the flag layout.

        Body: ``{"enabled": true|false}``. Only takes effect on the panel
        when flag mode is on, there are 3-4 pairs, and show-change is off
        -- the mode enforces those preconditions at render time and
        silently falls back to the stacked layout otherwise. Same
        non-switching philosophy as the sister toggles.
        """
        payload = request.get_json(silent=True) or {}
        enabled = bool(payload.get("enabled", True))
        config = load_config()
        value = config.set_currency_flag_grid(enabled)
        return jsonify(currency_flag_grid=value)

    # -- Weather / air-quality location ---------------------------------------
    #
    # Both weather modes need coordinates, which nobody knows offhand for the
    # place they're standing in. A ZIP is the one location token everyone can
    # recall, so the picker takes a ZIP, geocodes it server-side, and stores
    # the resolved coordinates -- the render path never geocodes.
    @app.post("/weather/zip")
    def set_weather_zip():  # type: ignore[no-untyped-def]
        """Aim the weather and air-quality modes at a US ZIP code.

        Body: ``{"zip": "94103"}``. An empty string clears the override and
        falls back to ``WEATHER_LAT``/``WEATHER_LON`` from ``.env``.

        Unlike the bike/BART pickers this does NOT switch modes: the same
        coordinates feed two different modes (weather and air), so there is
        no single "the mode you meant" to jump to. The caller decides.

        Responds 400 with the unchanged current location on a bad ZIP, so a
        typo never blanks a working forecast.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("zip", request.form.get("zip", ""))
        config = load_config()
        try:
            config.set_weather_zip(str(requested))
        except ValueError as error:
            zip_code, lat, lon, label = config.current_weather_location()
            return jsonify(
                error=str(error),
                weather_zip=zip_code,
                weather_lat=lat,
                weather_lon=lon,
                weather_label=label,
            ), 400

        zip_code, lat, lon, label = config.current_weather_location()
        return jsonify(
            weather_zip=zip_code,
            weather_lat=lat,
            weather_lon=lon,
            weather_label=label,
        )

    @app.post("/nametag")
    def set_nametag():  # type: ignore[no-untyped-def]
        """Update the desk-plate name and/or color, and switch to nametag mode.

        Both fields are optional in one request: sending only 'name' leaves the
        color alone, and vice versa. Any successful save switches the panel to
        nametag mode so the coworker sees the update immediately.
        """
        payload = request.get_json(silent=True) or {}
        raw_name = payload.get("name", request.form.get("name"))
        raw_color = payload.get("color", request.form.get("color"))
        raw_font = payload.get("font", request.form.get("font"))
        config = load_config()

        changed = False
        try:
            if raw_name is not None:
                config.set_nametag_name(str(raw_name))
                changed = True
            if raw_color is not None and str(raw_color).strip():
                config.set_nametag_color(str(raw_color))
                changed = True
            if raw_font is not None and str(raw_font).strip():
                config.set_nametag_font(str(raw_font))
                changed = True
        except ValueError as error:
            return jsonify(
                error=str(error),
                name=config.current_nametag_name(),
                color=_rgb_to_hex(config.current_nametag_color()),
                font=config.current_nametag_font(),
            ), 400

        if changed:
            config.set_mode("nametag")

        return jsonify(
            name=config.current_nametag_name(),
            color=_rgb_to_hex(config.current_nametag_color()),
            font=config.current_nametag_font(),
            current_mode=config.current_mode(),
        )

    @app.post("/quakes/filter")
    def set_quake_filter():  # type: ignore[no-untyped-def]
        """Update the display filter for the passive quakes mode.

        Distinct endpoint from ``/quakes`` (which controls the auto-switch
        alert) because the two settings are semantically different -- see
        ``config.quake_filter_file`` for the reasoning. Accepts an optional
        subset of ``{min_mag, region}``. Switches into quakes mode on success
        so the user can see the effect immediately.
        """
        payload = request.get_json(silent=True) or {}
        raw_mag = payload.get("min_mag", request.form.get("min_mag"))
        raw_region = payload.get("region", request.form.get("region"))
        config = load_config()
        try:
            config.set_quake_filter(
                min_mag=raw_mag if raw_mag is not None and str(raw_mag).strip() != "" else None,
                region=raw_region if raw_region is not None else None,
            )
        except ValueError as error:
            return jsonify(
                error=str(error),
                min_mag=config.current_quake_filter_min_mag(),
                region=config.current_quake_filter_region(),
            ), 400
        # Switch to quakes mode so the user sees the filter applied without a
        # manual mode switch. Mirrors the pattern set by /nametag and /bart.
        config.set_mode("quakes")
        return jsonify(
            min_mag=config.current_quake_filter_min_mag(),
            region=config.current_quake_filter_region(),
            current_mode=config.current_mode(),
        )

    @app.post("/quakes")
    def set_quakes():  # type: ignore[no-untyped-def]
        """Update the auto-switch alert settings (magnitude, region, dwell).

        Any field may be omitted -- send only the ones you want to change.
        The magnitude field is a float clamped 2.5-9.9; region is any USGS
        "place" substring, or empty for worldwide; dwell is seconds, 15-900.

        The response always echoes the *current effective* settings so the
        webapp can update its own inputs without a second GET.
        """
        payload = request.get_json(silent=True) or {}
        raw_mag = payload.get("min_mag", request.form.get("min_mag"))
        raw_region = payload.get("region", request.form.get("region"))
        raw_dwell = payload.get("dwell_seconds", request.form.get("dwell_seconds"))
        config = load_config()
        try:
            config.set_quake_alert_settings(
                min_mag=raw_mag if raw_mag is not None and str(raw_mag).strip() != "" else None,
                region=raw_region if raw_region is not None else None,
                dwell_seconds=raw_dwell if raw_dwell is not None and str(raw_dwell).strip() != "" else None,
            )
        except ValueError as error:
            return jsonify(
                error=str(error),
                min_mag=config.current_quake_alert_min_mag(),
                region=config.current_quake_alert_region(),
                dwell_seconds=config.current_quake_alert_dwell_seconds(),
            ), 400
        return jsonify(
            min_mag=config.current_quake_alert_min_mag(),
            region=config.current_quake_alert_region(),
            dwell_seconds=config.current_quake_alert_dwell_seconds(),
        )

    @app.post("/bart")
    def set_bart_station():  # type: ignore[no-untyped-def]
        """Choose the BART station, and switch to the departures mode.

        Same reasoning as the flight form: picking a station is a request to see
        it, so the panel follows rather than waiting for a second tap.
        """
        payload = request.get_json(silent=True) or {}
        requested = payload.get("station", request.form.get("station", ""))
        config = load_config()
        try:
            config.set_bart_station(str(requested))
        except ValueError:
            return jsonify(error="unknown station", station=config.current_bart_station()), 400
        station = config.current_bart_station()
        config.set_mode("bart")
        return jsonify(
            station=station,
            # BART's own casing, not title case: .title() turns MacArthur into
            # "Macarthur" and 12th St. into "12Th St.".
            station_name=bart.STATION_NAMES.get(station, station),
            current_mode=config.current_mode(),
        )

    # -- Focus timer --------------------------------------------------------
    #
    # Small state-machine API. Everything writes atomically to focus.json via
    # Config helpers; render() re-reads on every frame so a POST here is
    # visible on the LED next frame. All routes return the fresh state so
    # the webapp can update its own countdown without a second GET.

    def _focus_response(state):  # type: ignore[no-untyped-def]
        return jsonify(focus=state)

    @app.post("/focus/start")
    def focus_start():  # type: ignore[no-untyped-def]
        payload = request.get_json(silent=True) or {}
        try:
            # Accept either seconds or minutes; UI sends seconds, but a curl
            # user is more likely to type minutes. If both are present we
            # trust seconds because it's the precise value.
            if "duration_sec" in payload:
                duration = int(payload["duration_sec"])
            elif "duration_min" in payload:
                duration = int(payload["duration_min"]) * 60
            else:
                duration = 25 * 60
        except (TypeError, ValueError):
            return jsonify(error="duration must be a positive integer"), 400
        label = str(payload.get("label", "") or "")
        config = load_config()
        return _focus_response(config.focus_start(duration, label))

    @app.post("/focus/pause")
    def focus_pause():  # type: ignore[no-untyped-def]
        config = load_config()
        return _focus_response(config.focus_pause())

    @app.post("/focus/resume")
    def focus_resume():  # type: ignore[no-untyped-def]
        config = load_config()
        return _focus_response(config.focus_resume())

    @app.post("/focus/reset")
    def focus_reset():  # type: ignore[no-untyped-def]
        config = load_config()
        return _focus_response(config.focus_reset())

    @app.post("/focus/nudge")
    def focus_nudge():  # type: ignore[no-untyped-def]
        payload = request.get_json(silent=True) or {}
        try:
            delta = int(payload.get("delta_sec", 0))
        except (TypeError, ValueError):
            return jsonify(error="delta_sec must be an integer"), 400
        config = load_config()
        return _focus_response(config.focus_nudge(delta))

    @app.post("/focus/label")
    def focus_label():  # type: ignore[no-untyped-def]
        payload = request.get_json(silent=True) or {}
        config = load_config()
        return _focus_response(config.focus_set_label(str(payload.get("label", "") or "")))

    @app.get("/api/focus")
    def focus_state_api():  # type: ignore[no-untyped-def]
        config = load_config()
        return _focus_response(config.focus_state())

    # Commute routes.
    #
    # Three endpoints, one purpose each:
    #   POST /commute/addresses -- persist home + work text
    #   POST /commute/mode      -- persist transit/driving/walking/bicycling
    #   POST /commute/route     -- hit Google Directions once and cache result
    #   GET  /api/commute       -- return the cached result for the webapp echo
    #
    # /commute/route is intentionally the ONLY thing that spends API quota.
    # The renderer never fetches on its own; a user has to tap.

    def _commute_snapshot(config):  # type: ignore[no-untyped-def]
        # Instantiate the mode just to reuse its state-file readers. No
        # network call happens here -- state() is a file read.
        from ticker.modes.commute import CommuteMode
        mode = CommuteMode(config)
        return {
            "origin": config.current_commute_origin(),
            "destination": config.current_commute_destination(),
            "mode": config.current_commute_mode(),
            "last": mode.state(),
            "api_key_present": bool(config.google_maps_api_key.strip()),
        }

    @app.post("/commute/addresses")
    def commute_addresses():  # type: ignore[no-untyped-def]
        payload = request.get_json(silent=True) or {}
        origin = str(payload.get("origin", "") or "")
        destination = str(payload.get("destination", "") or "")
        config = load_config()
        try:
            config.set_commute_addresses(origin, destination)
        except ValueError as err:
            return jsonify(error=str(err), commute=_commute_snapshot(config)), 400
        return jsonify(commute=_commute_snapshot(config))

    @app.post("/commute/mode")
    def commute_mode():  # type: ignore[no-untyped-def]
        payload = request.get_json(silent=True) or {}
        travel = str(payload.get("mode", "") or "")
        config = load_config()
        try:
            config.set_commute_mode(travel)
        except ValueError as err:
            return jsonify(error=str(err), commute=_commute_snapshot(config)), 400
        return jsonify(commute=_commute_snapshot(config))

    @app.post("/commute/route")
    def commute_route():  # type: ignore[no-untyped-def]
        # Optional per-tap mode override so the UI can send the mode the
        # user picked without waiting for /commute/mode to round-trip first.
        # If absent, fetch() falls back to the persisted mode.
        payload = request.get_json(silent=True) or {}
        travel = payload.get("mode")
        config = load_config()
        # Tapping Route with a mode different from the persisted one also
        # persists the pick -- otherwise the UI radio and the card would
        # disagree on the next render.
        if travel and str(travel).strip():
            try:
                config.set_commute_mode(str(travel))
            except ValueError as err:
                return jsonify(error=str(err), commute=_commute_snapshot(config)), 400
        from ticker.modes.commute import CommuteMode
        mode = CommuteMode(config)
        mode.fetch()  # populates the state file; return value ignored -- read it back
        # Also flip the current mode to "commute" so the tap surfaces on
        # the LED without a separate mode click, matching how the flight
        # form flips to flights and the worldclock form flips to worldclock.
        config.set_mode("commute")
        snapshot = _commute_snapshot(config)
        snapshot["current_mode"] = config.current_mode()
        return jsonify(commute=snapshot)

    @app.get("/api/commute")
    def commute_state_api():  # type: ignore[no-untyped-def]
        config = load_config()
        return jsonify(commute=_commute_snapshot(config))

    #: Human-readable reasons for a failed autocomplete. The form must stay
    #: usable when this is down, so these are shown as a hint under the input
    #: rather than as an error -- typing the full address by hand still works.
    _PLACES_REASONS = {
        "no_key": "No Google Maps API key set.",
        "not_enabled": (
            "Enable \u201cPlaces API (New)\u201d on the Google Cloud project "
            "and add it to the API key\u2019s restrictions."
        ),
        "network": "No network.",
        "api": "Places API error.",
    }

    @app.get("/api/commute/places")
    def commute_places_api():  # type: ignore[no-untyped-def]
        """Address suggestions for the commute form.

        Proxied through the Pi rather than called from the browser so the
        Google API key stays server-side. A key shipped to the page is a key
        anyone on the LAN (or anyone past Cloudflare Access) can read from view
        source and spend, and this one is billable.

        Always returns 200. A failure here is a degraded input, not a broken
        page: the JSON carries an empty list plus a reason, and the form falls
        back to plain typing.
        """
        from ticker.modes.commute import (
            AUTOCOMPLETE_MIN_CHARS,
            AutocompleteUnavailable,
            autocomplete_addresses,
        )

        query = (request.args.get("q") or "").strip()
        if len(query) < AUTOCOMPLETE_MIN_CHARS:
            return jsonify(suggestions=[], query=query)
        config = load_config()
        try:
            suggestions = autocomplete_addresses(query, config.google_maps_api_key)
        except AutocompleteUnavailable as err:
            return jsonify(
                suggestions=[],
                query=query,
                reason=err.reason,
                message=_PLACES_REASONS.get(err.reason, "Autocomplete unavailable."),
            )
        return jsonify(suggestions=suggestions, query=query)

    @app.post("/worldclock")
    def set_worldclock():  # type: ignore[no-untyped-def]
        """Update the world-clock city list and switch to that mode.

        Accepts a JSON list of ``{"label", "tz"}`` entries. Same reasoning as
        the flight/nametag forms: saving cities is an explicit request to see
        them, so we flip the mode too.
        """
        payload = request.get_json(silent=True) or {}
        cities = payload.get("cities", [])
        config = load_config()
        try:
            saved = config.set_worldclock_cities(list(cities))
        except ValueError as err:
            return jsonify(
                error=str(err),
                cities=config.current_worldclock_cities(),
            ), 400
        config.set_mode("worldclock")
        return jsonify(cities=saved, current_mode=config.current_mode())

    @app.post("/vibes/vibe")
    def set_vibe_endpoint():  # type: ignore[no-untyped-def]
        """Change the active vibe and switch the panel to Vibes mode.

        Distinct from worldclock's view toggle, which is a preference-
        only change: a vibe pick is the whole reason the user opened
        the card, so we auto-flip to the vibes mode so they see the
        result immediately.
        """
        payload = request.get_json(silent=True) or {}
        vibe = str(payload.get("vibe", "")).strip()
        config = load_config()
        try:
            saved = config.set_vibe(vibe)
        except ValueError as err:
            return jsonify(
                error=str(err),
                vibe=config.current_vibe(),
            ), 400
        config.set_mode("vibes")
        return jsonify(vibe=saved, current_mode="vibes")

    @app.post("/worldclock/view")
    def set_worldclock_view():  # type: ignore[no-untyped-def]
        """Flip between the analog and digital world-clock views.

        This is a preference change rather than a data change, so we do NOT
        auto-switch to the worldclock mode. The renderer picks up the new
        view on its next frame if the mode is already active; otherwise the
        view will take effect the next time the user selects the mode.
        """
        payload = request.get_json(silent=True) or {}
        view = str(payload.get("view", "")).strip()
        config = load_config()
        try:
            saved = config.set_worldclock_view(view)
        except ValueError as err:
            return jsonify(
                error=str(err),
                view=config.current_worldclock_view(),
            ), 400
        return jsonify(view=saved)

    # -- Settings ------------------------------------------------------------
    #
    # A separate page for the destructive/rare controls (Wi-Fi join, module
    # show/hide) so a mis-tap on the always-live panel of mode buttons can
    # never change the network or hide a mode by accident. Wi-Fi in
    # particular is the one screen a user reaches while the ticker is
    # unreachable in the normal sense -- joined to its own setup hotspot
    # with no internet behind it -- so it must stay reachable in that state.
    # Live Wi-Fi state is fetched client-side from /api/wifi.

    @app.get("/settings")
    def settings_page():  # type: ignore[no-untyped-def]
        """Combined settings page: module visibility + Wi-Fi.

        The legacy ``/wifi`` URL is preserved as a redirect below so QR
        codes on the panel (and any bookmarks) keep working.
        """
        config = load_config()
        left_gain, right_gain = config.current_panel_calibration()
        return render_template(
            "settings.html",
            available=net.available(),
            setup_ssid=net.HOTSPOT_SSID,
            all_modes=VALID_MODES,
            hidden_modes=config.current_hidden_modes(),
            mode_labels=MODE_LABELS,
            panel_left_gain=left_gain,
            panel_right_gain=right_gain,
            panel_test=config.current_panel_calibration_test(),
        )

    @app.get("/wifi")
    def wifi_page_legacy():  # type: ignore[no-untyped-def]
        """Preserve the old /wifi URL so the panel's hotspot QR keeps working."""
        return redirect("/settings", code=301)

    @app.post("/settings/modules")
    def set_hidden_modes_endpoint():  # type: ignore[no-untyped-def]
        """Persist which modes are hidden from the webapp.

        Body: ``{"hidden": ["pokemon", "nametag", ...]}`` -- the full list of
        modes to hide. ``set_hidden_modes`` validates and refuses to hide
        everything, returning the error verbatim to the client so the UI can
        surface it.
        """
        payload = request.get_json(silent=True) or {}
        hidden = payload.get("hidden", [])
        if not isinstance(hidden, list):
            return jsonify(error="hidden must be a list"), 400
        config = load_config()
        try:
            saved = config.set_hidden_modes(list(hidden))
        except ValueError as err:
            return jsonify(
                error=str(err),
                hidden=config.current_hidden_modes(),
            ), 400
        return jsonify(hidden=saved)

    @app.post("/settings/calibration")
    def set_panel_calibration_endpoint():  # type: ignore[no-untyped-def]
        """Persist per-half panel gains and the flat-grey test target.

        Body: ``{"left": 1.0, "right": 0.92, "test": false}``. Every key is
        optional so the test toggle and the sliders can post independently;
        omitting a gain leaves it at its stored value rather than resetting it.
        """
        payload = request.get_json(silent=True) or {}
        config = load_config()
        stored = config.current_panel_calibration()
        left = payload.get("left", stored[0])
        right = payload.get("right", stored[1])
        try:
            saved = config.set_panel_calibration(left, right)
        except ValueError as err:
            return jsonify(
                error=str(err),
                left=stored[0],
                right=stored[1],
            ), 400
        test = config.current_panel_calibration_test()
        if "test" in payload:
            test = config.set_panel_calibration_test(bool(payload["test"]))
        return jsonify(left=saved[0], right=saved[1], test=test)

    @app.get("/api/wifi")
    def wifi_state():  # type: ignore[no-untyped-def]
        """Current Wi-Fi state, and optionally a fresh scan.

        Scanning is opt-in via ``?scan=1`` because it takes several seconds and
        the page polls this endpoint: a poll that rescanned every time would keep
        the radio busy and reshuffle the list under the user's thumb.
        """
        status = net.status()
        payload = {
            "available": net.available(),
            "state": status.state,
            "ssid": status.ssid,
            "ip": status.ip,
            "signal": status.signal,
            "saved": net.saved_networks(),
        }
        if request.args.get("scan") in ("1", "true", "yes"):
            payload["networks"] = [
                {
                    "ssid": found.ssid,
                    "signal": found.signal,
                    "bars": found.bars,
                    "locked": found.locked,
                    "saved": found.saved,
                    "active": found.active,
                }
                for found in net.scan()
            ]
        return jsonify(**payload)

    @app.post("/wifi/join")
    def wifi_join():  # type: ignore[no-untyped-def]
        """Join a network.

        The response is sent before the switch completes where possible, but a
        successful join from the setup hotspot necessarily kills the connection
        this request arrived on, so the page is written to treat a dropped
        response as a likely success rather than an error.
        """
        payload = request.get_json(silent=True) or {}
        ssid = str(payload.get("ssid", request.form.get("ssid", ""))).strip()
        password = str(payload.get("password", request.form.get("password", "")))
        hidden = bool(payload.get("hidden", request.form.get("hidden")))
        ok, message = net.join(ssid, password, hidden=hidden)
        status = net.status()
        return jsonify(ok=ok, message=message, state=status.state, ssid=status.ssid, ip=status.ip), (
            200 if ok else 400
        )

    @app.post("/wifi/forget")
    def wifi_forget():  # type: ignore[no-untyped-def]
        payload = request.get_json(silent=True) or {}
        ssid = str(payload.get("ssid", request.form.get("ssid", ""))).strip()
        ok, message = net.forget(ssid)
        return jsonify(ok=ok, message=message, saved=net.saved_networks()), 200 if ok else 400

    @app.get("/api/status")
    def status():  # type: ignore[no-untyped-def]
        config = load_config()
        pid, alive = _renderer_status(config.pid_file)
        current = config.current_mode()
        return jsonify(
            current_mode=current,
            # Send the display label too so the front-end readout stays in
            # step with MODE_LABELS (e.g. 'BART' rather than 'bart',
            # 'World Clock' rather than 'worldclock'). Kept as a separate
            # field so old clients that read current_mode still work.
            current_mode_label=MODE_LABELS.get(current, current),
            renderer_pid=pid,
            renderer_alive=alive,
            brightness=round(config.current_brightness() * 100),
            schedule_note=_describe_schedule(config),
            flight=config.current_flight(),
            flight_airport=config.current_flight_airport(),
            station=config.current_bart_station(),
            muni_stop=config.current_muni_stop(),
            symbols=list(config.current_symbols()),
            currency_pairs=[f"{b}/{q}" for b, q in config.current_currency_pairs()],
            currency_show_change=config.current_currency_show_change(),
            currency_flag_mode=config.current_currency_flag_mode(),
            currency_flag_grid=config.current_currency_flag_grid(),
            costco_warehouses=list(config.current_costco_warehouses()),
            max_costco_warehouses=MAX_COSTCO_WAREHOUSES,
            nametag_name=config.current_nametag_name(),
            nametag_color=_rgb_to_hex(config.current_nametag_color()),
            nametag_font=config.current_nametag_font(),
            spotify_configured=bool(config.spotify_client_id and config.spotify_client_secret and config.spotify_redirect_uri),
            spotify_connected=_spotify_auth(config).connected,
            network_notice=config.network_notice(),
            focus=config.focus_state(),
        )

    # --- Spotify OAuth ------------------------------------------------
    #
    # Two routes: /spotify/connect kicks off the OAuth handshake, and
    # /spotify/callback receives Spotify's redirect. A third, /spotify/disconnect,
    # wipes the token so the user can hand the ticker to a different person.
    #
    # CSRF state is stored in a short-lived file rather than a Flask session,
    # so no SECRET_KEY setup is required. Only one user hits the Pi's webapp
    # at a time in practice; the file is single-writer and single-reader.

    @app.get("/spotify/connect")
    def spotify_connect():  # type: ignore[no-untyped-def]
        config = load_config()
        auth = _spotify_auth(config)
        if not auth.configured:
            return jsonify(error="Spotify credentials not configured on this ticker."), 400
        state = spotify_client.new_state_token()
        state_path = config.state_dir / "spotify_oauth_state"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        # Store token + creation time so a leftover token from an abandoned
        # flow does not accept a callback hours later.
        state_path.write_text(f"{state}\n{int(time.time())}\n", encoding="utf-8")
        try:
            state_path.chmod(0o600)
        except OSError:
            pass
        return redirect(auth.build_authorize_url(state))

    @app.get("/spotify/callback")
    def spotify_callback():  # type: ignore[no-untyped-def]
        config = load_config()
        auth = _spotify_auth(config)
        error = request.args.get("error")
        if error:
            # User denied, or Spotify refused. Show a plain page explaining it.
            return _spotify_result_page(f"Spotify denied the request: {error}", ok=False)
        code = request.args.get("code", "").strip()
        received_state = request.args.get("state", "").strip()
        state_path = config.state_dir / "spotify_oauth_state"
        try:
            stored_state, ts_line = state_path.read_text(encoding="utf-8").splitlines()[:2]
            stored_ts = int(ts_line)
        except (OSError, ValueError):
            return _spotify_result_page("OAuth state not found. Restart the connect flow.", ok=False)
        # One-time use, and expire after 10 minutes so a leaked state token
        # cannot be replayed a day later.
        state_path.unlink(missing_ok=True)
        if time.time() - stored_ts > 600:
            return _spotify_result_page("OAuth state expired. Restart the connect flow.", ok=False)
        if not received_state or received_state != stored_state:
            return _spotify_result_page("OAuth state mismatch. Restart the connect flow.", ok=False)
        if not code:
            return _spotify_result_page("Spotify did not return an authorisation code.", ok=False)
        try:
            auth.exchange_code(code)
        except spotify_client.SpotifyAuthError as exc:
            return _spotify_result_page(f"Could not exchange code: {exc}", ok=False)
        return _spotify_result_page("Spotify connected. You can close this tab.", ok=True)

    @app.post("/spotify/paste")
    def spotify_paste():  # type: ignore[no-untyped-def]
        """Finish the OAuth flow from a pasted callback URL.

        Spotify now rejects plain-http redirect URIs unless the host is the
        literal loopback IP 127.0.0.1. That means a phone completing the flow
        gets redirected to *its own* 127.0.0.1 and the callback never reaches
        the Pi. Rather than require an HTTPS reverse proxy on a gift device,
        the user copies the failed URL out of their address bar and pastes it
        here; the code inside is still valid for the exchange.

        Accepts either a full URL or a bare code, so a user who only manages
        to copy the code fragment still succeeds.
        """
        config = load_config()
        auth = _spotify_auth(config)
        if not auth.configured:
            return jsonify(ok=False, error="Spotify credentials are not configured on this ticker."), 400
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get("url", request.form.get("url", ""))).strip()
        if not raw:
            return jsonify(ok=False, error="Paste the URL from your browser's address bar."), 400
        code = _extract_spotify_code(raw)
        if not code:
            return jsonify(
                ok=False,
                error="No authorisation code found in that text. Copy the whole address-bar URL.",
            ), 400
        try:
            auth.exchange_code(code)
        except spotify_client.SpotifyAuthError as exc:
            # The most common cause is a stale code: they are single-use and
            # expire in about a minute, so say so rather than showing a raw error.
            return jsonify(
                ok=False,
                error=f"Spotify rejected that code ({exc}). Codes expire quickly — tap Connect and try again.",
            ), 400
        return jsonify(ok=True, connected=True)

    @app.post("/spotify/disconnect")
    def spotify_disconnect():  # type: ignore[no-untyped-def]
        config = load_config()
        _spotify_auth(config).disconnect()
        return jsonify(ok=True, connected=False)

    return app


app = create_app()
