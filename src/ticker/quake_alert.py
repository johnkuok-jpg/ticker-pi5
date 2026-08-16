# MIT License — Copyright (c) 2026 John Kuok
"""Background watcher that auto-switches the panel to quakes mode on a regional shake.

The passive quakes mode rotates through the last 24 hours of M4.5+ events;
this watcher exists so that a locally-felt tremor lifts to the front of the
panel by itself instead of waiting for the user to notice and switch modes.

Design contract mirrors ``config.network_notice()``:

* The renderer polls this watcher once a second inside its cheap-check block.
* When a fresh, in-region, above-threshold quake is seen, the watcher writes
  a JSON alert file at ``state_dir/quake_alert``.
* The renderer treats that file the same as the Wi-Fi notice: it overrides
  the persisted mode selection *without* touching disk, so the user's chosen
  mode returns automatically the moment the alert dwell expires.
* Manual mode switches from the web app clear the alert -- the user's intent
  outranks the auto-switch.

The USGS M2.5+ hourly feed is a very small file (~50 KB), refreshes once a
minute upstream, and does not require an API key. Polling it every 60s on a
Pi is a rounding error against the rest of the panel's traffic.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# The M2.5+ 1-hour feed. Small file, low latency between event and publish.
# ``2.5_hour`` is the finest-grained feed USGS ships that still covers every
# felt event in California (M3.0+ is exceptional in California; the hourly
# 2.5+ feed catches them all).
FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_hour.geojson"
REQUEST_TIMEOUT = 8.0
USER_AGENT = "ticker-pi5 (github.com/johnkuok-jpg/ticker-pi5)"

# How long we consider a just-detected event "fresh enough" to alert on. The
# hourly feed publishes retrospectively for up to an hour, so at boot we don't
# want a 45-minute-old quake to jump the panel. Ten minutes is a comfortable
# ceiling: the feed usually surfaces within ~2 minutes of origin time.
MAX_FRESH_SECONDS = 10 * 60

# How often we hit the feed. USGS publishes once a minute; polling at 60s is
# aligned with that cadence without needing to be surgically synchronised.
DEFAULT_POLL_INTERVAL = 60

# Rough California bounding box (lon, lat). Used as a fallback when the USGS
# place string omits "California" (rare, but happens with sea-floor events
# just off Mendocino, for instance). Numbers are the state's coastline out to
# a small offshore buffer so an offshore aftershock of a felt event still
# fires.
CA_BBOX = {"lon_min": -125.5, "lon_max": -113.5, "lat_min": 32.0, "lat_max": 42.5}


@dataclass(frozen=True)
class QuakeAlert:
    """The alert payload written to the state file and read by the renderer.

    ``first_detected_monotonic`` is a monotonic-clock reading, not wall time,
    because dwell arithmetic must be immune to NTP steps. ``time_ms`` is the
    upstream USGS origin time so the mode can render an honest "3 min ago".
    """

    event_id: str
    magnitude: float
    place: str
    time_ms: int
    first_detected_monotonic: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "magnitude": self.magnitude,
                "place": self.place,
                "time_ms": self.time_ms,
                "first_detected_monotonic": self.first_detected_monotonic,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "QuakeAlert | None":
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                event_id=str(payload["event_id"]),
                magnitude=float(payload["magnitude"]),
                place=str(payload["place"]),
                time_ms=int(payload["time_ms"]),
                first_detected_monotonic=float(payload["first_detected_monotonic"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _in_region(place: str, coordinates: object, region: str) -> bool:
    """Return True if *place* matches *region* (case-insensitive substring),
    or -- specifically for the California default -- if the point sits inside
    the California bounding box.

    An empty *region* means "worldwide": every event qualifies. This is the
    escape hatch for a user who wants alerts everywhere and doesn't want to
    fight with the substring match.
    """
    if not region:
        return True
    if region.lower() in (place or "").lower():
        return True
    # Bounding-box fallback is California-specific. Widening this to arbitrary
    # regions would need a proper polygon lookup and isn't worth the code
    # weight for a hobby panel; users who want a different region and don't
    # trust the substring match can name a state or country string that USGS
    # reliably includes in its ``place`` text.
    if region.lower() == "california" and isinstance(coordinates, list) and len(coordinates) >= 2:
        try:
            lon = float(coordinates[0])
            lat = float(coordinates[1])
        except (TypeError, ValueError):
            return False
        return (
            CA_BBOX["lon_min"] <= lon <= CA_BBOX["lon_max"]
            and CA_BBOX["lat_min"] <= lat <= CA_BBOX["lat_max"]
        )
    return False


class QuakeAlertWatcher:
    """Poll USGS, gate on region + magnitude + freshness, and publish an alert file.

    The watcher owns two state files under ``state_dir``:

    * ``quake_alert`` -- the currently active alert (or absent). This is what
      the renderer reads. The renderer never writes it.
    * ``quake_seen`` -- a rolling list of event IDs we've already alerted on,
      so a service restart doesn't re-fire on a still-warm event. Bounded
      length so it can't grow forever.
    """

    # Bound the seen-set. Every entry is a stable USGS ID; a hundred is more
    # than an active week's worth of alerts and keeps the file trivially small.
    SEEN_CAP = 100

    # Alerts persist for this many seconds since first detection. The renderer
    # is the one that reads the file and decides whether to override; the
    # watcher just refuses to renew an expired alert. Set from config in ctor.
    def __init__(
        self,
        config,  # noqa: ANN001 -- Config, avoid circular import
        *,
        opener=None,
        now_monotonic=None,
        now_seconds=None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.config = config
        self._opener = opener or urllib.request.urlopen
        self._monotonic = now_monotonic or time.monotonic
        self._wall = now_seconds or time.time
        self._poll_interval = poll_interval
        # Far enough back that the first tick always polls.
        self._last_poll_monotonic = -1e9
        self._seen_ids: list[str] = self._load_seen()

    # -- state files --------------------------------------------------------

    @property
    def alert_file(self) -> Path:
        return self.config.state_dir / "quake_alert"

    @property
    def seen_file(self) -> Path:
        return self.config.state_dir / "quake_seen"

    def _load_seen(self) -> list[str]:
        try:
            raw = self.seen_file.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        # Coerce to a bounded list of strings; ignore garbage entries.
        return [str(item) for item in payload if isinstance(item, (str, int))][-self.SEEN_CAP :]

    def _save_seen(self) -> None:
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            self.seen_file.write_text(json.dumps(self._seen_ids[-self.SEEN_CAP :]), encoding="utf-8")
        except OSError as error:
            LOGGER.warning("quake watcher: failed to persist seen list: %s", error)

    def _write_alert(self, alert: QuakeAlert) -> None:
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            self.alert_file.write_text(alert.to_json(), encoding="utf-8")
        except OSError as error:
            LOGGER.warning("quake watcher: failed to write alert file: %s", error)

    # -- public API ---------------------------------------------------------

    def current_alert(self) -> QuakeAlert | None:
        """Read the alert file, honouring the configured dwell window.

        An expired alert is removed from disk on read so nothing else has to
        garbage-collect it. The renderer calls this once a second, so the
        cleanup happens promptly.
        """
        try:
            raw = self.alert_file.read_text(encoding="utf-8")
        except OSError:
            return None
        alert = QuakeAlert.from_json(raw)
        if alert is None:
            # Corrupt file -- drop it so we don't wedge in the alert state.
            self.alert_file.unlink(missing_ok=True)
            return None
        age = self._monotonic() - alert.first_detected_monotonic
        if age >= self.config.quake_alert_dwell_seconds:
            self.alert_file.unlink(missing_ok=True)
            return None
        return alert

    def clear(self) -> None:
        """Cancel any active alert. Called on a manual mode switch."""
        self.alert_file.unlink(missing_ok=True)

    def tick(self) -> None:
        """Poll if it's time, and publish an alert file when a match lands.

        Intentionally cheap on the non-polling ticks: it is called every
        second inside the render loop, but does no work except a monotonic
        compare unless the interval has elapsed. The actual HTTP fetch is
        wrapped in a broad except so a transient network hiccup can never
        crash the render loop.
        """
        if not self.config.quake_alert_enabled:
            return
        now_m = self._monotonic()
        if now_m - self._last_poll_monotonic < self._poll_interval:
            return
        self._last_poll_monotonic = now_m
        try:
            self._poll_once(now_m)
        except Exception:  # pragma: no cover - render-loop safety
            LOGGER.exception("quake watcher: poll failed")

    # -- polling ------------------------------------------------------------

    def _poll_once(self, now_monotonic: float) -> None:
        payload = self._fetch()
        if payload is None:
            return
        features = payload.get("features")
        if not isinstance(features, list):
            return
        region = self.config.quake_alert_region
        threshold = self.config.quake_alert_min_mag
        now_wall = self._wall()

        # Walk newest-first: USGS is not required to sort, and the freshest
        # matching event is the only one we care about. Once we pick one, we
        # write the alert and stop; the watcher does not stack alerts.
        candidates: list[tuple[int, dict]] = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties")
            if not isinstance(props, dict):
                continue
            try:
                time_ms = int(props.get("time") or 0)
            except (TypeError, ValueError):
                continue
            candidates.append((time_ms, feature))
        candidates.sort(key=lambda pair: pair[0], reverse=True)

        for time_ms, feature in candidates:
            props = feature["properties"]
            try:
                mag = float(props.get("mag") or 0)
            except (TypeError, ValueError):
                continue
            if mag < threshold:
                continue
            place = str(props.get("place") or "")
            geometry = feature.get("geometry")
            coords = (
                geometry.get("coordinates") if isinstance(geometry, dict) else None
            )
            if not _in_region(place, coords, region):
                continue
            event_id = str(feature.get("id") or "")
            if not event_id:
                continue
            if event_id in self._seen_ids:
                continue
            # Freshness gate: if it's older than MAX_FRESH_SECONDS at first
            # detection, treat it as historical. This is the anti-spam clause
            # for boots that happen after an event but before the alerter had
            # a chance to see it live.
            age_seconds = max(0.0, now_wall - time_ms / 1000.0)
            if age_seconds > MAX_FRESH_SECONDS:
                # Still mark it seen so a re-poll doesn't reconsider it.
                self._remember(event_id)
                continue
            alert = QuakeAlert(
                event_id=event_id,
                magnitude=mag,
                place=place,
                time_ms=time_ms,
                first_detected_monotonic=now_monotonic,
            )
            self._write_alert(alert)
            self._remember(event_id)
            LOGGER.info(
                "quake watcher: alerting M%.1f %r (age %.0fs, region=%r)",
                mag,
                place,
                age_seconds,
                region,
            )
            return

    def _remember(self, event_id: str) -> None:
        if event_id in self._seen_ids:
            return
        self._seen_ids.append(event_id)
        if len(self._seen_ids) > self.SEEN_CAP:
            self._seen_ids = self._seen_ids[-self.SEEN_CAP :]
        self._save_seen()

    def _fetch(self) -> dict | None:
        request = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
            LOGGER.warning("quake watcher: fetch failed: %s", error)
            return None
