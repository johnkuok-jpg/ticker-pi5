"""Guards on the webapp's status-poll race.

The settings page kept "randomly" snapping back to the weather card while the
user was editing a different mode. Nothing was wrong with the mode file and
nothing server-side ever writes "weather" -- the bug was entirely in how
refreshStatus() applied its response.

setInterval fires refreshStatus every 10s, seventeen event handlers also call
it directly, and /api/status is slow (it stats a dozen state files and checks
Spotify auth). So responses overlap. The mode-button handler flips
body[data-mode] optimistically and then POSTs /mode/<name>, which means a poll
that left *before* a tap can land *after* it still carrying the pre-tap mode.
refreshStatus wrote that straight onto body[data-mode]; the MutationObserver
behind syncModeSections then swapped the visible settings card. Because
DEFAULT_MODE is "weather", the pre-tap mode was usually weather -- hence the
symptom.

The interleaving that produced it:

    t=0    background poll leaves; server mode is weather
    t=100  user taps COMMUTE; data-mode := commute (optimistic); POST leaves
    t=400  the t=0 poll resolves carrying current_mode="weather"  <-- clobber
    t=500  the POST's own refreshStatus resolves carrying "commute"

so data-mode was written commute -> weather -> commute, and the middle write is
the visible snap-back.

These tests pin the two guards, structurally rather than by asserting on a
rendered page, because the failure lives in request ordering that a static
render cannot exercise:

1. refreshStatus snapshots data-mode *before* awaiting, and every write to
   data-mode inside it is gated on that snapshot still being current.
2. A stale response (one overtaken by a newer poll) is dropped wholesale, so
   no field repaints backwards.
"""

from __future__ import annotations

import re
from pathlib import Path

INDEX = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ticker"
    / "web"
    / "templates"
    / "index.html"
)


def _strip_js_comments(text: str) -> str:
    """Drop JS block and line comments.

    The comments above refreshStatus explain the race in prose and quote the
    identifiers involved, so scanning raw source would match the explanation
    rather than the code.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _refresh_status_body() -> str:
    """Return the source of refreshStatus() with comments stripped.

    Brace-counts to the matching close so the extraction does not depend on
    what happens to sit after the function.
    """
    source = _strip_js_comments(INDEX.read_text(encoding="utf-8"))
    start = source.index("async function refreshStatus()")
    open_brace = source.index("{", start)
    depth = 0
    for i in range(open_brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace : i + 1]
    raise AssertionError("unbalanced braces in refreshStatus()")


def test_refresh_status_snapshots_mode_before_awaiting() -> None:
    """The snapshot MUST be taken before the fetch it is meant to outlive.

    Taken after the await it would capture the post-tap mode and compare it to
    itself, so the guard would read as satisfied and never fire.
    """
    body = _refresh_status_body()
    assert "modeAtRequest" in body, "no request-time mode snapshot in refreshStatus"
    assert body.index("modeAtRequest") < body.index("await fetch"), (
        "modeAtRequest is captured after the await, so it cannot detect a mode "
        "change that happened while the request was in flight"
    )


def test_every_data_mode_write_in_refresh_status_is_guarded() -> None:
    """No unguarded write to body[data-mode] may exist in refreshStatus.

    This is the assertion that would have caught the original bug: the old code
    wrote data-mode whenever the response merely differed from the DOM, with no
    notion of the response being stale.
    """
    body = _refresh_status_body()
    writes = [
        line.strip()
        for line in body.splitlines()
        if re.search(r"document\.body\.dataset\.mode\s*=", line)
    ]
    assert writes, "expected refreshStatus to still adopt the server's mode"

    guard = "modeChangedLocally"
    assert guard in body, f"refreshStatus has no {guard} guard"
    # The guard must be established before any write, and the write must sit
    # inside a branch that consults it.
    first_write = min(body.index(w) for w in writes)
    assert body.index(f"const {guard}") < first_write, (
        f"{guard} is computed after data-mode is written"
    )
    guarded_branch = re.search(
        r"if\s*\(\s*!" + guard + r"\b[^)]*\)\s*\{[^}]*document\.body\.dataset\.mode\s*=",
        body,
        flags=re.DOTALL,
    )
    assert guarded_branch, (
        "the data-mode write is not inside an `if (!modeChangedLocally ...)` "
        "branch, so a stale poll can still clobber the user's mode"
    )


def test_refresh_status_drops_responses_overtaken_by_a_newer_poll() -> None:
    """An overtaken response MUST be abandoned before it paints anything.

    Otherwise brightness, the watchlist, and the currency chips can all flicker
    backwards when two polls resolve out of order -- the same class of bug as
    the mode clobber, just less visible.
    """
    body = _refresh_status_body()
    assert "statusApplied" in body, "no stale-response sequence guard"
    early_return = re.search(r"if\s*\(\s*seq\s*<\s*statusApplied\s*\)\s*return", body)
    assert early_return, "stale responses are not dropped with an early return"
    # The bail-out has to precede the first field the function paints.
    assert early_return.start() < body.index("status.textContent"), (
        "the staleness check runs after the UI has already been repainted"
    )


def test_status_sequence_counters_are_declared_once_outside_refresh_status() -> None:
    """The counters must be module-scoped, not per-call.

    Declared inside refreshStatus they would reset on every invocation and
    compare each response only against itself, silently disabling the guard.
    """
    source = _strip_js_comments(INDEX.read_text(encoding="utf-8"))
    body = _refresh_status_body()
    for name in ("statusSeq", "statusApplied"):
        assert f"let {name} = 0;" in source, f"{name} is not initialised"
        assert f"let {name}" not in body, (
            f"{name} is declared inside refreshStatus, so it resets every call"
        )
