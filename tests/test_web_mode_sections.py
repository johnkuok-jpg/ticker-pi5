"""Guards on the webapp's per-mode settings cards.

The commute mode shipped with a working backend, a working route endpoint, and
a settings card that was *never visible*: card visibility used to be a
hand-maintained allowlist of CSS selectors (one `body[data-mode="x"]
.flight[data-for~="x"]` line per mode) and commute's line was never added. The
card sat in the DOM at display:none, so there was no error, no log line, and no
failing test -- just a mode you could select but not configure.

These tests pin the two things that would have caught it:

1. Card visibility is derived generically, not enumerated per mode.
2. Every `data-for` token names a real mode (so a typo can't silently
   create a card that matches nothing).
"""

from __future__ import annotations

import re
from pathlib import Path

from ticker.config import VALID_MODES

WEB = Path(__file__).resolve().parents[1] / "src" / "ticker" / "web"
INDEX = WEB / "templates" / "index.html"
STYLE = WEB / "static" / "style.css"


def _strip_comments(text: str) -> str:
    """Drop HTML, Jinja, and JS block comments.

    Comments legitimately quote the markup they explain (the JS comment above
    syncModeSections spells out `data-for="MODE"`), so scanning raw source would
    treat prose as real attributes.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _data_for_tokens() -> dict[str, list[str]]:
    """Map each `data-for="..."` attribute to its mode tokens."""
    html = _strip_comments(INDEX.read_text(encoding="utf-8"))
    return {
        raw: raw.split()
        for raw in re.findall(r'data-for="([^"]*)"', html)
    }


def test_every_data_for_token_is_a_real_mode():
    """A card tagged for a nonexistent mode can never be shown."""
    for raw, tokens in _data_for_tokens().items():
        assert tokens, f'data-for="{raw}" has no mode tokens'
        for token in tokens:
            assert token in VALID_MODES, (
                f'data-for="{raw}" references unknown mode {token!r}; '
                f"it would never match body[data-mode]"
            )


def test_commute_has_a_settings_card():
    """Regression: commute must be configurable from the webapp.

    The route button lives in this card. Without it there is no way to spend a
    Directions call, which makes the whole mode unusable.
    """
    tokens = {t for tokens in _data_for_tokens().values() for t in tokens}
    assert "commute" in tokens


def test_css_does_not_reintroduce_a_per_mode_allowlist():
    """Visibility must stay generic.

    If someone adds `body[data-mode="newmode"] .flight[data-for~="newmode"]`
    back into the stylesheet, we're one forgotten line away from another
    invisible card. The `mode-visible` class that syncModeSections() toggles
    covers every mode without naming any.
    """
    css = STYLE.read_text(encoding="utf-8")
    # Strip comments first; the comment explaining the old pattern quotes it.
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    offenders = re.findall(r'body\[data-mode="[^"]+"\][^{}]*\[data-for', css_no_comments)
    assert not offenders, (
        "per-mode visibility selectors are back in style.css: "
        f"{offenders}. Rely on .mode-visible instead."
    )
    assert ".mode-visible" in css_no_comments, (
        "the generic .mode-visible rule is missing; cards would never show"
    )


def test_sections_are_hidden_by_default():
    """Cards must start hidden, or every mode's card shows at once."""
    css = re.sub(r"/\*.*?\*/", "", STYLE.read_text(encoding="utf-8"), flags=re.DOTALL)
    assert re.search(r"\.flight\[data-for\][^{]*\{[^}]*display:\s*none", css), (
        "no default display:none rule for .flight[data-for]"
    )
    assert re.search(r"\.card\[data-for\][^{]*\{[^}]*display:\s*none", css), (
        "no default display:none rule for .card[data-for]"
    )


def test_sync_function_is_wired_to_the_mode_attribute():
    """The JS half of the contract: derive from body[data-mode] and observe it."""
    html = INDEX.read_text(encoding="utf-8")
    assert "function syncModeSections()" in html
    assert "mode-visible" in html, "sync function must toggle the CSS hook"
    assert "attributeFilter: ['data-mode']" in html, (
        "sync must observe body[data-mode] so poll-driven mode changes update cards"
    )
