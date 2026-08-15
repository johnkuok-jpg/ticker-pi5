# MIT License — Copyright (c) 2026 John Kuok
"""Spotify Web API client for the 'now playing' mode.

Runs entirely on the Pi: no third-party middleman, no polling of an external
service. The user authorises once through the webapp, Spotify returns a
refresh token that lands in a file inside ``state_dir``, and from then on the
renderer swaps that refresh token for a short-lived access token as needed.

Two responsibilities live here:

* :class:`SpotifyAuth` owns tokens on disk — the OAuth code exchange, the
  refresh flow, and reading/writing ``spotify_tokens.json``. The webapp calls
  into it to start and finish the auth flow.
* :class:`SpotifyClient` owns 'what is playing right now', including a small
  album-art cache. The renderer holds one instance and calls
  :meth:`SpotifyClient.snapshot` every frame; the client throttles its own
  polling so the render loop cannot swamp Spotify.

The renderer must never block or raise on a network hiccup, so every network
call is wrapped: on failure the last known snapshot stays on screen until the
next successful poll clears it.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from PIL import Image

# Spotify's own base URLs. Kept as constants so a test can monkeypatch them
# to point at a fake server rather than touching the live API.
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Scope required to read what is currently playing. Deliberately minimal:
# we do NOT ask for playback-modify or library-read, so the token cannot be
# abused to change the user's music.
SCOPE = "user-read-currently-playing user-read-playback-state"

# HTTP timeouts. Long enough for a flaky home network, short enough that a
# dead upstream cannot stall the render loop for more than a handful of ms
# because the network call runs on a background thread.
_HTTP_TIMEOUT_SECONDS = 6

# How often to hit /currently-playing while a track is playing. Spotify's rate
# limit is generous (thousands/hour) but polling faster than the display
# updates buys nothing — the panel only refreshes about once per second visually.
POLL_SECONDS = 5

# When nothing is playing, back off to save network. The webapp still shows
# "Not playing" and the mode switch is instant on next state change.
IDLE_POLL_SECONDS = 20

# Access tokens live an hour; refresh 60s early so a token that expires
# mid-flight cannot cause a race between a 401 and a refresh.
_REFRESH_MARGIN_SECONDS = 60

# One shared session for all Spotify calls; keeps a TLS connection warm and
# sets a UA Spotify's edge is happy with.
_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": "ticker-pi5/1.0 (+github.com/johnkuok-jpg/ticker-pi5)",
        "Accept": "application/json",
    }
)


@dataclass(frozen=True)
class NowPlaying:
    """Everything the renderer needs to draw one 'now playing' frame.

    Immutable so the renderer can hold onto it across frames without worrying
    about the fetcher mutating fields underneath it.
    """

    is_playing: bool
    title: str
    artist: str
    progress_ms: int
    duration_ms: int
    #: 32x32 RGB album cover, already downscaled with nearest-neighbour so it
    #: looks correct on the LED panel. ``None`` while art is still loading or
    #: the API returned no image URL (rare, e.g. some podcasts).
    album_art: Image.Image | None
    #: Wall-clock time the snapshot was captured. Used only to age-out a
    #: stalled poll so a dropped connection does not freeze the progress bar.
    fetched_at: float


class SpotifyAuthError(RuntimeError):
    """Raised when the OAuth handshake or refresh fails permanently.

    A caller that catches this should surface a 'Reconnect Spotify' prompt
    rather than retrying; a permanent failure usually means the user revoked
    the app or the developer credentials are wrong.
    """


class SpotifyAuth:
    """Owns Spotify OAuth tokens on disk.

    Not thread-safe on writes: only the Flask webapp writes tokens (during the
    OAuth callback), and only one renderer reads and refreshes. The renderer's
    reads are safe because a partial file just yields a JSON parse error, and
    the fetcher treats that the same as 'no tokens yet'.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_file: Path,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._token_file = token_file

    @property
    def configured(self) -> bool:
        """True when developer credentials are set.

        Without a client id/secret the connect button is hidden and the mode
        renders a helpful 'Configure Spotify credentials' message instead of
        looking broken.
        """
        return bool(self._client_id and self._client_secret and self._redirect_uri)

    @property
    def connected(self) -> bool:
        """True when we have a stored refresh token to swap for access tokens."""
        return bool(self._load().get("refresh_token"))

    def build_authorize_url(self, state: str) -> str:
        """Where to send the user's browser to grant access.

        ``state`` is the CSRF token — the caller stores it in the Flask
        session, and the /spotify/callback route rejects any callback whose
        ``state`` does not match, so a random web page cannot trick the user
        into linking a different Spotify account.
        """
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": self._redirect_uri,
            "scope": SCOPE,
            "state": state,
            # show_dialog=false so a user who has already granted access sails
            # straight through instead of being nagged again.
            "show_dialog": "false",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> None:
        """Trade the one-time authorisation code for a refresh token.

        Called from the /spotify/callback handler. Persists the token on
        success and raises :class:`SpotifyAuthError` on failure.
        """
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
        }
        data = self._post_token(payload)
        # Spotify returns both an access and refresh token on the first exchange.
        # Store the refresh; the access token is cached for its lifetime so a
        # fresh Pi does not have to refresh immediately.
        self._save(
            {
                "refresh_token": data["refresh_token"],
                "access_token": data.get("access_token", ""),
                "expires_at": int(time.time()) + int(data.get("expires_in", 3600)),
            }
        )

    def access_token(self) -> str:
        """Return a live access token, refreshing when needed.

        The renderer calls this on every poll. Cheap in the common case (no
        network) because tokens live an hour and we refresh only when the
        cached one is within :data:`_REFRESH_MARGIN_SECONDS` of expiry.
        """
        state = self._load()
        if not state.get("refresh_token"):
            raise SpotifyAuthError("Spotify is not connected")
        if state.get("access_token") and int(state.get("expires_at", 0)) - _REFRESH_MARGIN_SECONDS > int(time.time()):
            return state["access_token"]
        # Either no cached access token, or the cached one is about to expire.
        return self._refresh(state["refresh_token"])

    def disconnect(self) -> None:
        """Forget the stored tokens. The next poll will report 'not connected'."""
        try:
            self._token_file.unlink()
        except FileNotFoundError:
            pass

    # -- internals ------------------------------------------------------

    def _refresh(self, refresh_token: str) -> str:
        """Swap a refresh token for a fresh access token and persist it.

        Spotify sometimes rotates the refresh token itself on refresh; when it
        does we store the new one. Losing this step would silently invalidate
        the connection after Spotify's next rotation.
        """
        payload = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        data = self._post_token(payload)
        stored = self._load()
        stored["access_token"] = data.get("access_token", "")
        stored["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
        # Spotify only returns a new refresh_token when it rotates one.
        if data.get("refresh_token"):
            stored["refresh_token"] = data["refresh_token"]
        self._save(stored)
        return stored["access_token"]

    def _post_token(self, payload: dict[str, str]) -> dict[str, Any]:
        """POST to Spotify's token endpoint with HTTP Basic auth on the app credentials."""
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode("utf-8")).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            response = _SESSION.post(TOKEN_URL, headers=headers, data=payload, timeout=_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:  # noqa: BLE001
            raise SpotifyAuthError(f"Could not reach Spotify auth: {exc}") from exc
        if response.status_code >= 400:
            # Surface Spotify's error body so a misconfigured redirect URI or
            # revoked app is diagnosable from the webapp response.
            raise SpotifyAuthError(f"Spotify auth failed ({response.status_code}): {response.text[:200]}")
        return response.json()

    def _load(self) -> dict[str, Any]:
        """Read the tokens file, returning an empty dict when absent or corrupt.

        A corrupt file is treated as 'not connected' rather than raising so
        that a bad write during a power-off cannot brick the mode.
        """
        try:
            raw = self._token_file.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        """Write the tokens file atomically enough for a single-writer setup."""
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self._token_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data), encoding="utf-8")
        temp.replace(self._token_file)
        # Keep the token file readable only by the owner; it grants access to
        # the user's currently-playing endpoint until they revoke the app.
        try:
            self._token_file.chmod(0o600)
        except OSError:
            pass


