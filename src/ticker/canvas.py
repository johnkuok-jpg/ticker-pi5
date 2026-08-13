# MIT License — Copyright (c) 2026 John Kuok
"""Small, PIL-backed drawing API shared by all ticker modes."""

from __future__ import annotations

from pathlib import Path
from typing import TypeAlias

from PIL import Image, ImageDraw, ImageFont

Color: TypeAlias = tuple[int, int, int]


class Canvas:
    """An RGB PIL image plus display-oriented drawing conveniences."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.image_buffer = Image.new("RGB", (width, height), (0, 0, 0))
        self._draw = ImageDraw.Draw(self.image_buffer)

    def clear(self, color: Color = (0, 0, 0)) -> None:
        self._draw.rectangle((0, 0, self.width, self.height), fill=color)

    @staticmethod
    def _font(font_size: int) -> ImageFont.ImageFont:
        """Use the normal system bitmap font if scalable DejaVu is unavailable."""
        for candidate in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ):
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, font_size)
        return ImageFont.load_default()

    def text(self, x: int, y: int, text: str, color: Color, font_size: int = 8) -> None:
        self._draw.text((x, y), text, font=self._font(font_size), fill=color, stroke_width=0)

    def text_width(self, text: str, font_size: int = 8) -> int:
        bbox = self._draw.textbbox((0, 0), text, font=self._font(font_size))
        return bbox[2] - bbox[0]

    def scroll_text(
        self, y: int, text: str, color: Color, offset: int, font_size: int = 8, gap: int = 20
    ) -> None:
        """Draw a seamlessly repeating, left-moving text string."""
        width = self.text_width(text, font_size)
        if width <= 0:
            return
        period = width + gap
        start_x = -(offset % period)
        for x in range(start_x, self.width + period, period):
            self.text(x, y, text, color, font_size)

    def image(self, x: int, y: int, pil_image: Image.Image) -> None:
        """Blit an image, retaining alpha when a logo supplies one."""
        image = pil_image.convert("RGBA")
        self.image_buffer.paste(image, (x, y), image)

    def pixel(self, x: int, y: int, color: Color) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self._draw.point((x, y), fill=color)
