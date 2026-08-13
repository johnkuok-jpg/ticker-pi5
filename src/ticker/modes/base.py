# MIT License — Copyright (c) 2026 John Kuok
"""Base contract for all display modes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ticker.canvas import Canvas
from ticker.config import Config


class Mode(ABC):
    """A stateful visual mode rendered on every display frame."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def clock_text(self, tick: int) -> str:
        """Wall clock whose colon blinks once a second, like a bedside clock.

        The blink is driven off *tick* rather than the system clock so it stays
        deterministic: the preview renderer can capture the loop and tests can
        assert on it. A dropped frame shifts the phase slightly, which is
        invisible on a blink and cheaper than reading the clock twice a frame.

        The panel fonts are fixed-cell, so swapping the colon for a space keeps
        the string exactly as wide and the digits do not shuffle sideways.
        """
        text = self.config.clock_text()
        half_second = max(1, round(self.config.fps / 2))
        if (tick // half_second) % 2:
            return text.replace(":", " ")
        return text

    @abstractmethod
    def render(self, canvas: Canvas, tick: int) -> None:
        """Draw one complete frame onto *canvas*. Never raise for network failures."""
