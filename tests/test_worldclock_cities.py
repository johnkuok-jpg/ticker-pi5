"""Validate the bundled world-clock city index.

The city index ships as a JSON blob inside the settings page, so an entry
with a bogus timezone or an over-long label would silently break the world
clock for whoever picked that city. Catch it here.
"""

from __future__ import annotations

import zoneinfo

import pytest

from ticker.modes.worldclock_cities import ALIASES, CITIES

# Matches the runtime cap in ``config.set_worldclock_cities``: labels get
# clamped to 6 characters before render, so anything longer would be
# silently truncated at save time.
_MAX_LABEL_LEN = 6

_AVAILABLE_TZS = zoneinfo.available_timezones()


def _city_by_name() -> dict[str, dict[str, str]]:
    return {c["name"]: c for c in CITIES}


def test_every_city_has_all_required_keys() -> None:
    required = {"name", "country", "tz", "label"}
    for city in CITIES:
        assert required <= set(city.keys()), city
        for key in required:
            assert isinstance(city[key], str) and city[key], (city, key)


def test_every_tz_is_a_real_iana_zone() -> None:
    for city in CITIES:
        assert (
            city["tz"] in _AVAILABLE_TZS
        ), f"unknown IANA tz {city['tz']!r} on {city['name']!r}"


def test_every_label_fits_the_panel_budget() -> None:
    for city in CITIES:
        assert len(city["label"]) <= _MAX_LABEL_LEN, city
        assert city["label"].strip() == city["label"], city


def test_country_codes_are_short() -> None:
    # Real ISO-2 or ISO-3 codes plus the special "UTC" anchor.
    for city in CITIES:
        assert 2 <= len(city["country"]) <= 3, city


def test_city_names_are_unique() -> None:
    names = [c["name"] for c in CITIES]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate city names would collide in the picker: {dupes}"


def test_every_alias_targets_a_real_city() -> None:
    known = _city_by_name()
    for alias, target in ALIASES.items():
        assert target in known, (
            f"alias {alias!r} points at unknown city {target!r}"
        )


def test_phoenix_is_arizona_no_dst() -> None:
    # This is the canonical trap: Phoenix does not observe DST, so it
    # can't share America/Denver.
    phoenix = _city_by_name()["Phoenix"]
    assert phoenix["tz"] == "America/Phoenix"


def test_all_of_china_maps_to_asia_shanghai() -> None:
    # China only uses one IANA zone despite its geographic span, so a
    # Beijing entry mapped to Asia/Urumqi (or similar) would be wrong.
    by_name = _city_by_name()
    for city_name in ("Beijing", "Shanghai", "Shenzhen", "Chengdu"):
        if city_name in by_name:
            assert by_name[city_name]["tz"] == "Asia/Shanghai", city_name


@pytest.mark.parametrize(
    ("city_name", "expected_tz"),
    [
        ("Sydney", "Australia/Sydney"),
        ("Perth", "Australia/Perth"),
        ("Adelaide", "Australia/Adelaide"),
        ("Brisbane", "Australia/Brisbane"),
        ("Darwin", "Australia/Darwin"),
        ("Hobart", "Australia/Hobart"),
    ],
)
def test_australian_cities_split_across_zones(city_name: str, expected_tz: str) -> None:
    by_name = _city_by_name()
    assert by_name[city_name]["tz"] == expected_tz