class SpotifyClient:
    """Polls Spotify for the current track and caches the album art.

    Designed to be safe to call from the render loop: :meth:`snapshot` is
    non-blocking and returns immediately, dispatching a background HTTP fetch
    only when the throttle allows one.
    """

    def __init__(self, auth: SpotifyAuth) -> None:
        self._auth = auth
        self._lock = threading.Lock()
        self._last: NowPlaying | None = None
        self._last_polled_at: float = 0.0
        self._in_flight: bool = False
        # Cache one album art at a time. Keyed by album image URL so a re-fetch
        # of the same track (which happens twice a second) is a no-op.
        self._art_url: str | None = None
        self._art_image: Image.Image | None = None

    def snapshot(self) -> NowPlaying | None:
        """Return the current playback snapshot, kicking off a poll if due.

        The render loop calls this every frame. Poll dispatch is throttled to
        :data:`POLL_SECONDS` when a track is playing, :data:`IDLE_POLL_SECONDS`
        otherwise, so a 30fps loop never fires 30 requests per second.
        """
        if not (self._auth.configured and self._auth.connected):
            return None
        now = time.monotonic()
        due_in = POLL_SECONDS if self._last and self._last.is_playing else IDLE_POLL_SECONDS
        with self._lock:
            should_poll = (now - self._last_polled_at) >= due_in and not self._in_flight
            if should_poll:
                self._in_flight = True
                self._last_polled_at = now
        if should_poll:
            # Background thread so the render loop cannot stall on the HTTP round-trip.
            threading.Thread(target=self._poll_once, daemon=True).start()
        return self._last

    # -- internals ------------------------------------------------------

    def _poll_once(self) -> None:
        """Fetch /currently-playing once and update ``_last``.

        Runs on a background thread. Any failure leaves the previous snapshot
        in place so the panel does not flicker on a transient error.
        """
        try:
            token = self._auth.access_token()
        except SpotifyAuthError:
            # Auth is permanently broken (revoked, bad creds). Report 'nothing
            # playing' rather than a stale track that will never advance.
            with self._lock:
                self._last = None
                self._in_flight = False
            return
        try:
            response = _SESSION.get(
                f"{API_BASE}/me/player/currently-playing",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            with self._lock:
                self._in_flight = False
            return
        # Default: keep whatever we last had, so a transient error does not
        # blink the panel to 'Not playing'. Only overwrite for known-good outcomes.
        snapshot: NowPlaying | None = self._last
        try:
            if response.status_code == 204 or not response.content:
                # 204 No Content = no active player. Show 'Not playing'.
                snapshot = NowPlaying(
                    is_playing=False,
                    title="",
                    artist="",
                    progress_ms=0,
                    duration_ms=0,
                    album_art=None,
                    fetched_at=time.time(),
                )
            elif response.status_code == 401:
                # Access token unexpectedly rejected. Leave the previous
                # snapshot in place; the next poll will trigger a refresh
                # via access_token()'s expiry check.
                snapshot = self._last
            elif response.status_code >= 400:
                snapshot = self._last
            else:
                snapshot = self._parse_now_playing(response.json())
        except Exception:  # noqa: BLE001 - never let renderer see this
            snapshot = self._last
        finally:
            with self._lock:
                if snapshot is not None:
                    self._last = snapshot
                self._in_flight = False

    def _parse_now_playing(self, payload: dict[str, Any]) -> NowPlaying:
        """Turn Spotify's JSON into a :class:`NowPlaying`.

        Handles both track and episode item types. Podcast episodes report
        ``show`` where a track reports ``artists``; we prefer showing the show
        name over an empty artist line.
        """
        item = payload.get("item") or {}
        is_playing = bool(payload.get("is_playing"))
        progress_ms = int(payload.get("progress_ms") or 0)
        duration_ms = int(item.get("duration_ms") or 0)
        title = str(item.get("name") or "")
        # Track: item.artists is a list of {name, ...}
        # Episode: item.show.name has the show
        artist = ""
        if isinstance(item.get("artists"), list) and item["artists"]:
            artist = ", ".join(str(a.get("name", "")) for a in item["artists"] if a.get("name"))
        elif isinstance(item.get("show"), dict):
            artist = str(item["show"].get("name") or "")

        # Album art: tracks put it under item.album.images; episodes under
        # item.images. Both are lists sorted largest-first.
        images: list[dict[str, Any]] = []
        if isinstance(item.get("album"), dict) and isinstance(item["album"].get("images"), list):
            images = item["album"]["images"]
        elif isinstance(item.get("images"), list):
            images = item["images"]
        art_url = ""
        if images:
            # Pick the smallest image at or above 64px — anything smaller
            # would look pixelated even after nearest-neighbour downscaling.
            candidates = [img for img in images if isinstance(img, dict) and img.get("url")]
            candidates.sort(key=lambda i: int(i.get("width") or 0))
            for img in candidates:
                if int(img.get("width") or 0) >= 64:
                    art_url = str(img["url"])
                    break
            if not art_url:
                art_url = str(candidates[-1]["url"]) if candidates else ""

        art_image = self._resolve_art(art_url)
        return NowPlaying(
            is_playing=is_playing,
            title=title,
            artist=artist,
            progress_ms=progress_ms,
            duration_ms=duration_ms,
            album_art=art_image,
            fetched_at=time.time(),
        )

    def _resolve_art(self, url: str) -> Image.Image | None:
        """Download and cache the album art at 32x32.

        Cached by URL so the twice-a-second poll on the same track only hits
        Spotify's CDN once. Nearest-neighbour downscale — bilinear muddies
        pixel-scale artwork on a low-res LED panel.
        """
        if not url:
            return None
        if url == self._art_url and self._art_image is not None:
            return self._art_image
        try:
            response = _SESSION.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            source = Image.open(io.BytesIO(response.content)).convert("RGB")
        except (requests.RequestException, OSError, ValueError):
            return None
        art = source.resize((32, 32), Image.NEAREST)
        self._art_url = url
        self._art_image = art
        return art


def new_state_token() -> str:
    """A URL-safe CSRF token for the OAuth ``state`` parameter."""
    return secrets.token_urlsafe(24)
