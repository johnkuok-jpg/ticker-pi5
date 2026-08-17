"""The commute address typeahead endpoint.

Two properties matter here and neither is about returning suggestions.

First, the Google API key must never leave the Pi. Places Autocomplete is
billable, and the ticker is exposed through a Cloudflare tunnel -- a key
embedded in the page is a key anyone who can load the page can read out of view
source and spend. So the browser talks to us and we talk to Google.

Second, the endpoint must never break the form. Autocomplete is a convenience
layered on a text input that already worked; when Google is unreachable, the
key is missing, or Places API (New) has not been enabled on the project, the
user must still be able to type the address by hand. That means 200 with an
empty list and a reason, not a 4xx/5xx the frontend has to special-case.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from ticker.modes.commute import AutocompleteUnavailable
from ticker.web.app import create_app

INDEX = Path(__file__).resolve().parents[1] / "src" / "ticker" / "web" / "templates" / "index.html"


@pytest.fixture()
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def test_suggestions_are_returned_for_a_long_enough_query(client):
    with patch(
        "ticker.modes.commute.autocomplete_addresses",
        return_value=["181 Fremont St, San Francisco, CA 94105, USA"],
    ):
        payload = client.get("/api/commute/places?q=181 fremont").get_json()
    assert payload["suggestions"] == ["181 Fremont St, San Francisco, CA 94105, USA"]


def test_short_query_short_circuits_before_the_api(client):
    """The client debounces, but the server must not trust it to.

    Anything reaching the API costs money, so the floor is enforced on both
    sides.
    """
    with patch("ticker.modes.commute.autocomplete_addresses") as called:
        payload = client.get("/api/commute/places?q=18").get_json()
    called.assert_not_called()
    assert payload["suggestions"] == []


def test_missing_query_is_not_an_error(client):
    response = client.get("/api/commute/places")
    assert response.status_code == 200
    assert response.get_json()["suggestions"] == []


@pytest.mark.parametrize(
    "reason,expect_in_message",
    [
        ("not_enabled", "Places API"),
        ("no_key", "key"),
        ("network", "network"),
        ("api", "error"),
    ],
)
def test_failures_return_200_with_an_actionable_reason(client, reason, expect_in_message):
    """A degraded typeahead is not a broken page.

    The message is what the user reads under the input, so it has to name the
    fix -- "Places API error" for a project that simply has the API switched off
    would send him looking in the wrong place.
    """
    with patch(
        "ticker.modes.commute.autocomplete_addresses",
        side_effect=AutocompleteUnavailable(reason, "detail"),
    ):
        response = client.get("/api/commute/places?q=181 fremont")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["suggestions"] == []
    assert payload["reason"] == reason
    assert expect_in_message.lower() in payload["message"].lower()


def test_api_key_is_never_sent_to_the_browser(client):
    """Guard the whole point of proxying.

    Checks the rendered page for anything shaped like a Google API key, and for
    a direct call to the Google host from page JS.
    """
    html = client.get("/").get_data(as_text=True)
    assert not re.search(r"AIza[0-9A-Za-z_\-]{20,}", html), (
        "something that looks like a Google API key is in the page source"
    )
    # Scoped to the billable Maps hosts. A bare "googleapis.com" check fails on
    # the page's Google Fonts link, which is unauthenticated and free.
    for host in ("places.googleapis.com", "maps.googleapis.com"):
        assert host not in html, (
            f"the page calls {host} directly; autocomplete must go through "
            "/api/commute/places so the key stays server-side"
        )


def test_page_requests_suggestions_from_the_proxy(client):
    html = client.get("/").get_data(as_text=True)
    assert "/api/commute/places" in html


def test_typeahead_guards_are_present_in_the_page():
    """Each guard below exists to stop the page spending money per keystroke.

    Pinned as source assertions because the alternative is a browser test for
    what are three lines of debounce/cache/abort logic.
    """
    source = INDEX.read_text()
    assert "AbortController" in source, "a superseded response can repaint a stale list"
    assert "setTimeout(() => lookup(query), 350)" in source, "keystrokes must be debounced"
    assert "cache.has(query)" in source, "backspacing must replay from cache, not refetch"


def test_browser_autofill_is_disabled_on_the_address_inputs():
    """autocomplete="street-address" would stack the browser's own dropdown
    on top of ours over the same input."""
    source = INDEX.read_text()
    for field in ("commute-origin", "commute-destination"):
        block = source.split(f'id="{field}"', 1)[1][:400]
        assert 'autocomplete="off"' in block, f"{field} still opts into browser autofill"
