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

**Rain.** Drops-on-a-window night scene: a shallow midnight-blue vertical
gradient stands in for the sky through the pane, and up to ~20 drops slide
down with fading trails, size-scaled speed, occasional surface-tension
pauses, and merge-on-contact (a bigger drop swallows a smaller one and
speeds up, exactly like real drops racing down a window). Every 20-40 s
a single-frame lightning strike screen-blends bright silver across the
whole panel and decays over ~4 frames, no branching bolts (they read as
noise on 32 rows).

**Aquarium.** Underwater tank diorama. A vertical blue gradient stands
in for water depth, with a broken caustic ripple animated across the
top 3 rows. Along the bottom a two-row sand strip anchors six swaying
seaweed fronds (top of each frond wobbles more than the base, exactly
like a real weed in a current). Two filter vents in the sand each
release bubbles on their own cadence -- bubbles wobble sideways as they
rise (bubbles in water actually do sway) and come in two sizes. Fish
come and go: a rotating cast of species (tang, angelfish, neon tetra,
clownfish, yellow tang, butterflyfish, pufferfish, blue chromis, betta,
shark) spawn just off-screen, drift across with a small triangular
y-bob, and exit the far side. 2-3 are always on screen; new species
stream in on a slow cadence so the tank never repeats itself. A crab
occasionally walks across the sand as a rare cameo.
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


# ---------------------------------------------------------------------------
# Rain-on-a-window palette + constants
# ---------------------------------------------------------------------------

# Night sky through the pane. A shallow top-to-bottom gradient sells
# the "looking out a window" read without any sky detail: darker at the
# bottom (deep glass), a hair lighter at the top (city light bleed).
_RAIN_BG_TOP    = (10, 14, 28)
_RAIN_BG_BOTTOM = (4,  6,  14)

# Drop colours. The head is a bright cool white -- water catching
# whatever ambient light there is. The trail decays through cooler,
# darker teal to nothing so a fresh drop reads as "currently sliding"
# and an old one reads as "passed by a moment ago".
_RAIN_HEAD          = (190, 215, 235)
_RAIN_TRAIL_STAGES  = (
    (140, 175, 210),  # freshest -- 1-2 ticks old
    (85,  120, 165),
    (50,  75,  110),
    (25,  40,  70),
    (12,  22,  45),   # oldest visible; below this the pixel is bg
)

# Lightning: a rare, dramatic flash. Frame 0 hits ~90% brightness on
# every panel pixel, then decays over the next few frames back to the
# base scene. During the flash the drops also read at silver so the
# whole scene feels lit from behind.
_RAIN_FLASH_STAGES = (
    (210, 220, 235),  # frame 0 -- full flash
    (150, 160, 180),  # frame 1
    (90,  100, 125),  # frame 2
    (45,  55,  80),   # frame 3 -- almost gone
)


