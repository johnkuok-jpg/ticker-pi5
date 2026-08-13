# MIT License — Copyright (c) 2026 John Kuok
"""Market session clock: is the market open, and how long until that changes.

The stocks screen shows prices but cannot say whether they are live or three
days stale. This screen answers that question and nothing else. It needs no
network, so it is the one mode that still tells the truth when the WiFi drops.
"""

from __future__ import annotations

from ticker import market
from ticker.canvas import LARGE, MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

WHITE = (235, 240, 250)
DIM = (72, 84, 106)
TRACK = (30, 38, 54)
AMBER = (255, 176, 0)

ROW_TWO_Y = 19
BAR_Y = 30


class MarketMode(Mode):
    """Session phase, a countdown, and a progress bar across the trading day."""

    #: Seconds each item holds in the rotating second row.
    DETAIL_ROTATE_SECONDS = 4

    def _detail_items(self, state: market.SessionState) -> list[tuple[str, tuple[int, int, int]]]:
        items: list[tuple[str, tuple[int, int, int]]] = [(state.countdown_label, WHITE)]
        if state.note:
            items.append((state.note, AMBER))
        if not state.calendar_known:
            # Say so rather than assert a holiday is a normal weekday.
            items.append(("NO HOLIDAY DATA", DIM))
        return items

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        state = market.session_state(self.config.now())

        canvas.text(0, 0, state.label, state.color, LARGE)

        clock = self.clock_text(tick)
        clock_width = canvas.text_width(clock, MEDIUM)
        label_end = canvas.text_width(state.label, LARGE)
        if label_end + 4 + clock_width <= canvas.width:
            canvas.text(canvas.width - clock_width, 2, clock, DIM, MEDIUM)

        items = self._detail_items(state)
        frames = max(1, int(self.DETAIL_ROTATE_SECONDS * max(1, self.config.fps)))
        text, color = items[(tick // frames) % len(items)]
        canvas.text(0, ROW_TWO_Y, canvas.fit(text, canvas.width, SMALL), color, SMALL)

        # Empty track always drawn, so the bar reads as a bar at 0% rather than
        # as a stray line, and fills only while the regular session runs.
        canvas.hline(BAR_Y, TRACK)
        if state.progress is not None:
            filled = int(round(state.progress * canvas.width))
            if filled > 0:
                canvas.fill_rect(0, BAR_Y, filled, 1, state.color)
