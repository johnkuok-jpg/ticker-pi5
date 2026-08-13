# MIT License — Copyright (c) 2026 John Kuok
"""Minimal Flask app for selecting ticker modes and brightness."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from ticker.config import VALID_MODES, load_config


def _renderer_status(pid_file: Path) -> tuple[int | None, bool]:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid, True
    except (OSError, ValueError):
        return None, False


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

    @app.get("/api/status")
    def status():  # type: ignore[no-untyped-def]
        config = load_config()
        pid, alive = _renderer_status(config.pid_file)
        return jsonify(current_mode=config.current_mode(), renderer_pid=pid, renderer_alive=alive)

    return app


app = create_app()
