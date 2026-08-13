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

    @abstractmethod
    def render(self, canvas: Canvas, tick: int) -> None:
        """Draw one complete frame onto *canvas*. Never raise for network failures."""