class _Rain:
    """Water droplets sliding down a night window pane.

    Each drop tracks a *head* position plus a deque of recent positions
    that render as a fading trail -- the wet path down the glass. Drops
    mostly slide down but wobble left/right so no two paths look like
    a ruler line. When two drops touch, they merge: the bigger drop
    absorbs the smaller and speeds up, exactly the way real drops on
    a window pane rip through smaller drops on their way down.

    Runtime footprint: ~20 drops * ~10 trail positions each = ~200
    pixel writes per frame, plus 128*32 background paint. Trivial
    on a Pi 5.

    Lightning: on a random schedule (12-40 s between strikes) the
    whole panel briefly flashes bright, decays over 4 frames, then
    returns to the base scene. No branching bolts -- at 32 rows a
    branching bolt reads as random noise, not lightning; the flash
    alone carries the storm feel.
    """

    # Drop physics tuning.
    _MAX_DROPS      = 20
    _TRAIL_LEN      = len(_RAIN_TRAIL_STAGES)
    # Speed is fractional rows per tick. Small drops crawl (0.2-0.4),
    # bigger drops rip (0.6-1.2). Real drops on a window pane obey
    # size-and-gravity: bigger = faster.
    _MIN_SPEED      = 0.18
    _MAX_SPEED      = 0.45
    # Pause behaviour: a drop occasionally sticks for a second or two
    # (surface tension) before continuing. Each frame there's a small
    # chance to enter a pause state.
    _PAUSE_PROB     = 0.006
    _PAUSE_MIN      = 20   # ticks
    _PAUSE_MAX      = 90
    # Spawn cadence. A new drop appears every ~15-40 ticks until the
    # population reaches _MAX_DROPS.
    _SPAWN_MIN      = 12
    _SPAWN_MAX      = 45
    # Lightning cadence and shape.
    _LIGHTNING_MIN  = 20 * 30   # 20 s at 30 fps
    _LIGHTNING_MAX  = 40 * 30   # 40 s at 30 fps
    _FLASH_FRAMES   = len(_RAIN_FLASH_STAGES)

    def __init__(self) -> None:
        # Two RNGs. The first seeds the initial drop layout so the
        # first preview frame is reproducible; the second drives
        # per-frame stochastic behaviour (spawn timing, wobble, pauses,
        # merges) and is intentionally *not* seeded so the scene
        # doesn't loop across service restarts.
        seed_rng = random.Random(0xBA5EBA11)
        self._rng = random.Random()

        self._drops: list[_Rain._Drop] = []
        # Pre-populate so the first frame isn't an empty window.
        for _ in range(self._MAX_DROPS // 2):
            self._drops.append(self._new_drop(seed_rng, y=seed_rng.randint(0, 31)))

        # Countdown until the next spawn (frames).
        self._next_spawn = self._rng.randint(self._SPAWN_MIN, self._SPAWN_MAX)
        # Countdown until the next lightning strike (frames).
        self._next_flash = self._rng.randint(self._LIGHTNING_MIN, self._LIGHTNING_MAX)
        # Frames remaining in the current flash (0 = no flash active).
        self._flash_left = 0

    # ------------------------------------------------------------------
    # Drop bookkeeping
    # ------------------------------------------------------------------

    class _Drop:
        """One water drop. Mutated in place each frame.

        ``size`` is 1..3 and drives both the head brightness and the
        speed multiplier. Merges bump ``size`` by 1 (capped at 3).

        ``trail`` is a list of recent integer (x, y) grid cells the
        drop has occupied, oldest first. Fresh positions get bright
        trail colours; the tail fades to background.

        ``pause`` is a frame countdown; while > 0 the drop doesn't
        move (surface tension holding it in place).
        """

        __slots__ = ("x", "y", "speed", "size", "trail", "pause")

        def __init__(self, x: float, y: float, speed: float, size: int) -> None:
            self.x = x
            self.y = y
            self.speed = speed
            self.size = size
            self.trail: list[tuple[int, int]] = []
            self.pause = 0

    def _new_drop(self, rng: random.Random, y: float = -1.0) -> "_Rain._Drop":
        """Spawn a fresh drop at a random x, given y (or off-panel top)."""
        return _Rain._Drop(
            x=float(rng.randint(2, 125)),
            y=y,
            speed=rng.uniform(self._MIN_SPEED, self._MAX_SPEED),
            size=1,
        )

    # ------------------------------------------------------------------
    # Physics step
    # ------------------------------------------------------------------

    def _step(self, tick: int) -> None:
        rng = self._rng

        # Spawn pacing.
        self._next_spawn -= 1
        if self._next_spawn <= 0 and len(self._drops) < self._MAX_DROPS:
            self._drops.append(self._new_drop(rng))
            self._next_spawn = rng.randint(self._SPAWN_MIN, self._SPAWN_MAX)

        # Lightning pacing. Flash duration overrides everything else
        # visually; the countdown to the *next* flash only ticks down
        # while no flash is active.
        if self._flash_left > 0:
            self._flash_left -= 1
        else:
            self._next_flash -= 1
            if self._next_flash <= 0:
                self._flash_left = self._FLASH_FRAMES
                self._next_flash = rng.randint(self._LIGHTNING_MIN, self._LIGHTNING_MAX)

        # Move each drop.
        for drop in self._drops:
            if drop.pause > 0:
                drop.pause -= 1
                continue
            # Occasionally stick to the glass (surface tension).
            if rng.random() < self._PAUSE_PROB:
                drop.pause = rng.randint(self._PAUSE_MIN, self._PAUSE_MAX)
                continue
            # Record trail (integer cell) BEFORE moving so the trail
            # includes the current head; the head render on top will
            # paint over the freshest trail pixel with head colour.
            ix, iy = int(drop.x), int(drop.y)
            if 0 <= ix < 128 and 0 <= iy < 32:
                if not drop.trail or drop.trail[-1] != (ix, iy):
                    drop.trail.append((ix, iy))
                    if len(drop.trail) > self._TRAIL_LEN:
                        drop.trail.pop(0)
            # Advance. Speed scales with size (bigger drops fall faster).
            drop.y += drop.speed * (1.0 + 0.3 * (drop.size - 1))
            # Real drops on glass don't slalom -- they mostly go straight
            # down, occasionally lurching one pixel sideways when they
            # unstick from a pin point on the surface. Model that with a
            # rare discrete horizontal step, not a continuous sine wave.
            if rng.random() < 0.02:
                drop.x += rng.choice((-1.0, 1.0))

        # Merges: any two drops whose heads occupy the same or adjacent
        # cell fuse. Iterating an index pair is O(n^2) but n <= 20.
        merged: set[int] = set()
        for i, a in enumerate(self._drops):
            if i in merged:
                continue
            for j in range(i + 1, len(self._drops)):
                if j in merged:
                    continue
                b = self._drops[j]
                if abs(a.x - b.x) <= 1.2 and abs(a.y - b.y) <= 1.2:
                    # Bigger drop absorbs the smaller; if equal, the
                    # lower one wins (gravity's on its side).
                    if a.size > b.size or (a.size == b.size and a.y >= b.y):
                        winner, loser = a, b
                        merged.add(j)
                    else:
                        winner, loser = b, a
                        merged.add(i)
                    winner.size = min(3, winner.size + 1)
                    # Speed bumps too -- merged drop is heavier.
                    winner.speed = min(self._MAX_SPEED * 1.6,
                                       winner.speed + loser.speed * 0.3)
                    # A merge often kicks the winner free from any pause.
                    winner.pause = 0
                    if winner is b:
                        break  # ``a`` was absorbed; stop scanning j for it
        if merged:
            self._drops = [d for i, d in enumerate(self._drops) if i not in merged]

        # Retire drops that have slid off the bottom. Also retire drops
        # that have wandered off the sides (wobble accumulator drift).
        self._drops = [
            d for d in self._drops
            if -2 <= d.x < 130 and d.y < 34
        ]

    # ------------------------------------------------------------------
    # Draw
    # ------------------------------------------------------------------

    def _paint_background(self, canvas: Canvas) -> None:
        """Vertical gradient from midnight top to darker glass bottom."""
        for y in range(32):
            t = y / 31.0
            r = int(_RAIN_BG_TOP[0] + (_RAIN_BG_BOTTOM[0] - _RAIN_BG_TOP[0]) * t)
            g = int(_RAIN_BG_TOP[1] + (_RAIN_BG_BOTTOM[1] - _RAIN_BG_TOP[1]) * t)
            b = int(_RAIN_BG_TOP[2] + (_RAIN_BG_BOTTOM[2] - _RAIN_BG_TOP[2]) * t)
            canvas.fill_rect(0, y, 128, 1, (r, g, b))

    def render(self, canvas: Canvas, tick: int) -> None:
        self._step(tick)

        # Base scene: night gradient behind everything.
        self._paint_background(canvas)

        # Draw each drop's trail (oldest first so newer trail pixels
        # paint over older ones at any collision) then its head.
        for drop in self._drops:
            for idx, (tx, ty) in enumerate(drop.trail):
                # Oldest is trail[0] -> stage index len-1;
                # newest is trail[-1] -> stage index 0.
                age = len(drop.trail) - 1 - idx
                stage = _RAIN_TRAIL_STAGES[min(age, self._TRAIL_LEN - 1)]
                if 0 <= tx < 128 and 0 <= ty < 32:
                    canvas.pixel(tx, ty, stage)
            ix, iy = int(drop.x), int(drop.y)
            if 0 <= ix < 128 and 0 <= iy < 32:
                canvas.pixel(ix, iy, _RAIN_HEAD)
                # Bigger drops get a slight halo so size reads visually.
                if drop.size >= 2 and iy + 1 < 32:
                    canvas.pixel(ix, iy + 1, _RAIN_TRAIL_STAGES[0])
                if drop.size >= 3 and ix + 1 < 128:
                    canvas.pixel(ix + 1, iy, _RAIN_TRAIL_STAGES[0])

        # Lightning: composite a flash colour over everything by simply
        # brightening every pixel. On an LED panel additive brightening
        # over the whole scene reads exactly like a lightning bulb going
        # off behind the glass -- the drops still show through because
        # they were already brighter than the sky.
        if self._flash_left > 0:
            stage = _RAIN_FLASH_STAGES[self._FLASH_FRAMES - self._flash_left]
            fr, fg, fb = stage
            img = canvas.image_buffer
            for y in range(32):
                for x in range(128):
                    r, g, b = img.getpixel((x, y))
                    # Screen-blend so bright drops stay bright, dark
                    # sky lifts most: out = 255 - (255-a)(255-b)/255.
                    nr = 255 - ((255 - r) * (255 - fr)) // 255
                    ng = 255 - ((255 - g) * (255 - fg)) // 255
                    nb = 255 - ((255 - b) * (255 - fb)) // 255
                    img.putpixel((x, y), (nr, ng, nb))


# ---------------------------------------------------------------------------
# Aquarium palette + fish sprites
# ---------------------------------------------------------------------------

# Water gradient: light near the surface (sunlight coming through), dark
# at the abyss. Cooler blue-teal so the fish colours pop against it.
_AQ_BG_TOP    = (10, 40, 75)
_AQ_BG_BOTTOM = (2,  8,  20)

# Caustic ripple colour -- a hair brighter than the surface, painted in
# broken segments across the top few rows.
_AQ_CAUSTIC = (60, 110, 150)

# Bubble palette: two shades so bubbles read as tiny spheres, not dots.
_AQ_BUBBLE_LIGHT = (170, 210, 235)
_AQ_BUBBLE_DARK  = (90, 140, 180)

# Seaweed: dark green fronds that sway. Two shades for shape.
_AQ_WEED_DARK = (12, 55, 25)
_AQ_WEED_LIT  = (35, 110, 55)

# Sand row along the very bottom.
_AQ_SAND = (95, 80, 45)

# Fish sprites. Pixel-art bitmaps with four palette slots:
#   ``B`` = body (main colour)
#   ``D`` = body shadow (darker version of body, adds volume)
#   ``F`` = fin / band / highlight
#   ``E`` = eye (near-black)
#   space = transparent
# Sprites face RIGHT by default; the renderer mirrors them horizontally
# for the left-facing case. Sizes were bumped from the original 3-5 row
# sprites to 6-9 rows so they actually read as fish on a 32-row panel
# instead of coloured smudges.
_FISH_SPRITES: tuple[
    tuple[str, tuple[int, int, int], tuple[int, int, int],
          tuple[int, int, int], tuple[int, int, int]],
    ...,
] = (
    # Tang -- oval body, tall triangular tail on the left, small pectoral
    # fin, dark eye near the mouth. Warm orange with a shaded belly.
    (
        "    BBBBB   \n"
        "   BBBBBBBB \n"
        "F BBBBBBBEB \n"
        "FFBBBBBBBBB \n"
        "F BBBFBBBBB \n"
        "   DDDDBBB  \n"
        "    DDDDD   ",
        (240, 175, 55),   # body  -- warm orange
        (170, 100, 25),   # dark  -- shaded belly
        (255, 220, 120),  # fin   -- pale yellow (tail + pectoral)
        (10,  10,  15),   # eye
    ),
    # Angelfish -- tall diamond with sweeping top+bottom fins, long tail
    # trailing behind. Coral pink with cream highlights.
    (
        "     F   \n"
        "    FFF  \n"
        "    BB   \n"
        "   BBBB  \n"
        "FFBBBBBB \n"
        " FBBBBEB \n"
        "FFBBBBBB \n"
        "   BBBB  \n"
        "    FF   ",
        (225, 130, 130),  # body -- coral
        (160, 80,  85),   # dark -- shaded
        (255, 210, 195),  # fin  -- cream
        (10,  10,  15),   # eye
    ),
    # Neon tetra -- sleek torpedo with a bright lateral stripe (fin
    # colour) running the length of the body, forked tail.
    (
        "F  BBBBBBB  \n"
        "FF BFFFFFEB \n"
        "F  BBBBBBB  \n"
        "    DDDDD   ",
        (80,  220, 235),  # body -- neon cyan
        (30,  120, 140),  # dark -- belly stripe
        (240, 90,  110),  # fin  -- red neon stripe + tail edge
        (10,  10,  15),   # eye
    ),
    # Clownfish -- chunky oval with three cream vertical bands, forked
    # tail, small pectoral fin. Bright clown orange.
    (
        "   BFBBFBB  \n"
        "  BBFBBFBBB \n"
        "F BBFBBFBEB \n"
        "FFBBFBBFBBBF\n"
        "F BBFBBFBBBF\n"
        "  BBFBBFBBB \n"
        "   BFBBFBB  ",
        (250, 130, 45),   # body -- clown orange
        (180, 75,  20),   # dark -- shadow
        (255, 250, 240),  # fin  -- white bands + tail edge
        (10,  10,  15),   # eye
    ),
    # Yellow tang -- longer oval with a needle-thin caudal peduncle and
    # wide dorsal + anal fins. Solid canary yellow.
    (
        "     FFFFFF   \n"
        "   BBBBBBBB   \n"
        "F BBBBBBBBBB  \n"
        "FFBBBBBBBBBEB \n"
        "F BBBBBBBBBB  \n"
        "   BBBBBBBB   \n"
        "     FFFFFF   ",
        (250, 210, 40),   # body -- canary yellow
        (180, 140, 20),   # dark -- underside
        (255, 235, 120),  # fin  -- pale yellow
        (10,  10,  15),   # eye
    ),
    # Butterflyfish -- disc-shaped with a black eye stripe. Yellow front
    # half (with the face + eye), silver-white rear half. Drawn facing
    # right so the eye sits near the leading edge, same as every other
    # sprite in this catalog.
    (
        "    BBBB    \n"
        "   BBBBBBB  \n"
        " FFFFFBBBB  \n"
        " FFFFFBBEBF \n"
        " FFFFFBBBBF \n"
        " FFFFFBBBB  \n"
        "   BBBBBBB  \n"
        "    BBBB    ",
        (230, 230, 235),  # body -- silver white (rear half)
        (150, 150, 160),  # dark -- shaded silver
        (250, 205, 60),   # fin  -- yellow front half + tail edge
        (10,  10,  15),   # eye
    ),
    # Pufferfish -- nearly circular with tiny fins and a big eye. Sandy
    # tan with darker spots (represented by the dark palette slot).
    (
        "    BBBB    \n"
        "  BBBDBBDBB \n"
        " BBBBBBBBBBB\n"
        "FEBBDBBBDBBB\n"
        "FFBBBBBBBBBB\n"
        " BBBBDBBBDBB\n"
        "  BBBBBBBBB \n"
        "    BBBB    ",
        (215, 175, 100),  # body -- sandy tan
        (140, 100, 50),   # dark -- spots
        (245, 210, 145),  # fin  -- pale belly hint
        (10,  10,  15),   # eye
    ),
    # Blue chromis -- small fusiform damselfish, sleek and quick. Solid
    # vivid blue.
    (
        "F BBBBBBBB  \n"
        "FFBBBBBBBEB \n"
        "F BBBBBBBB  \n"
        "   DDDDDD   ",
        (65,  115, 235),  # body -- vivid blue
        (30,  60,  140),  # dark -- shaded belly
        (150, 190, 250),  # fin  -- pale sky
        (10,  10,  15),   # eye
    ),
    # Betta / fighting fish -- long trailing fins. Rich magenta.
    (
        "       FF   \n"
        "     BBBFF  \n"
        "   FBBBBBBB \n"
        "F FBBBBBBEB \n"
        "FFFBBBBBBBB \n"
        "F FBBBBBBBB \n"
        "   FBBBBBBB \n"
        "     BBBFF  \n"
        "       FF   ",
        (185, 45,  110),  # body -- magenta
        (110, 25,  70),   # dark -- shaded
        (240, 100, 175),  # fin  -- pink flare
        (10,  10,  15),   # eye
    ),
    # Shark -- big, sleek, distinctive dorsal fin, forked tail. Grey top,
    # white belly (the classic countershaded silhouette). ~24 px wide so
    # it reads as clearly larger than the reef fish above. The ``D``
    # (dark) palette slot is repurposed here as the WHITE belly since
    # a shark's shading is inverted vs the other fish -- lighter
    # underneath.
    (
        "           FF                \n"
        "          FFFF               \n"
        "F   BBBBBBBBBBBBBBBBBBBB     \n"
        "FF BBBBBBBBBBBBBBBBBBBBBBBBB \n"
        "FFFBBBBBBBBBBBBBBBBBBBBBBBEB \n"
        "FF DDDDDDDDDDDDDDDDDDDDDDD   \n"
        "F   DDDDDDDDDDDDDDDDDDD      ",
        (110, 125, 140),  # body -- grey
        (220, 225, 230),  # dark -- white belly
        (75,  85,  100),  # fin  -- dark grey (tail + dorsal + pectoral)
        (10,  10,  15),   # eye
    ),
)

# Index of the shark in the sprite catalog. The spawn system uses this to
# skew the shark's odds down (it should be a rare guest, not resident).
_SHARK_IDX = len(_FISH_SPRITES) - 1

# Crab sprite -- walks along the sand, doesn't swim. Small, wide, with
# two claws pointing out to the sides and a small pair of eyes on top.
# Faces the viewer (crabs walk sideways, so "facing left" vs "right"
# doesn't matter for orientation -- direction only flips claw asymmetry).
_CRAB_SPRITE = (
    "  E   E  \n"
    " BBBBBBB \n"
    "FBBBBBBBF\n"
    " B B B B "
)
_CRAB_BODY = (200, 70,  55)   # bright red-orange shell
_CRAB_DARK = (130, 30,  20)   # shadow (unused for now but kept for parity)
_CRAB_CLAW = (220, 100, 80)   # claw tip -- lighter than body
_CRAB_EYE  = (10,  10,  15)


class _Aquarium:
    """Underwater tank: fish drift, bubbles rise, seaweed sways.

    Composition mirrors a real aquarium diorama:

    * A vertical blue gradient for water depth.
    * A single row of caustic ripples along the top few rows -- broken
      segments animated with a slow phase so the surface shimmers.
    * A sand strip and swaying seaweed silhouettes at the bottom.
    * Rising bubbles from a couple of fixed vents in the sand -- real
      bubbles come from filter outlets, not random floor tiles.
    * A rotating cast of fish that spawn just off-screen, swim across
      the panel, and exit the other side (not the same 4 residents on
      loop). Species are drawn from a catalog of ~10, always keeping
      2-3 on screen so the tank never looks empty. A shark shows up
      occasionally (~5% of spawns) and cruises slower than the reef
      fish so it reads as a big animal.
    * A rare crab cameo: at random intervals (15-45 s) a single crab
      walks along the sand from one side to the other, then exits.
      Only one crab on screen at a time.

    Everything animates off ``tick`` plus an unseeded RNG for bubble
    spawn jitter so the scene doesn't loop across restarts.
    """

    _MAX_BUBBLES  = 30
    _BUBBLE_VENTS = (28, 92)      # x positions of the two sand vents
    _WEED_XS      = (10, 20, 44, 78, 108, 118)  # seaweed frond bases
    _SAND_Y       = 30            # top row of sand (rows 30, 31)
    # Fish spawn pacing. Keep at least this many fish on screen so the
    # tank never looks abandoned, and never more than _MAX_FISH so the
    # panel doesn't turn into a fish pile-up.
    _MIN_FISH     = 2
    _MAX_FISH     = 5
    _SPAWN_MIN    = 30   # frames between spawn attempts once above min
    _SPAWN_MAX    = 150

    class _Fish:
        __slots__ = ("sprite_idx", "x", "y", "y_phase", "speed", "direction")

        def __init__(self, sprite_idx: int, x: float, y: float,
                     y_phase: float, speed: float, direction: int) -> None:
            self.sprite_idx = sprite_idx
            self.x = x
            self.y = y
            self.y_phase = y_phase   # 0..1 progress through a sine bob cycle
            self.speed = speed       # px per tick, always positive
            self.direction = direction  # +1 = swim right, -1 = swim left

    class _Bubble:
        __slots__ = ("x", "y", "speed", "wobble", "big")

        def __init__(self, x: float, y: float, speed: float,
                     wobble: float, big: bool) -> None:
            self.x = x
            self.y = y
            self.speed = speed
            self.wobble = wobble  # accumulator for small lateral drift
            self.big = big

    class _Crab:
        __slots__ = ("x", "direction", "leg_phase")

        def __init__(self, x: float, direction: int) -> None:
            self.x = x
            self.direction = direction  # +1 walks right, -1 walks left
            self.leg_phase = 0          # ticks-since-spawn, drives leg anim

    # Crab pacing: only one crab at a time, and there's a long quiet
    # window between appearances so it reads as a rare cameo (not a
    # resident of the tank). 30 fps assumed -> 15-45 seconds.
    _CRAB_COOLDOWN_MIN = 15 * 30
    _CRAB_COOLDOWN_MAX = 45 * 30
    _CRAB_SPEED        = 0.15   # px per tick -- slow scuttle

    def __init__(self) -> None:
        seed_rng = random.Random(0xC0FFEEBE)
        self._rng = random.Random()

        # Pre-populate: seed 3 random fish at random x positions inside
        # the tank so the first frame isn't empty water. Everything
        # after that comes from the spawn system.
        self._fish: list[_Aquarium._Fish] = []
        for _ in range(3):
            self._fish.append(self._make_fish(seed_rng, on_screen=True))

        self._bubbles: list[_Aquarium._Bubble] = []
        # Countdown until each vent next spawns a bubble.
        self._vent_cooldowns = [self._rng.randint(4, 14)
                                for _ in self._BUBBLE_VENTS]
        # Countdown until next fish spawn attempt.
        self._next_spawn = self._rng.randint(self._SPAWN_MIN, self._SPAWN_MAX)
        # Crab: none on screen at start; wait a randomized cooldown before
        # the first crab cameo so restarts don't all spawn one at t=0.
        self._crab: _Aquarium._Crab | None = None
        self._crab_cooldown = self._rng.randint(
            self._CRAB_COOLDOWN_MIN, self._CRAB_COOLDOWN_MAX,
        )

    def _make_fish(self, rng: random.Random,
                   on_screen: bool = False) -> "_Aquarium._Fish":
        """Build a fish -- randomly picked species, y row, direction.

        If ``on_screen`` is True, the fish starts inside the visible
        panel (used only for the initial pre-populate). Otherwise it
        starts just off the panel on the side opposite its swim
        direction, so it slides into frame naturally.

        The shark is a special guest: ~5% of picks, and gets a slower,
        steadier speed than the reef fish so it reads as a big animal
        cruising through, not zipping past.
        """
        # Weighted species pick: shark = ~5%, all others uniform.
        if rng.random() < 0.05:
            idx = _SHARK_IDX
        else:
            # Uniform over non-shark species.
            idx = rng.randrange(len(_FISH_SPRITES) - 1)
        sprite = _FISH_SPRITES[idx][0]
        rows = sprite.split("\n")
        h = len(rows)
        w = max(len(r) for r in rows)
        y_min = 4
        y_max = max(y_min, self._SAND_Y - h - 1)
        direction = rng.choice((-1, 1))
        if on_screen:
            x = float(rng.randint(8, max(9, 128 - w - 8)))
        else:
            # Spawn just off-screen on the trailing side.
            x = float(-w - 1) if direction > 0 else float(128 + 1)
        # Sharks cruise slower than reef fish; big animals read as big
        # when they move deliberately.
        if idx == _SHARK_IDX:
            speed = rng.uniform(0.12, 0.2)
        else:
            speed = rng.uniform(0.15, 0.4)
        return _Aquarium._Fish(
            sprite_idx=idx,
            x=x,
            y=float(rng.randint(y_min, y_max)),
            y_phase=rng.random(),
            speed=speed,
            direction=direction,
        )

    # ------------------------------------------------------------------
    # Physics step
    # ------------------------------------------------------------------

    def _step(self) -> None:
        rng = self._rng

        # Bubble spawn from each vent on its own cadence.
        for i, vent_x in enumerate(self._BUBBLE_VENTS):
            self._vent_cooldowns[i] -= 1
            if self._vent_cooldowns[i] <= 0 and len(self._bubbles) < self._MAX_BUBBLES:
                # Small jitter around the vent x so the stream isn't a
                # single column of pixels.
                self._bubbles.append(_Aquarium._Bubble(
                    x=vent_x + rng.uniform(-1.5, 1.5),
                    y=float(self._SAND_Y - 1),
                    speed=rng.uniform(0.35, 0.7),
                    wobble=rng.uniform(0.0, 6.283),
                    big=rng.random() < 0.25,
                ))
                self._vent_cooldowns[i] = rng.randint(4, 18)

        # Move bubbles up with small horizontal drift.
        for b in self._bubbles:
            b.y -= b.speed
            b.wobble += 0.25
            # A gentle sideways drift is realistic for bubbles rising
            # through moving water -- unlike drops on glass, bubbles do
            # sway. Amplitude stays well under 1 px per frame.
            b.x += 0.15 * (1.0 if (b.wobble % 6.283) < 3.1415 else -1.0) \
                   + rng.uniform(-0.05, 0.05)
        self._bubbles = [b for b in self._bubbles if b.y > 1 and 0 <= b.x < 128]

        # Move fish: horizontal drift plus a slow triangular y-bob.
        # Fish that swim off-screen are culled rather than reflected --
        # a real aquarium has fish coming and going, not four residents
        # bouncing back and forth like arcade pong.
        surviving: list[_Aquarium._Fish] = []
        for f in self._fish:
            f.x += f.speed * f.direction
            f.y_phase = (f.y_phase + 0.008) % 1.0
            sprite = _FISH_SPRITES[f.sprite_idx][0]
            sprite_w = max(len(row) for row in sprite.split("\n"))
            # Off-screen check: only cull once the whole sprite has
            # cleared the edge, so the exit reads.
            if f.direction > 0 and f.x > 128 + 1:
                continue
            if f.direction < 0 and f.x + sprite_w < -1:
                continue
            surviving.append(f)
        self._fish = surviving

        # Spawn pacing. Always spawn if below the minimum; otherwise
        # spawn on the countdown up to _MAX_FISH.
        if len(self._fish) < self._MIN_FISH:
            self._fish.append(self._make_fish(rng, on_screen=False))
            self._next_spawn = rng.randint(self._SPAWN_MIN, self._SPAWN_MAX)
        else:
            self._next_spawn -= 1
            if self._next_spawn <= 0 and len(self._fish) < self._MAX_FISH:
                self._fish.append(self._make_fish(rng, on_screen=False))
                self._next_spawn = rng.randint(self._SPAWN_MIN, self._SPAWN_MAX)

        # Crab step. Only one crab at a time. When none is on screen,
        # tick down the cooldown; when it hits zero, walk a new crab
        # in from an off-screen side. When the crab exits the far
        # side, retire it and reset the cooldown.
        if self._crab is None:
            self._crab_cooldown -= 1
            if self._crab_cooldown <= 0:
                direction = rng.choice((-1, 1))
                sprite_w = max(len(row) for row in _CRAB_SPRITE.split("\n"))
                x = float(-sprite_w - 1) if direction > 0 else float(128 + 1)
                self._crab = _Aquarium._Crab(x=x, direction=direction)
        else:
            c = self._crab
            c.x += self._CRAB_SPEED * c.direction
            c.leg_phase += 1
            sprite_w = max(len(row) for row in _CRAB_SPRITE.split("\n"))
            if c.direction > 0 and c.x > 128 + 1:
                self._crab = None
                self._crab_cooldown = rng.randint(
                    self._CRAB_COOLDOWN_MIN, self._CRAB_COOLDOWN_MAX,
                )
            elif c.direction < 0 and c.x + sprite_w < -1:
                self._crab = None
                self._crab_cooldown = rng.randint(
                    self._CRAB_COOLDOWN_MIN, self._CRAB_COOLDOWN_MAX,
                )

    # ------------------------------------------------------------------
    # Draw
    # ------------------------------------------------------------------

    def _paint_background(self, canvas: Canvas) -> None:
        for y in range(32):
            t = y / 31.0
            r = int(_AQ_BG_TOP[0] + (_AQ_BG_BOTTOM[0] - _AQ_BG_TOP[0]) * t)
            g = int(_AQ_BG_TOP[1] + (_AQ_BG_BOTTOM[1] - _AQ_BG_TOP[1]) * t)
            b = int(_AQ_BG_TOP[2] + (_AQ_BG_BOTTOM[2] - _AQ_BG_TOP[2]) * t)
            canvas.fill_rect(0, y, 128, 1, (r, g, b))

    def _paint_caustics(self, canvas: Canvas, tick: int) -> None:
        # Broken-line caustics across the top 3 rows. Each row uses a
        # different phase so the ripples look independent, and we skip
        # pixels on a slow-moving offset to draw dashes rather than a
        # solid streak.
        for row in range(3):
            phase = (tick // 2 + row * 5) % 32
            for x in range(0, 128, 4):
                lit_x = (x + phase) % 128
                # Dash pattern -- 2 lit, 2 skipped, staggered per row.
                if (lit_x + row * 3) % 8 < 2:
                    canvas.pixel(lit_x, row, _AQ_CAUSTIC)

    def _paint_sand_and_weed(self, canvas: Canvas, tick: int) -> None:
        # Sand: two rows of muted olive-tan.
        canvas.fill_rect(0, self._SAND_Y, 128, 2, _AQ_SAND)
        # Seaweed: each frond is a vertical strand of 6-10 pixels that
        # sways in x by 1 based on a slow phase; alternating shades
        # give the fronds a little dimension.
        for i, base_x in enumerate(self._WEED_XS):
            height = 6 + (i % 3) * 2   # varies 6, 8, 10
            sway_phase = (tick // 6 + i * 3) % 8
            for k in range(height):
                # Sway increases with height (top wobbles more than base).
                sway = 0
                if k >= height // 2:
                    sway = 1 if sway_phase < 4 else -1
                if k >= height - 2:
                    sway *= 2
                px = (base_x + sway) % 128
                py = self._SAND_Y - 1 - k
                color = _AQ_WEED_LIT if k % 2 == 0 else _AQ_WEED_DARK
                canvas.pixel(px, py, color)

    def _paint_bubbles(self, canvas: Canvas) -> None:
        for b in self._bubbles:
            ix, iy = int(b.x), int(b.y)
            if not (0 <= ix < 128 and 0 <= iy < 32):
                continue
            canvas.pixel(ix, iy, _AQ_BUBBLE_LIGHT)
            if b.big:
                # 2x2 bubble with a dark rim so it reads as a sphere.
                if ix + 1 < 128:
                    canvas.pixel(ix + 1, iy, _AQ_BUBBLE_DARK)
                if iy + 1 < 32:
                    canvas.pixel(ix, iy + 1, _AQ_BUBBLE_DARK)
                if ix + 1 < 128 and iy + 1 < 32:
                    canvas.pixel(ix + 1, iy + 1, _AQ_BUBBLE_DARK)

    def _paint_fish(self, canvas: Canvas) -> None:
        for f in self._fish:
            sprite, body, dark, fin, eye = _FISH_SPRITES[f.sprite_idx]
            rows = sprite.split("\n")
            # y-bob: a tiny triangular offset from y_phase (0..1).
            phase = f.y_phase
            if phase < 0.5:
                bob = int(phase * 4) - 1  # -1, 0, 0, 1
            else:
                bob = 1 - int((phase - 0.5) * 4)  # 1, 0, 0, -1
            base_x, base_y = int(f.x), int(f.y) + bob
            for dy, row in enumerate(rows):
                for dx, ch in enumerate(row):
                    if ch == " ":
                        continue
                    # Mirror when swimming left.
                    if f.direction < 0:
                        px = base_x + (len(row) - 1 - dx)
                    else:
                        px = base_x + dx
                    py = base_y + dy
                    if 0 <= px < 128 and 0 <= py < self._SAND_Y:
                        if ch == "B":
                            color = body
                        elif ch == "D":
                            color = dark
                        elif ch == "E":
                            color = eye
                        else:  # "F" or any other non-space marker
                            color = fin
                        canvas.pixel(px, py, color)

    def _paint_crab(self, canvas: Canvas) -> None:
        c = self._crab
        if c is None:
            return
        rows = _CRAB_SPRITE.split("\n")
        sprite_h = len(rows)
        # Sprite sits ON TOP of the sand: bottom row of the crab lies on
        # the top row of sand (_SAND_Y), so ``base_y`` is sprite_h - 1
        # rows above the sand top.
        base_x = int(c.x)
        base_y = self._SAND_Y - (sprite_h - 1)
        # Tiny leg animation: the bottom row of the sprite has 4 leg
        # pixels on alternating columns. Every ~8 ticks flip which
        # legs are up (drawn) vs down (skipped) so the crab reads as
        # walking rather than sliding.
        leg_phase_flip = (c.leg_phase // 8) % 2 == 1
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch == " ":
                    continue
                # Mirror on direction so the crab's leading claw
                # depends on which way it's walking.
                if c.direction < 0:
                    px = base_x + (len(row) - 1 - dx)
                else:
                    px = base_x + dx
                py = base_y + dy
                if not (0 <= px < 128 and 0 <= py < 32):
                    continue
                # Leg-row shuffle: on the bottom row, drop one of the
                # two alternating leg groups each phase so the legs
                # visibly move as the crab walks. Legs are at dx =
                # 1, 3, 5, 7 -- group them by (dx // 2) parity so
                # every other leg swaps each phase.
                if dy == sprite_h - 1 and ch == "B":
                    if leg_phase_flip:
                        if (dx // 2) % 2 == 0:
                            continue
                    else:
                        if (dx // 2) % 2 == 1:
                            continue
                if ch == "B":
                    color = _CRAB_BODY
                elif ch == "F":
                    color = _CRAB_CLAW
                elif ch == "E":
                    color = _CRAB_EYE
                else:
                    color = _CRAB_DARK
                canvas.pixel(px, py, color)

    def render(self, canvas: Canvas, tick: int) -> None:
        self._step()
        self._paint_background(canvas)
        self._paint_caustics(canvas, tick)
        self._paint_sand_and_weed(canvas, tick)
        # Bubbles behind fish so a fish crossing a bubble stream reads
        # as the fish being closer to the glass.
        self._paint_bubbles(canvas)
        # Crab walks on the sand -- painted before fish so a swimming
        # fish drifting overhead reads as being in front of the crab.
        self._paint_crab(canvas)
        self._paint_fish(canvas)


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
