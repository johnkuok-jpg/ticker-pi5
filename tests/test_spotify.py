# MIT License — Copyright (c) 2026 John Kuok
"""Spotify mode + auth tests. All network calls are stubbed.

The Spotify code has three moving parts we care about:

1. :class:`SpotifyAuth` — token storage on disk (tight file mode, refresh on
   expiry). Verified without hitting Spotify by monkeypatching the HTTP client.
2. :class:`SpotifyClient` — a snapshot cache that never blocks the render
   loop. We verify that a disconnected auth short-circuits (no crash, returns
   None) and that a manually-injected :class:`NowPlaying` renders.
3. :class:`SpotifyMode` — the renderer. Should show a placeholder when idle,
   render the layout when a snapshot exists, and never overpaint the album
   art with the progress bar.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from ticker import spotify as spotify_client
from ticker.canvas import Canvas
from ticker.config import load_config
from ticker.modes.spotify import (
    ART_SIZE,
    BAR_ROW,
    SPOTIFY_GREEN,
    TEXT_LEFT,
    SpotifyMode,
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Minimal Config with Spotify credentials set so `configured` is True."""
    monkeypatch.setattr("ticker.config._first_writable_state_dir", lambda: tmp_path / "state")
    env = tmp_path / ".env"
    env.write_text(
        "TICKER_WIDTH=128\nTICKER_HEIGHT=32\n"
        "SPOTIFY_CLIENT_ID=fake_id\n"
        "SPOTIFY_CLIENT_SECRET=fake_secret\n"
        "SPOTIFY_REDIRECT_URI=http://ticker.local:8080/spotify/callback\n",
        encoding="utf-8",
    )
    return load_config(env)


def test_spotify_auth_configured_and_disconnected(config) -> None:  # type: ignore[no-untyped-def]
    """Fresh config: credentials on disk, no token file → configured, not connected."""
    auth = spotify_client.SpotifyAuth(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
        redirect_uri=config.spotify_redirect_uri,
        token_file=config.spotify_token_file,
    )
    assert auth.configured is True
    assert auth.connected is False


def test_spotify_auth_authorize_url_carries_state(config) -> None:  # type: ignore[no-untyped-def]
    """The authorize URL must include the caller-provided state token verbatim."""
    auth = spotify_client.SpotifyAuth(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
        redirect_uri=config.spotify_redirect_uri,
        token_file=config.spotify_token_file,
    )
    url = auth.build_authorize_url("state123")
    assert "client_id=fake_id" in url
    assert "state=state123" in url
    assert "user-read-currently-playing" in url


