"""Static assets must be content-fingerprinted in page markup.

The page's JS and its stylesheet are a matched pair: syncModeSections() adds a
`mode-visible` class, and only the current stylesheet knows to render it. Serve
fresh HTML against a stylesheet cached from an earlier deploy and *every* mode's
settings card stays hidden -- a worse failure than the single invisible commute
card that motivated the class in the first place.

The ticker is reachable through a Cloudflare tunnel, so there are two caches in
front of the file (edge and browser) that no amount of care on the Pi controls.
A content fingerprint in the URL is what makes a deploy self-invalidating.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ticker.web.app import create_app

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "ticker" / "web" / "templates"


@pytest.fixture()
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def test_stylesheet_link_carries_a_fingerprint(client):
    html = client.get("/").get_data(as_text=True)
    match = re.search(r'<link rel="stylesheet" href="([^"]*style\.css[^"]*)"', html)
    assert match, "no stylesheet link found in the rendered page"
    href = match.group(1)
    assert "?v=" in href, (
        f"stylesheet URL {href!r} has no version query; a cached copy from an "
        f"earlier deploy would hide every settings card"
    )
    version = href.split("?v=")[1]
    assert re.fullmatch(r"[0-9a-f]{12}", version), (
        f"expected a 12-char hex fingerprint, got {version!r}"
    )


def test_fingerprint_tracks_file_contents(tmp_path, monkeypatch):
    """Editing the stylesheet must change its URL; reverting must restore it."""
    app = create_app()
    static_url = app.jinja_env.globals["static_url"]
    css = Path(app.static_folder) / "style.css"
    original = css.read_bytes()
    with app.test_request_context("/"):
        before = static_url("style.css")
        try:
            css.write_bytes(original + b"\n/* cache-busting probe */\n")
            after = static_url("style.css")
        finally:
            css.write_bytes(original)
        restored = static_url("style.css")

    assert before != after, "editing style.css did not change its URL"
    assert before == restored, (
        "reverting style.css did not restore its original URL; the fingerprint "
        "is not derived from content"
    )


def test_missing_asset_does_not_break_the_page():
    """A bad filename must degrade to an unversioned URL, not raise.

    The stylesheet link is in <head>; an exception here would blank the whole
    control page rather than lose a cache hint.
    """
    app = create_app()
    static_url = app.jinja_env.globals["static_url"]
    with app.test_request_context("/"):
        href = static_url("definitely-not-a-real-file.css")
    assert "definitely-not-a-real-file.css" in href
    assert "?v=" not in href


def test_no_template_links_the_stylesheet_unversioned():
    """Catch a new template (or a revert) that skips static_url()."""
    offenders = []
    for template in TEMPLATES.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "style.css" not in line or "rel=\"stylesheet\"" not in line:
                continue
            if "static_url(" not in line:
                offenders.append(f"{template.name}: {line.strip()}")
    assert not offenders, (
        "these stylesheet links bypass static_url() and will serve a cached "
        f"stylesheet after a deploy: {offenders}"
    )
