# MIT License — Copyright (c) 2026 John Kuok
"""Full-screen ambient "vibes" -- a screensaver-style mode for the panel.

``vibes`` owns the whole 128x32 canvas: no clock, no header, no ticker.
It picks a single sub-vibe (currently campfire, rain, aquarium) driven
by the webapp picker and renders that scene every frame. Adding a vibe
means writing a class with ``render(canvas, tick)`` and dropping it in
``_VIBES``.

**Why one mode for many scenes.** Each vibe is small (100-200 LoC) and
they share nothing but the full-screen convention. Bundling them into
one mode keeps the mode grid at exactly one tile ("Vibes") that expands
into a picker on the settings card, mirroring how ``worldclock`` shows
an Analog/Digital toggle instead of shipping two top-level modes.

**Campfire.** The flame effect is the Doom PSX / "Fabien Sanglard fire"
cellular automaton: seed a hot row at the bottom, propagate upward one
row per frame, decay by 0-1 palette steps with a horizontal wind jitter.
That produces the flicker Doom's fire is known for at a cost of one
uint8 per pixel per frame -- easily 60fps on a Pi 5 for our 128x22
flame area. A hand-drawn log pile sits below the flames; embers on the
top of the logs pulse with a slow noise so the fire looks like it's
consuming something rather than floating above dead wood.

**Rain / Aquarium.** Stubbed with a light animation and a "COMING SOON"
label. They exist so the picker is not a one-item menu today and so a
future PR only has to fill in ``render`` rather than plumb the mode
selection end-to-end.
"""

from __future__ import annotations

import logging
import random
from typing import ClassVar, Protocol

from ticker.canvas import MEDIUM, SMALL, Canvas
from ticker.modes.base import Mode

LOGGER = logging.getLogger(__name__)


class _Vibe(Protocol):
    """A vibe owns the full canvas and updates its own internal state.

    ``render`` is called once per frame. Vibes may keep mutable state
    (particle positions, plasma buffers) between calls; the mode never
    resets them, so a vibe that needs a warm-up should render sensibly
    from tick 0.
    """

    def render(self, canvas: Canvas, tick: int) -> None: ...


# ---------------------------------------------------------------------------
# Campfire
# ---------------------------------------------------------------------------
#
# The flame palette is 32 entries wide, indexed by "heat": 0 = cold (black),
# 31 = hot (near-white). This is the classic Doom-fire ramp -- black through
# deep red, orange, yellow, and up into pale yellow just short of white so
# the tips of the flame twinkle without ever going full white (a fully-white
# LED at panel scale reads as a bug rather than a fire tip).
#
# The values were tuned on the panel: raising the red channel before the
# green kicks in gives the deep-orange "coals" band that reads as heat;
# holding green flat above orange lets the yellow tips stay saturated
# instead of washing out.
_CAMPFIRE_PALETTE: tuple[tuple[int, int, int], ...] = (
    (0,   0,   0),   # 0  cold -- extinguished pixel
    (7,   7,   7),   # 1  faintest smoke
    (31,  7,   7),   # 2
    (47,  15,  7),   # 3
    (71,  15,  7),   # 4
    (87,  23,  7),   # 5
    (103, 31,  7),   # 6
    (119, 31,  7),   # 7
    (143, 39,  7),   # 8
    (159, 47,  7),   # 9
    (175, 63,  7),   # 10
    (191, 71,  7),   # 11
    (199, 71,  7),   # 12
    (223, 79,  7),   # 13  hot orange -- reads as "flame body"
    (223, 87,  7),   # 14
    (223, 87,  7),   # 15
    (215, 95,  7),   # 16
    (215, 95,  7),   # 17
    (215, 103, 15),  # 18
    (207, 111, 15),  # 19
    (207, 119, 15),  # 20
    (207, 127, 15),  # 21
    (207, 135, 23),  # 22
    (199, 135, 23),  # 23
    (199, 143, 23),  # 24
    (199, 151, 31),  # 25  yellow band -- upper flame
    (191, 159, 31),  # 26
    (191, 159, 31),  # 27
    (191, 167, 39),  # 28
    (191, 167, 39),  # 29
    (191, 175, 47),  # 30
    (215, 199, 87),  # 31  tip -- pale yellow, kept short of white
)

