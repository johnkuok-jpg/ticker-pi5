# MIT License — Copyright (c) 2026 John Kuok
"""Tests for the frame-timing diagnostics added to renderer.py.

``renderer.run()`` itself needs real Pi hardware (``_open_matrix`` imports
the piomatter driver directly) so it isn't unit-tested here, same as before
this change -- there was no test_renderer.py prior to this file. What *is*
hardware-independent, and worth covering, is the ``_FrameDiag`` accumulator
that turns raw per-frame timings into the periodic summary line: the drop
threshold, the averaging, and the window reset. Those are the parts that
would quietly give a wrong answer (e.g. an off-by-one on the drop condition,
or forgetting to reset and reporting a growing max forever) without an
exception to catch it.
"""

from __future__ import annotations

import importlib
import logging
import time

import pytest

renderer = importlib.import_module("ticker.renderer")


@pytest.fixture
def diag() -> "renderer._FrameDiag":
    return renderer._FrameDiag()


def test_starts_empty(diag: "renderer._FrameDiag") -> None:
    assert diag.frame_count == 0
    assert diag.drop_count == 0
    assert diag.max_total_ms == 0.0


def test_record_accumulates_frame_count(diag: "renderer._FrameDiag") -> None:
    diag.record(render_ms=1.0, show_ms=1.0, total_ms=2.0, budget_ms=33.3)
    diag.record(render_ms=1.0, show_ms=1.0, total_ms=2.0, budget_ms=33.3)
    assert diag.frame_count == 2


def test_frame_under_budget_is_not_dropped(diag: "renderer._FrameDiag") -> None:
    diag.record(render_ms=5.0, show_ms=5.0, total_ms=10.0, budget_ms=33.3)
    assert diag.drop_count == 0


def test_frame_over_budget_is_dropped(diag: "renderer._FrameDiag") -> None:
    diag.record(render_ms=20.0, show_ms=20.0, total_ms=40.0, budget_ms=33.3)
    assert diag.drop_count == 1


def test_frame_exactly_at_budget_is_not_dropped(diag: "renderer._FrameDiag") -> None:
    # Boundary case: strictly-greater-than, so a frame that lands exactly on
    # the budget (the common case at a quiet moment, floating-point noise
    # aside) doesn't get counted as behind schedule.
    diag.record(render_ms=10.0, show_ms=10.0, total_ms=33.3, budget_ms=33.3)
    assert diag.drop_count == 0


def test_max_total_ms_tracks_the_worst_frame(diag: "renderer._FrameDiag") -> None:
    diag.record(render_ms=1.0, show_ms=1.0, total_ms=5.0, budget_ms=33.3)
    diag.record(render_ms=1.0, show_ms=1.0, total_ms=50.0, budget_ms=33.3)
    diag.record(render_ms=1.0, show_ms=1.0, total_ms=12.0, budget_ms=33.3)
    assert diag.max_total_ms == 50.0


def test_mixed_batch_drop_count_and_totals(diag: "renderer._FrameDiag") -> None:
    # Three normal frames, two over budget -- checks the counters agree with
    # each other rather than each being individually plausible in isolation.
    for total_ms in (10.0, 12.0, 40.0, 11.0, 45.0):
        diag.record(render_ms=total_ms / 2, show_ms=total_ms / 2, total_ms=total_ms, budget_ms=33.3)
    assert diag.frame_count == 5
    assert diag.drop_count == 2
    assert diag.total_ms_total == pytest.approx(118.0)


def test_maybe_log_summary_waits_for_the_window(
    diag: "renderer._FrameDiag", caplog: pytest.LogCaptureFixture
) -> None:
    diag.record(render_ms=1.0, show_ms=1.0, total_ms=2.0, budget_ms=33.3)
    with caplog.at_level(logging.INFO, logger=renderer.LOGGER.name):
        diag.maybe_log_summary("pokemon", fps=30)
    assert caplog.records == []
    assert diag.frame_count == 1  # not reset -- window hasn't elapsed


def test_maybe_log_summary_emits_and_resets_after_window(
    diag: "renderer._FrameDiag", caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    diag.record(render_ms=5.0, show_ms=3.0, total_ms=8.0, budget_ms=33.3)
    diag.record(render_ms=40.0, show_ms=3.0, total_ms=43.0, budget_ms=33.3)
    # Force the window to look elapsed without an actual sleep.
    diag.window_started = time.monotonic() - renderer.FRAME_DIAG_SUMMARY_SEC - 1.0
    with caplog.at_level(logging.INFO, logger=renderer.LOGGER.name):
        diag.maybe_log_summary("youtube", fps=30)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "mode=youtube" in message
    assert "frames=2" in message
    assert "dropped=1" in message
    # Window resets so the next summary doesn't double-count these frames.
    assert diag.frame_count == 0
    assert diag.drop_count == 0
    assert diag.max_total_ms == 0.0


def test_maybe_log_summary_skips_empty_window(
    diag: "renderer._FrameDiag", caplog: pytest.LogCaptureFixture
) -> None:
    # No frames recorded at all -- must not emit (and must not divide by
    # zero computing the averages).
    diag.window_started = time.monotonic() - renderer.FRAME_DIAG_SUMMARY_SEC - 1.0
    with caplog.at_level(logging.INFO, logger=renderer.LOGGER.name):
        diag.maybe_log_summary("clock", fps=30)
    assert caplog.records == []


def test_frame_diag_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-import with no env var set: the module-level flag must default off
    # so normal operation never pays for the extra time.monotonic() calls or
    # logs anything new to journalctl.
    monkeypatch.delenv("TICKER_FRAME_DIAG", raising=False)
    reloaded = importlib.reload(renderer)
    assert reloaded.FRAME_DIAG_ENABLED is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES"])
def test_frame_diag_enabled_by_env_var(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TICKER_FRAME_DIAG", value)
    reloaded = importlib.reload(renderer)
    assert reloaded.FRAME_DIAG_ENABLED is True
    # Leave the module in its default state for any test that runs after.
    monkeypatch.delenv("TICKER_FRAME_DIAG", raising=False)
    importlib.reload(renderer)


@pytest.mark.parametrize("value", ["", "0", "false", "no", "garbage"])
def test_frame_diag_stays_disabled_for_other_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TICKER_FRAME_DIAG", value)
    reloaded = importlib.reload(renderer)
    assert reloaded.FRAME_DIAG_ENABLED is False
    monkeypatch.delenv("TICKER_FRAME_DIAG", raising=False)
    importlib.reload(renderer)
