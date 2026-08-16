# MIT License — Copyright (c) 2026 John Kuok
"""ZIP normalisation, geocoding, and the config/web plumbing built on it."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO

import pytest

from ticker import zipcode
from ticker.config import Config


@pytest.fixture(autouse=True)
def _clear_zip_cache():
    zipcode.clear_cache()
    yield
    zipcode.clear_cache()


# -- normalize -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("94103", "94103"),
        ("  94103  ", "94103"),
        ("94103-1234", "94103"),  # ZIP+4 truncates to the 5-digit prefix
        ("94103 1234", "94103"),
        ("02134", "02134"),       # leading zero survives
        ("9410", ""),             # too short
        ("", ""),
        ("SW1A 1AA", ""),         # UK postcode
        ("M5V 3L9", ""),          # Canadian postal code
        ("San Francisco", ""),
        (None, ""),
    ],
)
def test_normalize(raw, expected):
    assert zipcode.normalize(raw) == expected


# -- lookup ----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_SF_PAYLOAD = {
    "post code": "94103",
    "country": "United States",
    "places": [
        {
            "place name": "San Francisco",
            "state": "California",
            "state abbreviation": "CA",
            "latitude": "37.7726",
            "longitude": "-122.4099",
        }
    ],
}


def test_lookup_resolves_coordinates_and_label(monkeypatch):
    monkeypatch.setattr(
        zipcode.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_SF_PAYLOAD)
    )
    location = zipcode.lookup("94103")
    assert location is not None
    assert location.zip_code == "94103"
    assert location.lat == pytest.approx(37.7726)
    assert location.lon == pytest.approx(-122.4099)
    assert location.city == "San Francisco"
    assert location.state == "CA"
    assert location.label == "San Francisco, CA"


def test_lookup_accepts_zip_plus_four(monkeypatch):
    monkeypatch.setattr(
        zipcode.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_SF_PAYLOAD)
    )
    assert zipcode.lookup("94103-1234").zip_code == "94103"


def test_lookup_rejects_malformed_zip_without_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not hit the network for a malformed ZIP")

    monkeypatch.setattr(zipcode.urllib.request, "urlopen", _boom)
    assert zipcode.lookup("abc") is None
    assert zipcode.lookup("941") is None


def test_lookup_caches_success(monkeypatch):
    calls = []

    def _once(*a, **k):
        calls.append(1)
        return _FakeResponse(_SF_PAYLOAD)

    monkeypatch.setattr(zipcode.urllib.request, "urlopen", _once)
    zipcode.lookup("94103")
    zipcode.lookup("94103")
    assert len(calls) == 1


def test_lookup_caches_404_miss(monkeypatch):
    calls = []

    def _not_found(*a, **k):
        calls.append(1)
        raise urllib.error.HTTPError("url", 404, "Not Found", {}, BytesIO(b""))

    monkeypatch.setattr(zipcode.urllib.request, "urlopen", _not_found)
    assert zipcode.lookup("00000") is None
    assert zipcode.lookup("00000") is None
    assert len(calls) == 1, "a 404 is a definitive miss and should be cached"


def test_lookup_does_not_cache_transient_failure(monkeypatch):
    calls = []

    def _flaky(*a, **k):
        calls.append(1)
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(zipcode.urllib.request, "urlopen", _flaky)
    assert zipcode.lookup("94103") is None
    assert zipcode.lookup("94103") is None
    assert len(calls) == 2, "a network blip must not become a sticky failure"


def test_lookup_handles_empty_places(monkeypatch):
    monkeypatch.setattr(
        zipcode.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse({"post code": "94103", "places": []}),
    )
    assert zipcode.lookup("94103") is None


# -- config integration ----------------------------------------------------


def _config(tmp_path, **kwargs) -> Config:
    return Config(state_dir=tmp_path, **kwargs)


def test_set_weather_zip_persists_and_reads_back(tmp_path, monkeypatch):
    monkeypatch.setattr(
        zipcode.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_SF_PAYLOAD)
    )
    config = _config(tmp_path)
    location = config.set_weather_zip("94103")
    assert location.city == "San Francisco"

    zip_code, lat, lon, label = config.current_weather_location()
    assert zip_code == "94103"
    assert float(lat) == pytest.approx(37.7726, abs=1e-4)
    assert float(lon) == pytest.approx(-122.4099, abs=1e-4)
    assert label == "San Francisco, CA"
    assert config.current_weather_coords() == (lat, lon)
    assert config.current_weather_zip() == "94103"


def test_state_file_beats_env_coordinates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        zipcode.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_SF_PAYLOAD)
    )
    config = _config(tmp_path, weather_lat="40.7", weather_lon="-74.0")
    assert config.current_weather_coords() == ("40.7", "-74.0")
    config.set_weather_zip("94103")
    lat, lon = config.current_weather_coords()
    assert float(lat) == pytest.approx(37.7726, abs=1e-4)


def test_env_coordinates_used_when_no_state_file(tmp_path):
    config = _config(tmp_path, weather_lat="40.7128", weather_lon="-74.0060")
    zip_code, lat, lon, label = config.current_weather_location()
    assert (zip_code, lat, lon, label) == ("", "40.7128", "-74.0060", "")


def test_unset_location_is_all_empty(tmp_path):
    config = _config(tmp_path)
    assert config.current_weather_location() == ("", "", "", "")
    assert config.current_weather_coords() == ("", "")


def test_clearing_zip_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        zipcode.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_SF_PAYLOAD)
    )
    config = _config(tmp_path, weather_lat="40.7", weather_lon="-74.0")
    config.set_weather_zip("94103")
    config.set_weather_zip("")
    assert config.current_weather_coords() == ("40.7", "-74.0")


def test_bad_zip_raises_and_leaves_existing_location_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(
        zipcode.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_SF_PAYLOAD)
    )
    config = _config(tmp_path)
    config.set_weather_zip("94103")

    def _not_found(*a, **k):
        raise urllib.error.HTTPError("url", 404, "Not Found", {}, BytesIO(b""))

    monkeypatch.setattr(zipcode.urllib.request, "urlopen", _not_found)
    with pytest.raises(ValueError):
        config.set_weather_zip("00000")
    # The good location survives the failed write.
    assert config.current_weather_zip() == "94103"


def test_malformed_zip_raises_before_any_lookup(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not geocode a malformed ZIP")

    monkeypatch.setattr(zipcode.urllib.request, "urlopen", _boom)
    config = _config(tmp_path)
    with pytest.raises(ValueError, match="5-digit"):
        config.set_weather_zip("abc")


def test_corrupt_state_file_falls_back_to_env(tmp_path):
    config = _config(tmp_path, weather_lat="40.7", weather_lon="-74.0")
    config.weather_location_file.parent.mkdir(parents=True, exist_ok=True)
    config.weather_location_file.write_text("94103\tnot-a-number\tnope\n", encoding="utf-8")
    assert config.current_weather_coords() == ("40.7", "-74.0")


def test_env_zip_seed_resolves_once_and_persists(tmp_path, monkeypatch):
    calls = []

    def _once(*a, **k):
        calls.append(1)
        return _FakeResponse(_SF_PAYLOAD)

    monkeypatch.setattr(zipcode.urllib.request, "urlopen", _once)
    config = _config(tmp_path, weather_zip="94103")
    lat, lon = config.current_weather_coords()
    assert float(lat) == pytest.approx(37.7726, abs=1e-4)
    # Second read comes off the state file, and the module cache means even a
    # re-resolve would not re-fetch; either way, no second HTTP call.
    config.current_weather_coords()
    assert len(calls) == 1
    assert config.weather_location_file.exists()


# -- modes -----------------------------------------------------------------


def test_weather_mode_prompts_for_zip_when_unset(tmp_path):
    from ticker.canvas import Canvas
    from ticker.modes.weather import WeatherMode

    config = _config(tmp_path)
    canvas = Canvas(128, 32)
    WeatherMode(config).render(canvas, 0)
    # Something was drawn (the prompt), and no network call was attempted.
    assert any(px != (0, 0, 0) for px in canvas.image_buffer.convert("RGB").getdata())


def test_weather_mode_uses_zip_coordinates(tmp_path, monkeypatch):
    from ticker.modes.weather import WeatherMode

    monkeypatch.setattr(
        zipcode.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_SF_PAYLOAD)
    )
    config = _config(tmp_path)
    config.set_weather_zip("94103")

    seen = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _fake_get(url, **kwargs):
        seen.setdefault("urls", []).append(url)
        if "/points/" in url:
            return _Resp({"properties": {"forecast": "https://example.test/fc"}})
        return _Resp(
            {
                "properties": {
                    "periods": [
                        {
                            "temperature": 61,
                            "temperatureUnit": "F",
                            "shortForecast": "Sunny",
                            "windSpeed": "10 mph",
                            "isDaytime": True,
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr("ticker.modes.weather.requests.get", _fake_get)
    mode = WeatherMode(config)
    mode._refresh()
    assert any("37.77" in url and "-122.40" in url for url in seen["urls"])
    assert mode.forecast is not None
    assert mode.forecast.temperature == 61