# Log pile lives in the bottom rows. Six pixels of "logs" sit at the very
# bottom, and the flame occupies most of the panel above them so it has
# room to actually rise. The fire's fuel row sits ONE row above the log
# crest -- i.e. the flames appear to shoot up from just above the logs.
#
# The Doom PSX fire needs vertical room to look like fire: with only 4-5
# rows the flame reads as a bright edge, not a flame. Twenty-plus rows
# is where the tips start visibly waving and the classic wobble emerges.
_LOG_ROWS = 5
_LOG_TOP = 32 - _LOG_ROWS   # y = 27
# Flame buffer covers rows 0..26 (27 rows visible + 1 hidden fuel row).
# The fuel row is fed in just above the log crest so heat drives up the
# whole panel, letting the tips lick the top edge.
_FIRE_TOP = 0
_FIRE_HEIGHT = _LOG_TOP - _FIRE_TOP  # rows of *visible* flame (0..26)

# Log palette. Kept dim so the logs read as silhouettes with warmth on top
# rather than competing with the flames for attention. The "ember" colour
# is what the automaton pulses along the log crest to sell the "burning"
# read; without it the logs look like a dead prop.
_LOG_DARK = (55, 30, 15)   # deep brown -- log body
_LOG_MID = (95, 55, 25)    # highlight along the top of each log
_EMBER = (215, 87, 15)     # pulsing coal on the crest
_EMBER_HOT = (231, 143, 31)  # brief brighter flash