def test_spotify_auth_token_file_is_owner_only(config, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """After exchange_code, the on-disk token file must be mode 0600."""
    auth = spotify_client.SpotifyAuth(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
        redirect_uri=config.spotify_redirect_uri,
        token_file=config.spotify_token_file,
    )
    monkeypatch.setattr(
        spotify_client.SpotifyAuth,
        "_post_token",
        lambda self, payload: {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    auth.exchange_code("dummy_code")
    assert config.spotify_token_file.exists()
    mode = config.spotify_token_file.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"
    assert auth.connected is True


def test_spotify_mode_placeholder_when_disconnected(config) -> None:  # type: ignore[no-untyped-def]
    """No tokens on disk → placeholder renders without raising."""
    mode = SpotifyMode(config)
    canvas = Canvas(config.width, config.height)
    mode.render(canvas, tick=0)
    # Any non-black pixel means we drew *something*; the placeholder disc
    # must exist so the panel is not entirely blank.
    pixels = list(canvas.image_buffer.getdata())
    non_black = sum(1 for p in pixels if p != (0, 0, 0))
    assert non_black > 0


def test_spotify_mode_renders_now_playing_snapshot(config, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A fake NowPlaying snapshot should paint art, text, and a green progress bar."""
    mode = SpotifyMode(config)
    art = Image.new("RGB", (ART_SIZE, ART_SIZE), (200, 40, 40))
    snapshot = spotify_client.NowPlaying(
        is_playing=True,
        title="Song Title",
        artist="Artist Name",
        progress_ms=30_000,
        duration_ms=60_000,
        album_art=art,
        fetched_at=0.0,
    )
    # Bypass network + connection: force snapshot() to hand us the fake, and
    # pretend the auth is connected so the mode does not draw the placeholder.
    monkeypatch.setattr(mode.client, "snapshot", lambda: snapshot)
    monkeypatch.setattr(type(mode.auth), "connected", property(lambda self: True))

    canvas = Canvas(config.width, config.height)
    mode.render(canvas, tick=0)

    # Album art: top-left pixel should be the red we painted, not black.
    assert canvas.image_buffer.getpixel((0, 0)) == (200, 40, 40)
    # Progress bar: at 50% progress, col halfway into the text zone should
    # be Spotify green.
    midpoint_x = TEXT_LEFT + (128 - TEXT_LEFT) // 2 - 1
    assert canvas.image_buffer.getpixel((midpoint_x, BAR_ROW)) == SPOTIFY_GREEN


def test_spotify_mode_progress_bar_never_overpaints_art(config, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """Layout invariant: the progress bar starts at TEXT_LEFT and never touches art."""
    mode = SpotifyMode(config)
    art = Image.new("RGB", (ART_SIZE, ART_SIZE), (12, 34, 56))
    snapshot = spotify_client.NowPlaying(
        is_playing=True,
        title="t",
        artist="a",
        progress_ms=99_000,  # nearly complete
        duration_ms=100_000,
        album_art=art,
        fetched_at=0.0,
    )
    monkeypatch.setattr(mode.client, "snapshot", lambda: snapshot)
    monkeypatch.setattr(type(mode.auth), "connected", property(lambda self: True))

    canvas = Canvas(config.width, config.height)
    mode.render(canvas, tick=0)

    # Every pixel in the art square must still be the art colour on row 31.
    # If the progress bar started at col 0 by accident it would have overwritten (0,31).
    assert canvas.image_buffer.getpixel((0, BAR_ROW)) == (12, 34, 56)
    assert canvas.image_buffer.getpixel((ART_SIZE - 1, BAR_ROW)) == (12, 34, 56)


def test_extract_spotify_code_from_pasted_urls() -> None:
    """The paste-back parser must handle the shapes users actually paste.

    Spotify only allows plain-http redirect URIs on the literal loopback IP,
    so a phone finishing the flow lands on an unreachable 127.0.0.1 page. The
    user copies that dead address and pastes it; these are the variants seen
    in practice (full URL, no scheme, extra params, bare code, and mis-pastes).
    """
    from ticker.web.app import _extract_spotify_code

    full = "http://127.0.0.1:8080/spotify/callback?code=AQD9x_ABC-123&state=xyz"
    assert _extract_spotify_code(full) == "AQD9x_ABC-123"

    # state before code — order must not matter
    reordered = "http://127.0.0.1:8080/spotify/callback?state=xyz&code=AQD9x_ABC-123"
    assert _extract_spotify_code(reordered) == "AQD9x_ABC-123"

    # scheme stripped by the browser's copy behaviour
    no_scheme = "127.0.0.1:8080/spotify/callback?code=AQD9x_ABC-123"
    assert _extract_spotify_code(no_scheme) == "AQD9x_ABC-123"

    # trailing fragment / whitespace
    messy = "  http://127.0.0.1:8080/spotify/callback?code=AQD9x_ABC-123#_=_  "
    assert _extract_spotify_code(messy) == "AQD9x_ABC-123"

    # a bare code pasted on its own
    bare = "AQD9x_ABC-123456789012345678"
    assert _extract_spotify_code(bare) == bare

    # mis-pastes must be rejected rather than sent to Spotify
    assert _extract_spotify_code("") == ""
    assert _extract_spotify_code("https://open.spotify.com/") == ""
    assert _extract_spotify_code("short") == ""


def test_spotify_paste_route_rejects_junk(config, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """POST /spotify/paste should 400 on unusable input and never call Spotify."""
    from ticker.web.app import create_app

    monkeypatch.setattr("ticker.web.app.load_config", lambda *a, **k: config)
    app = create_app()
    client = app.test_client()

    # Empty body
    assert client.post("/spotify/paste", json={}).status_code == 400
    # Something that is clearly not a callback URL
    assert client.post("/spotify/paste", json={"url": "hello"}).status_code == 400


def test_spotify_paste_route_exchanges_valid_code(config, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A pasted callback URL should complete the token exchange and connect."""
    from ticker.web.app import create_app

    monkeypatch.setattr("ticker.web.app.load_config", lambda *a, **k: config)
    monkeypatch.setattr(
        spotify_client.SpotifyAuth,
        "_post_token",
        lambda self, payload: {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    app = create_app()
    client = app.test_client()

    res = client.post(
        "/spotify/paste",
        json={"url": "http://127.0.0.1:8080/spotify/callback?code=AQD9x_ABC-123&state=s"},
    )
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert config.spotify_token_file.exists()
