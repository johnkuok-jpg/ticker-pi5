# MIT License — Copyright (c) 2026 John Kuok
"""Minimal Flask app for selecting ticker modes and brightness."""

from __future__ import annotations

import os
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

from ticker import bart, baywheels, net, spotify as spotify_client
from ticker.config import VALID_MODES, load_config


def _renderer_status(pid_file: Path) -> tuple[int | None, bool]:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid, True
    except (OSError, ValueError):
        return None, False


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


def create_app() -> Flask:
    """Build the web app without any in-process dependency on the renderer."""
    app = Flask(__name__)

    @app.get("/")
    def index():  # type: ignore[no-untyped-def]
        config = load_config()
        return render_template(
            "index.html",
            modes=VALID_MODES,
            current_mode=config.current_mode(),
            brightness=round(config.current_brightness() * 100),
            schedule_note=_describe_schedule(config),
            flight=config.current_flight(),
            flight_airport=config.current_flight_airport(),
            stations=bart.STATIONS,
            station=config.current_bart_station(),
            bike_station=config.current_bike_station(),
            nametag_name=config.current_nametag_name(),
            nametag_color=_rgb_to_hex(config.current_nametag_color()),
            nametag_font=config.current_nametag_font(),
            spotify_configured=bool(config.spotify_client_id and config.spotify_client_secret and config.spotify_redirect_uri),
            spotify_connected=_spotify_auth(config).connected,
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

    # -- Wi-Fi ---------------------------------------------------------------
    #
    # Kept on its own page rather than on the panel of mode buttons. It is the one
    # screen a user reaches while the ticker is unreachable in the normal sense --
    # joined to its own setup hotspot with no internet behind it -- and mixing a
    # scan list and a password field into the control panel would put a
    # destructive control (change the network, lose this connection) next to
    # everyday taps.

    @app.get("/wifi")
    def wifi_page():  # type: ignore[no-untyped-def]
        status = net.status()
        return render_template(
            "wifi.html",
            status=status,
            available=net.available(),
            setup_ssid=net.HOTSPOT_SSID,
        )

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
        return jsonify(
            current_mode=config.current_mode(),
            renderer_pid=pid,
            renderer_alive=alive,
            brightness=round(config.current_brightness() * 100),
            schedule_note=_describe_schedule(config),
            flight=config.current_flight(),
            flight_airport=config.current_flight_airport(),
            station=config.current_bart_station(),
            bike_station=config.current_bike_station(),
            nametag_name=config.current_nametag_name(),
            nametag_color=_rgb_to_hex(config.current_nametag_color()),
            nametag_font=config.current_nametag_font(),
            spotify_configured=bool(config.spotify_client_id and config.spotify_client_secret and config.spotify_redirect_uri),
            spotify_connected=_spotify_auth(config).connected,
            network_notice=config.network_notice(),
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