class _Campfire:
    """Doom-fire plasma flames sitting on a hand-drawn log pile.

    The plasma buffer is a 2D uint-ish array of heat values (0..31) that
    is one row taller than the visible flame area: the extra row at the
    bottom is the always-hot fuel row that seeds each frame. Every frame
    we walk the buffer bottom-up and for each cell copy the row below,
    decaying by 0-1 and shifting horizontally by -1..1 (Doom did it with
    a signed rand & 3; we do the same). That is the entire secret.

    A key convention: the buffer's ``[0]`` row is the TOP of the visible
    flame, ``[-1]`` is the fuel row. Rendering walks rows and paints
    palette[heat] at ``(x, _FIRE_TOP - 1 + row_index)``. The one-row
    overlap with the log crest is intentional: it makes the flames look
    like they're attached to the wood instead of levitating.
    """

    # Buffer geometry. Width matches the panel; height is the visible
    # flame area plus one hidden fuel row at the bottom that stays hot
    # every frame. On a 32-row panel with a 5-row log pile this gives
    # 27 visible flame rows -- enough for the flame tips to actually
    # wave rather than reading as a bright edge.
    _BUFFER_HEIGHT = _FIRE_HEIGHT + 1  # includes fuel row

    def __init__(self) -> None:
        # Deterministic seed on init so preview captures + tests are
        # reproducible. Real-world uses reseed via ``random.random()``
        # on every propagation step, so the fire still flickers.
        self._rng = random.Random(0xC0FFEE)
        # 2D list of ints, [y][x], top row first, height includes fuel.
        self._buffer: list[list[int]] = [
            [0] * 128 for _ in range(self._BUFFER_HEIGHT)
        ]
        # Fuel row is the last row -- kept hot every frame.
        for x in range(128):
            self._buffer[-1][x] = 31
        # Ember phase advances slowly so the log crest pulses out of
        # sync with the flame body, which is what real coals look like.
        self._ember_phase = 0

    def _step(self) -> None:
        """Advance the plasma one row upward with wind + decay jitter.

        Walks rows top-down (destination first). For each destination
        cell we sample the row below at ``x + wind`` and decay by 0-1.
        Top-down is fine because each destination only reads from the
        source row below it, which we haven't touched yet this frame.

        ``wind`` is ``rand() & 3`` -> 0..3, treated as a signed shift:
        0 -> stay, 1 -> +1, 2 -> -1, 3 -> +2. Doom used a wider set;
        at 128 wide with 32 rows visible the narrower set reads tighter
        without losing the wobble.
        """
        buf = self._buffer
        rng = self._rng
        # Fuel is CONCENTRATED, not panel-wide. A real campfire has
        # heat only above the log pile; if we seed the full 128 px
        # base row uniformly, the flame reads as a squat 128 x 27
        # wall of fire. Narrowing the fuel to the centre of the panel
        # gives a much taller silhouette even though the buffer height
        # is unchanged, because the same heat now propagates through
        # a narrow column instead of a wide one.
        #
        # Window layout:
        #   HOT_CORE  = full-intensity fuel where the log pile sits.
        #   WARM_EDGE = tapered fuel band on either side of the core --
        #               a few flickering sparks so the fire has some
        #               width variation instead of a hard-edged pillar.
        #   COLD      = everything outside; permanently zero.
        HOT_CORE_L, HOT_CORE_R = 40, 88     # 48 px hot column, centred
        WARM_L, WARM_R         = 28, 100    # 24 px total taper (12 each side)
        fuel = buf[-1]
        for x in range(128):
            if HOT_CORE_L <= x < HOT_CORE_R:
                # Same tri-state as before, but only inside the core:
                # ~40% hot / ~30% mid / ~30% cold. The cold pixels
                # inside the core are what create the flame TONGUES;
                # without them we're back to a solid wall.
                r = rng.random()
                if r > 0.60:
                    fuel[x] = 31
                elif r > 0.30:
                    fuel[x] = rng.randint(18, 28)
                else:
                    fuel[x] = 0
            elif WARM_L <= x < WARM_R:
                # Sparse warm sparks around the edges -- most of the
                # time they're cold, occasionally a mid or hot spark
                # pops so the flame doesn't have a hard vertical edge.
                r = rng.random()
                if r > 0.85:
                    fuel[x] = rng.randint(18, 28)
                else:
                    fuel[x] = 0
            else:
                fuel[x] = 0

        # Propagate up. Note that ``rng.randint(0, 3)`` is a hot loop --
        # each is a Python-level call. On a Pi 5 with 128 * (fire_height)
        # cells this comes in well under a millisecond even at 30fps.
        for y in range(self._BUFFER_HEIGHT - 2, -1, -1):
            src = buf[y + 1]
            dst = buf[y]
            for x in range(128):
                rand = rng.randint(0, 3)
                if rand == 0:
                    sx = x
                elif rand == 1:
                    sx = x + 1 if x + 1 < 128 else x
                elif rand == 2:
                    sx = x - 1 if x - 1 >= 0 else x
                else:
                    sx = x + 2 if x + 2 < 128 else x
                # Decay by rand (0..3). This is the Doom PSX trick --
                # aggressive decay means heat runs out well before the
                # top of the panel, so the flame tapers and tips fade
                # to black instead of holding a bright ceiling.
                heat = src[sx] - rand
                dst[x] = heat if heat > 0 else 0

    def _draw_logs(self, canvas: Canvas) -> None:
        """Two logs angled inward like a teepee.

        Each log is a parallelogram: a diagonal band with its long axis
        running at ~30 degrees from horizontal. Both the TOP edge and
        the BOTTOM edge slope in the same direction, so the whole log
        reads as tilted rather than as a stack of shifted horizontal
        bars. An earlier attempt fixed the base at y=31 and only slanted
        the top edge -- the eye read the flat baseline as a floor and
        the whole thing came off as horizontal.

        Geometry: each log has a length (along its axis), a thickness
        (across its axis), a centre point, and a slope. We walk the
        length in unit steps and paint a short vertical column of
        ``thickness`` px at each step, offset by ``slope * step`` on
        the y axis. That gives a genuine diagonal band whose top and
        bottom edges are parallel and both slanted.
        """
        # Each log: (x_start, y_start, x_end, y_end, thickness).
        # (x_start, y_start) is the LOW end of the log, (x_end, y_end)
        # is the HIGH end. The log leans from the low outer corner
        # toward the high inner corner, meeting the other log near the
        # centre of the panel a couple rows above the panel floor.
        #
        # Panel is 128x32. Logs sit in the bottom 8 rows so there's
        # room for a real diagonal to develop -- a 5-row-tall log at
        # ~30 degrees only slopes 3 px, which reads as a jaggy horizontal.
        # A steeper drop (8 rows over ~40 px of run) reads unmistakably
        # as "leaning inward".
        log_specs = (
            # x_lo, y_lo, x_hi, y_hi, thickness
            (2,   31, 58, 24, 4),  # left  log: low-left to high-right
            (70,  24, 126, 31, 4), # right log: high-left  to low-right
        )

        for x_lo, y_lo, x_hi, y_hi, thickness in log_specs:
            dx = x_hi - x_lo
            dy = y_hi - y_lo
            # Walk the longer axis in unit steps and interpolate the
            # other. dx dominates for these near-horizontal logs.
            steps = max(abs(dx), abs(dy))
            for i in range(steps + 1):
                t = i / steps
                cx = int(round(x_lo + dx * t))
                cy = int(round(y_lo + dy * t))
                # Paint a short vertical stripe of ``thickness`` px
                # centred on (cx, cy). Vertical stripes stack cleanly
                # for a mostly-horizontal log; a fully rotated stripe
                # would need proper line-drawing math for what buys
                # nothing at 128x32.
                for k in range(thickness):
                    py = cy - thickness // 2 + k
                    if 0 <= py < 32 and 0 <= cx < 128:
                        # Top pixel of the stripe is the crest -- paint
                        # it in the highlight colour so the firelight
                        # line runs along the top edge of the log.
                        colour = _LOG_MID if k == 0 else _LOG_DARK
                        canvas.pixel(cx, py, colour)

        # Ember dots along each log's crest. Positions are deterministic
        # (seeded RNG separate from the plasma) so a given ember stays
        # in one spot and pulses in place, like a real coal.
        ember_rng = random.Random(0xE1BE12)
        embers_per_log = 6
        for x_lo, y_lo, x_hi, y_hi, thickness in log_specs:
            dx = x_hi - x_lo
            dy = y_hi - y_lo
            steps = max(abs(dx), abs(dy))
            crest_dy = -(thickness // 2)
            # Choose a handful of ``t`` values along the log's length,
            # skipping the very ends so the embers don't fall off the
            # rounded log tips.
            for _ in range(embers_per_log):
                t = ember_rng.uniform(0.2, 0.8)
                ex = int(round(x_lo + dx * t))
                ey = int(round(y_lo + dy * t)) + crest_dy
                offset = ember_rng.randint(0, 15)
                phase = (self._ember_phase + offset) % 32
                if phase == 0:
                    color = (0, 0, 0)
                elif phase < 8:
                    color = _EMBER_HOT
                else:
                    color = _EMBER
                if 0 <= ex < 128 and 0 <= ey < 32:
                    canvas.pixel(ex, ey, color)

    def render(self, canvas: Canvas, tick: int) -> None:
        # Step the plasma once per frame. At the panel's 30fps this gives
        # a flame that visibly flickers without stuttering; at half that
        # rate (webapp preview strider) it still reads as fire because
        # each step is a full palette-scale shift.
        self._step()
        canvas.clear()
        # Draw flames from the top of the panel down to the log crest.
        # Buffer row 0 == top of visible flame == panel row _FIRE_TOP (=0).
        # Buffer row _FIRE_HEIGHT - 1 == panel row _LOG_TOP - 1 (just above
        # the logs). Row _FIRE_HEIGHT is the hidden fuel row; we don't
        # paint it because the log crest covers that row.
        buf = self._buffer
        for row_index in range(_FIRE_HEIGHT):
            y = _FIRE_TOP + row_index
            row = buf[row_index]
            for x in range(128):
                heat = row[x]
                if heat > 1:
                    canvas.pixel(x, y, _CAMPFIRE_PALETTE[heat])
        # Logs on top of the flames, then embers on top of the logs. The
        # log body covers the bottommost flame row so the fire looks like
        # it emerges from the wood rather than floating just above it.
        self._draw_logs(canvas)
        # Advance the ember phase every ~4 ticks so the pulse is slower
        # than the flame flicker. Real coals breathe over seconds, not
        # frames.
        if tick % 4 == 0:
            self._ember_phase = (self._ember_phase + 1) % 32


# ---------------------------------------------------------------------------
# Rain / Aquarium -- stubs
# ---------------------------------------------------------------------------
#
# These are intentionally minimal: a very light animation + a "COMING SOON"
# label so the picker isn't a one-item menu. When a real implementation
# lands, replace the ``render`` body -- the mode-selection and webapp
# plumbing don't have to change.


class _Rain:
    """Placeholder: dim blue diagonal streaks with a COMING SOON banner."""

    def __init__(self) -> None:
        # Streak positions carried across frames so the streaks appear
        # to fall rather than jitter each tick. Each entry is ``(x, y)``.
        rng = random.Random(0xBA5EBA11)
        self._streaks: list[list[int]] = [
            [rng.randint(0, 127), rng.randint(0, 31)] for _ in range(40)
        ]
        self._rng = rng

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        for streak in self._streaks:
            x, y = streak
            # Two-pixel tail for a bit of motion feel.
            canvas.pixel(x, y, (60, 90, 160))
            canvas.pixel(x, (y - 1) % 32, (30, 45, 90))
            # Diagonal fall: 1 down, 1 across every two frames.
            streak[1] = (y + 1) % 32
            if tick % 2 == 0:
                streak[0] = (x - 1) % 128
        canvas.text_centered(12, "RAIN", (180, 200, 235), MEDIUM)
        canvas.text_centered(24, "COMING SOON", (110, 130, 170), SMALL)


class _Aquarium:
    """Placeholder: a couple of drifting fish shapes + COMING SOON."""

    def __init__(self) -> None:
        # Two "fish" as (x, y, direction). Direction is +1 or -1.
        self._fish: list[list[int]] = [
            [10, 8, 1],
            [100, 20, -1],
        ]

    def render(self, canvas: Canvas, tick: int) -> None:
        # Dim blue water: draw a soft top-to-bottom gradient of two
        # colors so the background reads as water even at this size.
        for y in range(32):
            if y < 16:
                color = (5, 15, 45)
            else:
                color = (5, 20, 60)
            canvas.fill_rect(0, y, 128, 1, color)
        # Occasional bubble rising from the bottom (deterministic modulo
        # tick so the preview builder stays reproducible).
        if tick % 12 == 0:
            bx = (tick * 7) % 128
            canvas.pixel(bx, 31 - ((tick // 12) % 20), (140, 190, 230))
        # Fish are 5x3 pixel silhouettes; direction determines which
        # end holds the tail. Simple bit-pattern draw -- kept inline
        # rather than reused from a bitmap because there are only two.
        for fish in self._fish:
            x, y, direction = fish
            if direction == 1:
                # Head right: >==<
                pattern = (
                    " xxx>",
                    "xxxxx",
                    " xxx>",
                )
            else:
                pattern = (
                    "<xxx ",
                    "xxxxx",
                    "<xxx ",
                )
            for dy, row in enumerate(pattern):
                for dx, ch in enumerate(row):
                    if ch != " ":
                        canvas.pixel(
                            (x + dx) % 128,
                            (y + dy) % 32,
                            (180, 130, 60) if ch in "<>" else (220, 160, 80),
                        )
            # Advance every other frame so the fish drift instead of
            # zooming across.
            if tick % 2 == 0:
                fish[0] = (x + direction) % 128
        canvas.text_centered(2, "AQUARIUM", (170, 200, 235), SMALL)
        canvas.text_centered(24, "COMING SOON", (110, 140, 180), SMALL)


# ---------------------------------------------------------------------------
# Registry + mode wrapper
# ---------------------------------------------------------------------------

# Ordered dict so the webapp picker shows the vibes in a stable order;
# campfire is first because it's the flagship of this mode.
_VIBES: dict[str, tuple[str, type]] = {
    "campfire": ("Campfire", _Campfire),
    "rain":     ("Rain",     _Rain),
    "aquarium": ("Aquarium", _Aquarium),
}

DEFAULT_VIBE = "campfire"


def vibe_labels() -> dict[str, str]:
    """``{key: display_name}`` for the webapp picker."""
    return {key: label for key, (label, _cls) in _VIBES.items()}


def valid_vibes() -> tuple[str, ...]:
    """Frozen tuple of accepted vibe keys, in display order."""
    return tuple(_VIBES.keys())


class VibesMode(Mode):
    """Full-screen ambient screensaver. Renders one vibe at a time.

    The active vibe is read from ``config.current_vibe()`` on every
    frame so the webapp picker takes effect immediately -- no service
    restart, no waiting for a cache to expire. The vibe instances are
    lazily built on first use and cached so switching back to a vibe
    keeps its state (a plasma buffer that was already warmed up looks
    natural on return; a cold-start plasma looks like a lit match).
    """

    _instances: ClassVar[dict[str, object]] = {}  # class-level; harmless singleton

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        # Per-mode-instance cache so a config with a fresh state dir gets
        # a fresh cache without leaking across renderer restarts. The
        # ``ClassVar`` above is not used; kept for documentation intent
        # in case a subclass wants a shared cache.
        self._cache: dict[str, object] = {}

    def _get_vibe(self, key: str) -> object:
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        entry = _VIBES.get(key)
        if entry is None:
            # A bad value on disk shouldn't crash the panel. Fall back
            # to the default vibe and log once so it shows up in
            # ``journalctl -u ticker`` without spamming.
            LOGGER.warning(
                "vibes: unknown vibe %r; falling back to %s",
                key, DEFAULT_VIBE,
            )
            entry = _VIBES[DEFAULT_VIBE]
        _label, cls = entry
        instance = cls()
        self._cache[key] = instance
        return instance

    def render(self, canvas: Canvas, tick: int) -> None:
        vibe_key = self.config.current_vibe()
        vibe = self._get_vibe(vibe_key)
        vibe.render(canvas, tick)  # type: ignore[attr-defined]
