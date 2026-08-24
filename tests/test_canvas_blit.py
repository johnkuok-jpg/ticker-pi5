# MIT License — Copyright (c) 2026 John Kuok
"""Tests for ``Canvas.blit_rgb``.

The blit exists for the campfire's fluid solver: a 128x29 flame band
painted through ``Canvas.pixel`` costs ~3.7k Python calls per frame,
which is the difference between comfortable and marginal at 30fps on a
Pi 5. Because it bypasses the per-pixel path it also bypasses that
path's bounds checking, so the clipping branch is the part worth
pinning -- an off-panel paste must crop, never raise and never make PIL
silently drop the whole block.
"""

from __future__ import annotations

from ticker.canvas import Canvas


def _block(width: int, height: int, colour: tuple[int, int, int]):
    return [[colour for _ in range(width)] for _ in range(height)]


def test_blit_rgb_pastes_a_nested_sequence_block() -> None:
    """Nested sequences work, not just numpy arrays.

    The fast path uses ``ndarray.tobytes``; the fallback flattens any
    sequence of sequences of triples. The fallback is what runs when
    numpy is missing, so it has to actually work.
    """
    canvas = Canvas(16, 8)
    canvas.blit_rgb(2, 3, _block(4, 2, (200, 100, 50)))

    img = canvas.image_buffer
    assert img.getpixel((2, 3)) == (200, 100, 50)
    assert img.getpixel((5, 4)) == (200, 100, 50)
    # Just outside the block on every side stays untouched.
    assert img.getpixel((1, 3)) == (0, 0, 0)
    assert img.getpixel((6, 3)) == (0, 0, 0)
    assert img.getpixel((2, 2)) == (0, 0, 0)
    assert img.getpixel((2, 5)) == (0, 0, 0)


def test_blit_rgb_clips_a_block_that_hangs_off_the_panel() -> None:
    """Overhanging blocks are cropped, and the on-panel part still lands.

    PIL's ``paste`` will happily drop a block whose box falls partly
    outside the image, so the source is cropped explicitly. A regression
    here would show up as an effect that vanishes entirely the moment it
    is positioned near an edge.
    """
    canvas = Canvas(16, 8)
    # Straddles the right and bottom edges.
    canvas.blit_rgb(14, 6, _block(6, 6, (10, 20, 30)))
    img = canvas.image_buffer
    assert img.getpixel((14, 6)) == (10, 20, 30)
    assert img.getpixel((15, 7)) == (10, 20, 30)

    # Straddles the left and top edges: the negative-offset source rows
    # and columns are dropped and the rest lands at the origin.
    canvas2 = Canvas(16, 8)
    canvas2.blit_rgb(-2, -1, _block(4, 3, (40, 50, 60)))
    img2 = canvas2.image_buffer
    assert img2.getpixel((0, 0)) == (40, 50, 60)
    assert img2.getpixel((1, 1)) == (40, 50, 60)
    assert img2.getpixel((2, 0)) == (0, 0, 0)


def test_blit_rgb_ignores_empty_and_fully_offscreen_blocks() -> None:
    """Degenerate inputs are no-ops rather than exceptions.

    A solver that produces a zero-height band during startup, or an
    effect scrolled fully past the edge, must not take the whole render
    loop down with it.
    """
    canvas = Canvas(16, 8)
    canvas.blit_rgb(0, 0, [])
    canvas.blit_rgb(0, 0, [[]])
    canvas.blit_rgb(40, 40, _block(4, 4, (255, 255, 255)))
    canvas.blit_rgb(-10, 0, _block(4, 4, (255, 255, 255)))
    assert set(canvas.image_buffer.convert("RGB").getdata()) == {(0, 0, 0)}


def test_blit_rgb_pastes_flat_per_row_byte_buffers() -> None:
    """Rows may be flat ``3*w`` byte buffers, not just triple sequences.

    The driving vibe builds each row as a single ``bytearray`` and mutates
    it in place while rasterizing spans, because allocating 128 tuples per
    row per frame is exactly the sort of overhead a pure-Python rasterizer
    cannot carry at 30fps. If this path regressed to being interpreted as
    a sequence of triples the whole scene would smear sideways rather than
    fail loudly, so pin the byte layout.
    """
    canvas = Canvas(16, 8)
    row = bytearray()
    for _ in range(4):
        row += bytes((200, 100, 50))
    canvas.blit_rgb(2, 3, [row, bytes(row)])

    img = canvas.image_buffer
    assert img.getpixel((2, 3)) == (200, 100, 50)
    assert img.getpixel((5, 3)) == (200, 100, 50)
    # Second row proves ``bytes`` works alongside ``bytearray``.
    assert img.getpixel((5, 4)) == (200, 100, 50)
    assert img.getpixel((6, 3)) == (0, 0, 0)
    assert img.getpixel((2, 5)) == (0, 0, 0)


def test_blit_rgb_clips_flat_per_row_buffers_at_the_edges() -> None:
    """The flat path shares the nested path's cropping, including x < 0.

    Cropping a flat row means slicing at ``3*dx``, not ``dx`` -- an
    off-by-three here would shift colour channels and tint the scene
    instead of moving it, which is a bug that survives a smoke test.
    """
    canvas = Canvas(16, 8)
    row = bytes((10, 20, 30)) * 4
    canvas.blit_rgb(-2, 0, [row])
    img = canvas.image_buffer
    assert img.getpixel((0, 0)) == (10, 20, 30)
    assert img.getpixel((1, 0)) == (10, 20, 30)
    assert img.getpixel((2, 0)) == (0, 0, 0)
