# MIT License — Copyright (c) 2026 John Kuok
"""Minimal Flask app for selecting ticker modes and brightness."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from ticker import bart
from ticker.config import VALID_MODES, load_config


def _renderer_status(pid_file: Path) -> tuple[int | None, bool]:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid, True
    except (OSError, ValueError):
        return None, False


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
            stations=bart.STATIONS,
            station=config.current_bart_station(),
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
            station=config.current_bart_station(),
        )

    return app


app = create_app()
