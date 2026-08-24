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

**Campfire.** The flame is an actual fluid simulation (``_FluidFlame``):
buoyancy, curl-noise turbulence, vorticity confinement, Jacobi pressure
projection and semi-Lagrangian advection on a 2x supersampled grid. It
replaced the Doom PSX / "Fabien Sanglard fire" cellular automaton, which
is a 1993 trick for machines with no CPU budget: that automaton re-rolls
every cell independently, so consecutive frames are uncorrelated and the
flame sizzles, and every fix for the sizzle is a display filter fighting
the generator. A solver is temporally coherent for free, because each
frame's heat field is the previous frame's field carried along the flow.
A Pi 5 has the headroom: measure it with ``scripts/bench_campfire.py``.
The automaton is kept intact as the fallback for a checkout without
numpy. A hand-drawn log pile sits below the flames; embers on the top of
the logs pulse with a slow noise so the fire looks like it's consuming
something rather than floating above dead wood.

**Rain.** Drops-on-a-window night scene: a shallow midnight-blue vertical
gradient stands in for the sky through the pane, and up to ~20 drops slide
down with fading trails, size-scaled speed, occasional surface-tension
pauses, and merge-on-contact (a bigger drop swallows a smaller one and
speeds up, exactly like real drops racing down a window). Every 20-40 s
a single-frame lightning strike screen-blends bright silver across the
whole panel and decays over ~4 frames, no branching bolts (they read as
noise on 32 rows).

**Driving.** First-person view through a windshield at dusk, drawn from
one pinhole camera that every element shares: ``screen_y = horizon +
K/Z`` for depth, ``F*X/Z`` for width, ``F*h/Z`` for prop height. Sharing
a single projection is the whole trick -- an earlier version used three
disagreeing perspective rules and the road, the poles and the dashes all
converged on different vanishing points. A blue-to-orange sky gradient
carries a setting sun with a quadratic glow, occluded by two parallax
layers of mesas with scree ramps at their bases. Below the horizon a
single ground-to-asphalt gradient runs per row, edged by cream shoulder
lines and centre dashes; both are stretched between this row's x and the
next row's so a 1px stripe cannot read as a dotted diagonal on a road
edge that moves ~4px per row. Dash alpha comes from 4 depth samples
across each row's depth interval, which is what stops far dashes
strobing. Poles (with crossarms and sagging catenary wires) and cacti
pass on both shoulders, faded toward the sky colour by an aerial-
perspective haze and clipped at Z=18 so they do not collapse into a
smear at the vanishing point. Wire pixels pick their colour from the
background luma they land on -- dark over bright sky, rim-lit over a dark
ridge -- because a single fixed wire colour sat within a couple of units
of the near mesa and half the run vanished into the hill it crossed.
Traffic is a handful of dicts carrying a Z and a lane, placed by the same
projection: oncoming vehicles close at our speed plus their own and exit
past the camera, while same-direction traffic closes only on the speed
difference, is followed for a few seconds at a fixed gap (overtaking
needs the oncoming lane, which is more scene than 32 rows can carry), and
then pulls away. Cars and trucks get a body with an inset cab and a
sky-glow rim once they are more than a couple of rows tall; below that
the lamps alone are the honest read, blooming radially into whatever is
behind them. Spans are anti-aliased horizontally only -- vertical
supersampling costs more than a pure-Python rasterizer can afford at
30fps. No fixed loop: speed, curve, and prop jitter come from an
unseeded RNG. See ``scripts/bench_driving.py`` for the per-frame cost.

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
import math
import random
from typing import ClassVar, Protocol

try:  # numpy drives the campfire's fluid solver; see ``_FluidFlame``.
    import numpy as _np
except ImportError:  # pragma: no cover - exercised by the fallback test
    _np = None  # type: ignore[assignment]

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
# Log pile sits low in the frame so the fire dominates. Log geometry is
# tuned in ``_draw_logs`` (thinner logs, outer ends dropped one row), so
# the visible flame area grows from 27 to 29 rows. ``_LOG_TOP`` here is
# where the FUEL row lives -- above that height heat can propagate. We
# push it down two rows to give the plasma more vertical runway. Reads
# as if the camera pulled up: wood is a rim along the bottom edge, and
# a tall column of flame climbs out of it.
_LOG_ROWS = 3
_LOG_TOP = 32 - _LOG_ROWS   # y = 29
# Flame buffer covers rows 0..28 (29 rows visible + 1 hidden fuel row).
# The fuel row is fed in just above the log crest so heat drives up the
# whole panel, letting the tips lick the top edge.
_FIRE_TOP = 0
_FIRE_HEIGHT = _LOG_TOP - _FIRE_TOP  # rows of *visible* flame (0..28)

# Log palette. Kept dim so the logs read as silhouettes with warmth on top
# rather than competing with the flames for attention. The "ember" colour
# is what the automaton pulses along the log crest to sell the "burning"
# read; without it the logs look like a dead prop.
_LOG_SHADOW = (28, 14, 6)   # deep shadow under the log / underside
_LOG_DARK   = (55, 30, 15)  # log body
_LOG_MID    = (95, 55, 25)  # highlight along the top of each log
_LOG_LIGHT  = (150, 92, 44) # fire-lit crest rim -- catches the flame
# End-grain palette: the sawn face of the log, concentric rings.
_END_OUTER  = (72, 40, 18)  # outer bark ring on the end face
_END_MID    = (128, 78, 38) # sap-wood ring
_END_INNER  = (185, 118, 60) # bright inner ring (heartwood, fire-lit)
_EMBER = (215, 87, 15)     # pulsing coal on the crest
_EMBER_HOT = (231, 143, 31)  # brief brighter flash


class _FluidFlame:
    """The campfire flame as an actual fluid simulation.

    The Doom automaton this replaced is a 1993 trick for machines with no
    CPU budget: every cell independently re-rolls its decay and a
    horizontal jitter, which carves tongue shapes but has no memory, so
    consecutive frames are uncorrelated and the result sizzles. Every fix
    for the sizzle (cross-fading, asymmetric attack/release, snap
    thresholds) is a display-side filter fighting the generator.

    A real solver does not need any of that: temporal coherence is
    intrinsic, because each frame's temperature field is the previous
    frame's field carried along the velocity field. Per frame:

    1. Inject fuel (heat) in the bottom rows over the log pile, with an
       upward impulse.
    2. Buoyancy: hot gas accelerates upward, proportional to temperature.
    3. Curl-noise turbulence: add the curl of a smooth sine potential.
       Divergence-free by construction, so it stirs without inventing
       mass. This is what makes tongues -- buoyancy on its own gives a
       featureless plume, and white noise gives vertical streaks.
    4. Vorticity confinement: re-inject the small curls that numerical
       diffusion eats, or the flame goes soft after a few seconds.
    5. Pressure projection (Jacobi): make the flow near-incompressible,
       which is why hot gas rolls and curls instead of just sliding up.
    6. Semi-Lagrangian advection of velocity, then temperature.
    7. Cool multiplicatively.

    Two details that took the longest to get right:

    * Turbulence is masked by both temperature and a height ramp. Real
      flame is laminar where it leaves the fuel and breaks up as it rises
      and entrains air; unmasked turbulence shimmers the entire panel,
      including the still air outside the plume.
    * The fuel bed is a low plateau across the log pile plus four uneven
      Gaussian hot spots. Hot spots alone read as a row of separate
      candles; the plateau alone reads as a gas burner. Together the
      tongues share one base and separate as they rise, which is what a
      log pile actually does.

    Runs on a supersampled grid and area-averages down to the panel, so
    each LED is the mean of ``_SS**2`` cells and the flame edge lands on
    sub-LED positions instead of snapping between columns.
    """

    # Supersample factor. 3 is visually indistinguishable from 2 at this
    # panel size and costs a bit over twice as much, so 2 it is.
    _SS = 2

    # All coefficients are per-frame at 30fps; there is no separate dt.
    _JACOBI = 10          # pressure iterations
    _BUOYANCY = 0.16      # upward accel per unit temperature
    _COOLING = 0.905      # multiplicative heat loss per frame
    _VORTICITY = 0.55     # vorticity-confinement strength
    _DRAG = 0.978         # velocity damping
    _VMAX = 3.0           # px/frame clamp, keeps advection stable
    _GUST = 0.010         # ambient horizontal breeze amplitude

    _TURB = 14.0          # curl-noise forcing strength
    _TURB_RAMP = 0.35     # fraction of height over which turbulence ramps in
    _TURB_MODES = 6
    _KX_LO, _KX_HI = 5.0, 14.0   # cycles across the grid width
    _KY_LO, _KY_HI = 1.5, 5.0    # cycles across the grid height

    _FUEL_ROWS = 3
    _FUEL_KICK = 0.40     # upward impulse at the fuel rows
    _FUEL_HALF = 0.045    # hot-spot width, as a fraction of panel width
    _BED_LEVEL = 0.18     # low plateau that merges the tongues at the base
    _BED_HALF = 0.24      # plateau half-width, fraction of panel width
    _BED_EDGE = 0.045     # plateau edge softness, fraction of panel width
    # (centre as a fraction of width, weight, width scale)
    _SPOTS = (
        (0.28, 0.80, 0.75),
        (0.40, 0.55, 0.60),
        (0.61, 0.78, 0.70),
        (0.76, 0.60, 0.65),
    )
    # Each hot spot's strength wanders on its own slow cycle, so the
    # dominant tongue moves along the log line instead of two bright
    # pillars standing in fixed columns forever. Physically this is a log
    # burning through: the flame front migrates. Periods are in frames and
    # deliberately non-harmonic so the pattern never visibly repeats.
    _SPOT_WANDER = 0.55       # fraction of each spot's weight it swings by
    _SPOT_PERIODS = (211.0, 137.0, 179.0, 97.0, 251.0)

    # Temperature window mapped across the palette. Averaging _SS**2 cells
    # per LED plus a 32-step palette turns a flame edge into a soft ramp;
    # real flame has a thin bright reaction zone, so rescale the window we
    # care about across the whole palette instead of showing the cold tail.
    _FRONT_LO = 0.05
    _FRONT_HI = 0.82

    def __init__(self, width: int = 128, height: int = _FIRE_HEIGHT) -> None:
        np = _np
        if np is None:  # pragma: no cover - guarded by the caller
            raise RuntimeError("_FluidFlame requires numpy")
        self.width = width
        self.height = height
        ss = self._SS
        self._w = w = width * ss
        self._h = h = height * ss

        # Deterministic so preview captures and tests reproduce.
        rng = np.random.default_rng(0xC0FFEE)
        self._rng = rng

        self._t = np.zeros((h, w), dtype=np.float32)   # temperature
        self._u = np.zeros((h, w), dtype=np.float32)   # velocity, +x
        self._v = np.zeros((h, w), dtype=np.float32)   # velocity, +y is DOWN
        self._p = np.zeros((h, w), dtype=np.float32)   # pressure scratch
        self._tick = 0
        self._ys, self._xs = np.meshgrid(
            np.arange(h, dtype=np.float32), np.arange(w, dtype=np.float32), indexing="ij"
        )
        # Fuel is rebuilt every frame from per-spot shapes and wandering
        # weights; see ``_fuel_profile``.
        self._bed, self._shapes, self._weights = self._fuel_parts()
        self._spot_phases = rng.uniform(0, 2 * np.pi, size=len(self._weights)).astype(
            np.float32
        )
        self._fuel = self._fuel_profile()

        # Smooth flicker: a sum of a few sines, so fuel intensity wanders
        # over about a second instead of white-noising every frame.
        self._phases = rng.uniform(0, 2 * np.pi, size=6).astype(np.float32)
        self._freqs = np.array(
            [0.013, 0.021, 0.034, 0.055, 0.089, 0.144], dtype=np.float32
        )

        n = self._TURB_MODES
        self._kx = rng.uniform(self._KX_LO, self._KX_HI, size=n).astype(np.float32) * (
            2 * np.pi / w
        )
        self._ky = rng.uniform(self._KY_LO, self._KY_HI, size=n).astype(np.float32) * (
            2 * np.pi / h
        )
        self._px = rng.uniform(0, 2 * np.pi, size=n).astype(np.float32)
        self._py = rng.uniform(0, 2 * np.pi, size=n).astype(np.float32)
        self._wx = rng.uniform(-0.05, 0.05, size=n).astype(np.float32)
        self._wy = rng.uniform(-0.04, 0.04, size=n).astype(np.float32)
        self._amp = (rng.uniform(0.6, 1.0, size=n) / np.arange(1, n + 1)).astype(
            np.float32
        )

        rows = np.arange(h, dtype=np.float32)
        above = (h - 1 - rows) / max(h - 1, 1)   # 0 at the fuel, 1 at the top
        self._height_ramp = np.clip(above / self._TURB_RAMP, 0.0, 1.0)[:, None].astype(
            np.float32
        )
        self._xline = np.arange(w, dtype=np.float32)
        self._yline = np.arange(h, dtype=np.float32)

        # Palette as an array, so mapping heat -> RGB is one fancy index.
        self._lut = np.array(_CAMPFIRE_PALETTE, dtype=np.uint8)
        # Heat 0 and 1 were never painted by the automaton (1 is a grey
        # smoke value that reads as a dead pixel at panel scale), so keep
        # them black here too.
        self._lut[1] = (0, 0, 0)

    # -- setup ------------------------------------------------------------
    def _fuel_parts(self):
        """Split the fuel bed into a static plateau and per-spot shapes.

        Returns ``(bed, shapes, weights)`` where ``bed`` is the low
        plateau over the log pile, ``shapes`` is one unit-height Gaussian
        per hot spot, and ``weights`` their nominal strengths. Keeping
        them separate means the per-frame update is one small matrix
        product rather than recomputing five exponentials.
        """
        np = _np
        w = self._w
        x = np.arange(w, dtype=np.float32)
        centre = w * 0.5
        half = w * self._FUEL_HALF

        spots = ((0.5, 1.0, 1.0),) + tuple(self._SPOTS)
        shapes = np.stack(
            [
                np.exp(-((x - w * frac) / (half * wscale)) ** 2).astype(np.float32)
                for frac, _weight, wscale in spots
            ]
        )
        weights = np.array([weight for _frac, weight, _ws in spots], dtype=np.float32)

        bed_half = w * self._BED_HALF
        edge = max(w * self._BED_EDGE, 1.0)
        d = np.abs(x - centre) - bed_half
        bed = np.clip(1.0 - d / edge, 0.0, 1.0)
        bed = bed * bed * (3.0 - 2.0 * bed)      # smoothstep
        bed = (self._BED_LEVEL * bed).astype(np.float32)
        return bed, shapes, weights

    def _fuel_profile(self):
        """The fuel bed for this frame, with hot spots wandering slowly."""
        np = _np
        periods = self._SPOT_PERIODS
        phase = np.array(
            [
                self._spot_phases[i] + 2 * np.pi * self._tick / periods[i % len(periods)]
                for i in range(len(self._weights))
            ],
            dtype=np.float32,
        )
        swing = np.float32(1.0) + np.float32(self._SPOT_WANDER) * np.sin(phase)
        hump = self._bed + (self._weights * swing) @ self._shapes
        return np.clip(hump, 0.0, 1.4).astype(np.float32)

    # -- solver pieces ----------------------------------------------------
    def _flicker(self, k: float) -> float:
        np = _np
        return float(np.sin(self._freqs * self._tick * 2 * np.pi + self._phases + k).mean())

    def _turbulence(self) -> None:
        np = _np
        t = float(self._tick)
        du = np.zeros((self._h, self._w), dtype=np.float32)
        dv = np.zeros((self._h, self._w), dtype=np.float32)
        xl, yl = self._xline, self._yline
        for i in range(len(self._kx)):
            sx = np.sin(self._kx[i] * xl + self._px[i] + self._wx[i] * t)
            cx = np.cos(self._kx[i] * xl + self._px[i] + self._wx[i] * t)
            sy = np.sin(self._ky[i] * yl + self._py[i] + self._wy[i] * t)
            cy = np.cos(self._ky[i] * yl + self._py[i] + self._wy[i] * t)
            a = self._amp[i] * np.float32(self._TURB)
            # psi = a * sx(x) * sy(y);  u += dpsi/dy, v -= dpsi/dx
            du += np.float32(a * self._ky[i]) * np.outer(cy, sx).astype(
                np.float32, copy=False
            )
            dv -= np.float32(a * self._kx[i]) * np.outer(sy, cx).astype(
                np.float32, copy=False
            )
        # Stir only where there is hot gas, and more the higher it has
        # risen. Still air outside the plume must stay still.
        mask = np.minimum(self._t * np.float32(2.0), np.float32(1.0)) * self._height_ramp
        self._u += du * mask
        self._v += dv * mask

    def _advect(self, field):
        """Semi-Lagrangian backtrace with bilinear sampling."""
        np = _np
        x = self._xs - self._u
        y = self._ys - self._v
        np.clip(x, 0, self._w - 1.001, out=x)
        np.clip(y, 0, self._h - 1.001, out=y)
        x0 = x.astype(np.int32)
        y0 = y.astype(np.int32)
        x1 = x0 + 1
        y1 = y0 + 1
        fx = x - x0
        fy = y - y0
        f00 = field[y0, x0]
        f10 = field[y0, x1]
        f01 = field[y1, x0]
        f11 = field[y1, x1]
        top = f00 + (f10 - f00) * fx
        bot = f01 + (f11 - f01) * fx
        return (top + (bot - top) * fy).astype(np.float32, copy=False)

    def _project(self) -> None:
        """Make the velocity field (nearly) divergence-free."""
        np = _np
        div = np.zeros_like(self._u)
        div[1:-1, 1:-1] = 0.5 * (
            self._u[1:-1, 2:]
            - self._u[1:-1, :-2]
            + self._v[2:, 1:-1]
            - self._v[:-2, 1:-1]
        )
        p = self._p
        p *= 0.0
        for _ in range(self._JACOBI):
            p[1:-1, 1:-1] = np.float32(0.25) * (
                p[1:-1, 2:] + p[1:-1, :-2] + p[2:, 1:-1] + p[:-2, 1:-1] - div[1:-1, 1:-1]
            )
        self._u[1:-1, 1:-1] -= 0.5 * (p[1:-1, 2:] - p[1:-1, :-2])
        self._v[1:-1, 1:-1] -= 0.5 * (p[2:, 1:-1] - p[:-2, 1:-1])

    def _vorticity_confinement(self) -> None:
        """Re-inject the small curls numerical diffusion smears out."""
        np = _np
        w = np.zeros_like(self._u)
        w[1:-1, 1:-1] = 0.5 * (
            self._v[1:-1, 2:]
            - self._v[1:-1, :-2]
            - (self._u[2:, 1:-1] - self._u[:-2, 1:-1])
        )
        aw = np.abs(w)
        gx = np.zeros_like(aw)
        gy = np.zeros_like(aw)
        gx[1:-1, 1:-1] = 0.5 * (aw[1:-1, 2:] - aw[1:-1, :-2])
        gy[1:-1, 1:-1] = 0.5 * (aw[2:, 1:-1] - aw[:-2, 1:-1])
        mag = np.sqrt(gx * gx + gy * gy) + np.float32(1e-5)
        gx /= mag
        gy /= mag
        # force = eps * (N x w); in 2D the cross with the scalar curl.
        self._u += np.float32(self._VORTICITY) * gy * w
        self._v -= np.float32(self._VORTICITY) * gx * w

    # -- one frame --------------------------------------------------------
    def step(self) -> None:
        np = _np
        self._tick += 1

        # Fuel injection at the log line, intensity wandering smoothly.
        base = np.float32(1.0 + 0.35 * self._flicker(0.0))
        self._fuel = self._fuel_profile()
        rows = slice(self._h - self._FUEL_ROWS, self._h)
        jitter = (1.0 + 0.25 * self._rng.standard_normal(self._w)).astype(np.float32)
        self._t[rows] = np.maximum(
            self._t[rows], (self._fuel * base * jitter).astype(np.float32)
        )
        self._v[rows] -= np.float32(self._FUEL_KICK) * self._fuel
        self._u += np.float32(self._GUST * self._flicker(1.7))

        # Buoyancy: hot gas rises, and +y is down, so subtract.
        self._v -= np.float32(self._BUOYANCY) * self._t
        self._v *= np.float32(self._DRAG)
        self._u *= np.float32(self._DRAG)
        np.clip(self._u, -self._VMAX, self._VMAX, out=self._u)
        np.clip(self._v, -self._VMAX, self._VMAX, out=self._v)

        self._turbulence()
        self._vorticity_confinement()
        self._project()

        u_new = self._advect(self._u)
        v_new = self._advect(self._v)
        self._u, self._v = u_new, v_new
        self._t = self._advect(self._t)

        self._t *= np.float32(self._COOLING)
        np.clip(self._t, 0.0, 1.6, out=self._t)

    def heat(self):
        """Area-average to the panel band, as 0..1 floats."""
        np = _np
        ss = self._SS
        t = self._t.reshape(self.height, ss, self.width, ss).mean(axis=(1, 3))
        t = (t - np.float32(self._FRONT_LO)) / np.float32(self._FRONT_HI - self._FRONT_LO)
        return np.clip(t, 0.0, 1.0) ** np.float32(1.1)

    def rgb(self):
        """The band as an ``(height, width, 3)`` uint8 array."""
        np = _np
        max_heat = len(self._lut) - 1
        idx = np.clip((self.heat() * max_heat + 0.5).astype(np.int32), 0, max_heat)
        return self._lut[idx]


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
    # 29 visible flame rows -- plenty of vertical runway for the tips
    # to actually wave and curl rather than reading as a bright edge.
    _BUFFER_HEIGHT = _FIRE_HEIGHT + 1  # includes fuel row

    def __init__(self) -> None:
        # Deterministic seed on init so preview captures + tests are
        # reproducible. Real-world uses reseed via ``random.random()``
        # on every propagation step, so the fire still flickers.
        self._rng = random.Random(0xC0FFEE)
        # 2D list of ints, [y][x], top row first, height includes fuel.
        # We keep TWO buffers: the current plasma state and the previous
        # one. In-between frames blend between them so the perceived
        # animation is smooth even though the plasma only steps at
        # 15 Hz. Without this, dropping the step rate for a slower
        # flicker made the fire look choppy -- each simulation step
        # was a full palette-scale jump.
        self._buffer: list[list[int]] = [
            [0] * 128 for _ in range(self._BUFFER_HEIGHT)
        ]
        self._prev_buffer: list[list[int]] = [
            [0] * 128 for _ in range(self._BUFFER_HEIGHT)
        ]
        # Fuel row is the last row -- kept hot every frame.
        for x in range(128):
            self._buffer[-1][x] = 31
            self._prev_buffer[-1][x] = 31
        # Displayed heat, as floats. This is what actually gets painted:
        # a smoothed follower of ``_buffer`` (see ``_ATTACK``/``_RELEASE``).
        # Kept separate from the simulation so the filter never feeds back
        # into the plasma -- an earlier fire died exactly that way.
        self._shown: list[list[float]] = [
            [0.0] * 128 for _ in range(self._BUFFER_HEIGHT)
        ]
        # Ember phase advances slowly so the log crest pulses out of
        # sync with the flame body, which is what real coals look like.
        self._ember_phase = 0
        # The flame itself is a fluid solver when numpy is available (see
        # ``_FluidFlame``). The automaton above stays as the fallback so a
        # numpy-less venv still renders the panel rather than crashing the
        # whole vibes mode -- numpy is in requirements.txt, but the mode
        # should not be the thing that discovers a broken install.
        self._fluid: _FluidFlame | None = None
        if _np is not None:
            try:
                self._fluid = _FluidFlame()
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("campfire: fluid flame unavailable, using the automaton")
                self._fluid = None
        else:
            LOGGER.warning("campfire: numpy missing, falling back to the plasma automaton")

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

        Before advancing, snapshot the current buffer to ``_prev_buffer``
        so ``render`` can interpolate between them on in-between frames.
        """
        # Snapshot for interpolation.
        for y in range(self._BUFFER_HEIGHT):
            self._prev_buffer[y][:] = self._buffer[y]
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
        # Panel is 128x32. Logs sit in the bottom 7 rows. Two logs
        # cross like a teepee: LEFT log runs low-left to high-right,
        # RIGHT log runs high-left to low-right. They overlap near the
        # centre; the LEFT log is painted second so it appears to sit
        # ON TOP of the right one, which sells the cross more than any
        # amount of shading (a stack has a definite over/under; two
        # bands that just intersect read as a plus sign).
        #
        # Each log carries three shading rows across its 7 px thickness:
        #   row 0 (crest): fire-lit rim -- LIGHT
        #   row 1        : normal highlight -- MID
        #   rows 2-5     : body -- DARK
        #   row 6 (base) : ground shadow -- SHADOW
        # Plus an end-cap disc at the OUTER end of each log showing the
        # sawn face's concentric rings. The inner end is buried by the
        # other log so no cap is needed there.
        # Outer end starts 6 px inside the panel edge so the end-cap disc
        # can render fully. Logs are longer than the buffer would suggest
        # -- they cross well past the centre so the overlap region is
        # substantial (no black gap between them). The RIGHT-facing log
        # (drawn second) sits on top of the LEFT-facing log at the cross.
        # Vertical kindling piece: a shorter, slimmer log standing upright
        # behind the crossed pair. Drawn FIRST so the horizontal logs
        # cover its base -- reads as "stood into the fire behind the
        # stack." Leans a hair to the right so it doesn't look robotic.
        # Its top is inside the flame area; the crest gets the fire-lit
        # LIGHT colour and the sides fade to DARK, giving it volume.
        # Kindling base sits inside the log cross (log crest is now y=27
        # after shrinking the pile), top pokes higher into the taller flame
        # column so the flames lick past it.
        vert_x_bot, vert_y_bot = 64, 29  # base sits inside the log cross
        vert_x_top, vert_y_top = 66, 10  # top pokes into the flames
        vert_thick = 4
        vdx = vert_x_top - vert_x_bot
        vdy = vert_y_top - vert_y_bot
        vsteps = max(abs(vdx), abs(vdy))
        for i in range(vsteps + 1):
            t = i / vsteps
            cy = int(round(vert_y_bot + vdy * t))
            cx = int(round(vert_x_bot + vdx * t))
            # Paint a horizontal stripe of vert_thick px across (cx, cy).
            # Left edge is the shaded side, right edge catches highlight.
            left = cx - vert_thick // 2
            for k in range(vert_thick):
                px = left + k
                if not (0 <= px < 128 and 0 <= cy < 32):
                    continue
                if k == 0:
                    colour = _LOG_SHADOW
                elif k == vert_thick - 1:
                    colour = _LOG_MID
                else:
                    colour = _LOG_DARK
                canvas.pixel(px, cy, colour)
        # Top-of-kindling cap: a tiny 3-px-wide TOP RING so the upright
        # piece also shows an end grain like the horizontal logs. Uses
        # the same inner/mid/outer palette but at a smaller scale.
        top_ring = [
            (vert_x_top - 1, vert_y_top,     _END_OUTER),
            (vert_x_top + 1, vert_y_top,     _END_OUTER),
            (vert_x_top,     vert_y_top - 1, _END_OUTER),
            (vert_x_top,     vert_y_top,     _END_INNER),
        ]
        for px, py, colour in top_ring:
            if 0 <= px < 128 and 0 <= py < 32:
                canvas.pixel(px, py, colour)

        log_specs = (
            # (x_outer, y_outer, x_inner, y_inner, thickness)
            # y_outer=30 sits the outer ends one row lower than before so
            # the log pile reads as a rim along the bottom edge ("camera
            # up"). Thickness stays 5 -- thick enough to keep the four
            # shading rows below the crest, thin enough that the crest
            # is at y=28 (outer) / y=22 (inner) and the flames get all
            # of rows 0..27 to work with. 3-px-radius end cap still fits
            # inside the panel: cap centre y=30, cap spans y=27..32 clipped
            # to y=27..31, which sits just below the crest without eating
            # into the flame column.
            (7,   30, 78, 24, 5),   # left log:  outer left,  crosses right of centre
            (120, 30, 50, 24, 5),   # right log: outer right, crosses left  of centre (on top)
        )

        for x_o, y_o, x_i, y_i, thickness in log_specs:
            dx = x_i - x_o
            dy = y_i - y_o
            steps = max(abs(dx), abs(dy))
            for i in range(steps + 1):
                t = i / steps
                cx = int(round(x_o + dx * t))
                cy = int(round(y_o + dy * t))
                # Paint the vertical stripe of ``thickness`` px centred
                # around (cx, cy). k=0 is the crest (top of the stripe).
                top = cy - thickness // 2
                for k in range(thickness):
                    py = top + k
                    if not (0 <= py < 32 and 0 <= cx < 128):
                        continue
                    if k == 0:
                        colour = _LOG_LIGHT
                    elif k == 1:
                        colour = _LOG_MID
                    elif k == thickness - 1:
                        colour = _LOG_SHADOW
                    else:
                        colour = _LOG_DARK
                    canvas.pixel(cx, py, colour)

            # End cap: 3-px-radius disc centred on the OUTER end,
            # showing the sawn face with concentric rings. Drawn last for
            # this log so it sits on top of the body pixels.
            cap_r = 3
            cap_cx = x_o
            cap_cy = y_o - thickness // 2 + thickness // 2  # centre of the stripe at outer end
            # ...which is just y_o for a stripe centred on y_o. Use y_o directly.
            cap_cy = y_o
            for oy in range(-cap_r, cap_r + 1):
                for ox in range(-cap_r, cap_r + 1):
                    d2 = ox * ox + oy * oy
                    if d2 > cap_r * cap_r:
                        continue
                    px = cap_cx + ox
                    py = cap_cy + oy
                    if not (0 <= px < 128 and 0 <= py < 32):
                        continue
                    if d2 <= 1:
                        colour = _END_INNER
                    elif d2 <= 4:
                        colour = _END_MID
                    else:
                        colour = _END_OUTER
                    canvas.pixel(px, py, colour)



        # Ember dots along each log's crest. Positions are deterministic
        # (seeded RNG separate from the plasma) so a given ember stays
        # in one spot and pulses in place, like a real coal.
        ember_rng = random.Random(0xE1BE12)
        embers_per_log = 6
        for x_o, y_o, x_i, y_i, thickness in log_specs:
            dx = x_i - x_o
            dy = y_i - y_o
            steps = max(abs(dx), abs(dy))
            crest_dy = -(thickness // 2)
            # Choose a handful of ``t`` values along the log's length,
            # skipping the very ends so the embers don't fall off the
            # rounded log tips.
            for _ in range(embers_per_log):
                t = ember_rng.uniform(0.2, 0.8)
                ex = int(round(x_o + dx * t))
                ey = int(round(y_o + dy * t)) + crest_dy
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

    # Plasma step rate. At 30fps, stepping every frame reads as a strobe
    # even with interpolation -- each simulation step is a big palette
    # jump (Doom's decay is aggressive by design), so what the eye sees
    # is a rapid twitch on every step frame. Real relaxing fire wobbles
    # closer to 7-8 Hz: slow enough that the flame tongues visibly rise
    # and curl instead of blurring together.
    #
    # We step every 4 ticks (~7.5 Hz on the 30fps loop) and interpolate
    # the THREE intermediate frames between the previous buffer and the
    # current one on the heat scalar, so the perceived animation is
    # smooth 30fps motion with a slow plasma underneath. Blend fractions
    # are 0, 0.25, 0.5, 0.75 -- the eye sees a continuous rise instead
    # of a hard jump every step frame.
    _STEP_EVERY = 4

    # Temporal smoothing (attack / release) on the DISPLAYED heat field.
    #
    # The automaton is what makes this read as fire -- each cell re-rolls
    # its own decay and x-jitter every step, which is exactly what carves
    # the tongues. It is also why the raw output sizzles: a pixel can go
    # bright -> dark -> bright in three steps, and 3700 pixels doing that
    # independently reads as static.
    #
    # A symmetric low-pass fixes the sizzle and destroys the shape,
    # because a tongue's leading edge is a step function and averaging it
    # rounds it off. So the filter is ASYMMETRIC, the way a real flame
    # behaves: brightening is nearly instant (combustion), dimming is
    # slow (the gas is still glowing as it cools). Per frame, for every
    # pixel:
    #
    #     shown += (target - shown) * (_ATTACK if rising else _RELEASE)
    #
    # _ATTACK near 1.0 keeps new tongues crisp; a low _RELEASE turns the
    # flicker-out into a fade, so the eye reads motion instead of noise.
    # This replaces the old prev/current cross-fade -- the filter already
    # supplies the in-between frames, and running both just double-blurs.
    _ATTACK = 0.85
    _RELEASE = 0.30

    # ...with one exception. A slow release applied all the way down to
    # zero leaves a dark-red haze hanging above the flame for a third of
    # a second after the tongue is gone -- read as smeary ghosting, and
    # the thing that makes the fire look like it is fading rather than
    # burning. The fix is not a faster release everywhere (that just
    # brings the sizzle back), it is a faster release only in the bottom
    # of the palette, where the smoothing was buying nothing: those
    # pixels are dim enough that their flicker was never the problem.
    # Below ``_SNAP_BELOW`` heat, fall at ``_SNAP_RELEASE`` instead, so
    # the tail extinguishes in ~2 frames while the bright body of the
    # flame keeps the slow, calm falloff.
    _SNAP_BELOW = 7.0
    _SNAP_RELEASE = 0.65

    def render(self, canvas: Canvas, tick: int) -> None:
        if self._fluid is not None:
            self._render_fluid(canvas, tick)
            return
        self._render_automaton(canvas, tick)

    def _render_fluid(self, canvas: Canvas, tick: int) -> None:
        """Advance the fluid solver one frame and blit it.

        No step-rate division and no attack/release filter here: the
        solver is already temporally coherent, so it runs at the full frame
        rate and needs no smoothing on top. Blitting the band in one paste
        instead of ~3.7k ``canvas.pixel`` calls is most of why this fits
        the frame budget at all.
        """
        self._fluid.step()
        canvas.clear()
        canvas.blit_rgb(0, _FIRE_TOP, self._fluid.rgb())
        self._draw_logs(canvas)
        if tick % 4 == 0:
            self._ember_phase = (self._ember_phase + 1) % 32

    def _render_automaton(self, canvas: Canvas, tick: int) -> None:
        # Step the plasma on a slower cadence than the frame rate, then
        # let the display filter carry the in-between frames.
        if tick % self._STEP_EVERY == 0:
            self._step()
        canvas.clear()
        buf = self._buffer
        shown = self._shown
        attack = self._ATTACK
        release = self._RELEASE
        snap_below = self._SNAP_BELOW
        snap_release = self._SNAP_RELEASE
        palette = _CAMPFIRE_PALETTE
        max_heat = len(palette) - 1
        for row_index in range(_FIRE_HEIGHT):
            y = _FIRE_TOP + row_index
            row_target = buf[row_index]
            row_shown = shown[row_index]
            for x in range(128):
                target = row_target[x]
                value = row_shown[x]
                # Rising and falling get different time constants; see the
                # class comment above.
                if target > value:
                    coeff = attack
                elif value < snap_below:
                    # Embers of the tail: let them go quickly. See
                    # ``_SNAP_BELOW``.
                    coeff = snap_release
                else:
                    coeff = release
                value += (target - value) * coeff
                row_shown[x] = value
                heat = int(value + 0.5)
                if heat > max_heat:
                    heat = max_heat
                if heat > 1:
                    canvas.pixel(x, y, palette[heat])
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
# Driving palette + geometry
# ---------------------------------------------------------------------------
#
# Every object in this scene -- road edges, lane lines, dashes, poles,
# wires, cacti, ground flecks, oncoming headlights -- is placed in world
# coordinates and projected through ONE pinhole camera. That is the whole
# point of the rewrite: the previous version had a linearly-interpolated
# road trapezoid, an ad-hoc ``z ** 1.6`` curve for the dashes and a
# third rule for the poles, so nothing agreed about where "far away"
# was and the scene never converged to a vanishing point.
#
# The camera sits at the world origin looking down +Z. For a point at
# world ``(X, Y, Z)`` with Z > 0:
#
#     screen_y = _DR_HORIZON_Y + _DR_K / Z          (Y = ground plane)
#     screen_x = vanishing_x   + _DR_F * X / Z
#     screen_h = _DR_F * world_height / Z           (apparent height)
#
# ``_DR_K`` is the camera height times the focal length, and is fixed by
# the requirement that the ground at Z = 1 lands on the last road row.
# ``_DR_F`` is the focal length in pixels at Z = 1, fixed by the
# requirement that the road half-width (3.5 world units, i.e. a ~7-unit
# two-lane road) fills 59 px at Z = 1. Inverting the first equation
# gives the depth of a scanline, ``Z = _DR_K / (y - horizon)``, which is
# how the road band is rasterized: walk output rows, ask each row how
# deep it is, and lay down the spans that belong at that depth.

# Sky gradient -- deep blue at the top fading to warm orange at the
# horizon. Dusk was chosen over daylight so the sky/road boundary reads
# even on a low-contrast LED panel: the warm horizon line pops against
# the dark asphalt.
_DR_SKY_TOP    = (18, 22, 55)     # deep blue-purple
_DR_SKY_HORIZ  = (215, 110, 55)   # warm orange at the horizon

# The setting sun. A dusk palette with no sun in it reads as "someone
# picked orange", so put the source of the light in the frame. It sits
# a hair below the horizon row so the mesas can bite into it.
_DR_SUN        = (255, 230, 150)  # hot core
_DR_SUN_GLOW   = (255, 150, 70)   # the halo it bleeds into the sky

# Desert mesas in two depth layers. Atmospheric perspective, not just
# parallax: the far layer is LIGHTER (more air between it and you, so
# more scattered sky light) and scrolls slower. A single silhouette
# colour, which is what the old version used, flattens the horizon into
# a sticker.
_DR_MESA_FAR   = (86, 56, 74)     # hazy, blends toward the sky
_DR_MESA_NEAR  = (46, 24, 38)     # deep purple-brown, nearly a silhouette
_DR_TALUS_FAR  = (94, 64, 78)     # scree slope at the base, a touch lighter
_DR_TALUS_NEAR = (56, 32, 44)

# Road palette. Real asphalt at dusk photographs almost black with a
# warm tint picked up from the sky, and it gets LIGHTER with distance
# as haze and grazing-angle sky reflection take over -- which is also
# the only reason a 16-row road reads as a receding surface rather than
# a black wedge. ``_DR_ROAD`` is the near end of that ramp and is the
# colour the tests key on.
_DR_ROAD       = (18, 18, 28)     # asphalt directly under the bumper
_DR_ROAD_FAR   = (56, 48, 56)     # asphalt at the horizon, hazed out
_DR_SHOULDER   = (95, 65, 45)     # dusty sand-orange gravel edge
_DR_GROUND     = (62, 38, 30)     # desert sand at the camera
_DR_GROUND_FAR = (112, 76, 60)    # desert sand at the horizon, hazed
_DR_FLECK      = (140, 100, 76)   # scrub/gravel specks out on the sand
_DR_ROAD_FLECK = (72, 68, 78)     # tar/chip texture on the asphalt
_DR_LANE_LINE  = (250, 235, 180)  # bright cream -- the centre dashes
_DR_LANE_EDGE  = (230, 210, 150)  # the solid white edge lines

# Roadside poles: dark silhouettes against the sky, with a crossarm and
# catenary wires strung between consecutive tops. The wires are what
# make a vertical stick read as infrastructure.
_DR_POLE       = (22, 14, 26)
_DR_WIRE       = (34, 24, 38)     # seen against a bright sky
_DR_WIRE_LIT   = (124, 100, 116)  # seen against a dark mesa, rim-lit

# Saguaro cacti along the shoulders. These MUST be lighter than the
# ground: the old (30, 55, 30) was darker than the sand, so every
# cactus read as a hole punched in the desert instead of a plant
# standing on it.
_DR_CACTUS_NEAR = (74, 98, 58)    # sage green, lighter than the sand
_DR_CACTUS_FAR  = (86, 76, 72)    # distant saguaros, hazed toward the mesas

# Dashboard strip: solid dark at the very bottom so the road doesn't
# hit the panel edge. Reads as "looking over a dashboard".
_DR_DASH       = (12, 10, 18)
_DR_DASH_TRIM  = (60, 40, 30)     # a hair of warmth along the top edge

# Oncoming headlights on the opposite lane -- a rare, bright cameo.
_DR_HEADLIGHT      = (255, 245, 200)  # hot cream-white core
_DR_HEADLIGHT_HALO = (200, 180, 130)  # halo around the core

# Traffic. Bodies are only a shade above the asphalt on purpose: at dusk
# a car ahead of you is a silhouette carrying two red lights, not a
# painted shape. The lights do the identifying, the body just blocks the
# lane markings behind it.
_DR_CAR_BODY   = (26, 22, 34)
_DR_CAR_ROOF   = (33, 28, 41)
_DR_CAR_GLASS  = (58, 54, 78)
_DR_CAR_EDGE   = (98, 86, 106)     # sky-glow rim along the roof and shoulders
_DR_TRUCK_BODY = (44, 34, 42)
_DR_TRUCK_TRIM = (214, 152, 68)    # trailer marker lamps along the top
_DR_TAIL       = (232, 48, 32)
_DR_TAIL_HALO  = (128, 26, 20)

# Scene geometry. Panel is 128x32.
_DR_HORIZON_Y   = 13          # last sky row; the horizon line itself
_DR_ROAD_TOP_Y  = 14          # first road row
_DR_BOTTOM_Y    = 29          # last road row (Z = 1 lands here)
_DR_DASH_Y      = 30          # first row of the dashboard strip
_DR_VP_X        = 64.0        # vanishing-point x on a straight stretch

# Camera constants (see the projection note above).
_DR_K = float(_DR_BOTTOM_Y - _DR_HORIZON_Y)   # 16.0 -> Z=1 at row 29
_DR_ROAD_HALF   = 3.5         # world half-width of the asphalt
_DR_F = 59.0 / _DR_ROAD_HALF  # ~16.857 px per world unit at Z = 1

# World widths of the painted markings.
_DR_SHOULDER_HALF = 4.4       # outer edge of the gravel shoulder
_DR_EDGE_W        = 0.18      # solid white edge line
_DR_DASH_W        = 0.16      # centre dash
_DR_DASH_LEN      = 0.45      # dash length along Z
_DR_DASH_PERIOD   = 1.20      # dash + gap along Z

# Depth window we bother rasterizing props into. Beyond ~34 world units
# a pole is under a pixel tall and only contributes shimmer.
_DR_Z_NEAR = 1.0
_DR_Z_FAR  = 34.0
# Poles and cacti stop well short of that. Past ~18 world units
# consecutive poles land on the same column and the parade collapses
# into a black smear straddling the vanishing point, which reads as a
# smudge on the panel rather than as distance.
_DR_PROP_Z_FAR = 18.0
# Wires anchor to poles beyond the range the poles themselves are drawn
# over, at both ends. Without the near overhang the wire pops out of
# existence when the closest pole passes the camera; without the far
# overhang the run stops short of the vanishing point.
_DR_WIRE_Z_NEAR = 0.40
_DR_WIRE_Z_FAR = 26.0
# Beyond ~9 units props are hazed toward the sky behind them. Without
# this an 18-unit-away pole is exactly as black as one at the bumper,
# and the whole receding line reads flat.
_DR_HAZE_Z = 9.0


def _dr_lerp(c0, c1, t: float):
    """Linear blend of two RGB triples at ``t`` in [0, 1]."""
    return (
        int(c0[0] + (c1[0] - c0[0]) * t + 0.5),
        int(c0[1] + (c1[1] - c0[1]) * t + 0.5),
        int(c0[2] + (c1[2] - c0[2]) * t + 0.5),
    )


def _dr_span(row: bytearray, x0: float, x1: float, colour, alpha: float = 1.0) -> None:
    """Paint the half-open horizontal span ``[x0, x1)`` into an RGB row.

    This is the antialiasing story for the whole scene, and it is
    deliberately horizontal-only. Vertical supersampling was the obvious
    fix for the staircased road edges and it is the wrong one here: it
    triples the cost of a pure-Python rasterizer to fight aliasing on
    edges that are nearly vertical, where the visible error is
    horizontal. Blending only the two boundary pixels of each span by
    exact fractional coverage removes the 4-5 px jogs for the price of
    two multiply-adds a span, and the interior is a slice assignment
    rather than a per-pixel loop.

    Sub-pixel spans (a far edge line 0.2 px wide) fall through to the
    per-pixel path and come out as a dim tint, which is what you want:
    they fade with distance instead of popping in and out as the
    rounding flips.
    """
    if x1 <= x0:
        return
    if x0 < 0.0:
        x0 = 0.0
    if x1 > 128.0:
        x1 = 128.0
    if x1 <= x0:
        return
    ia = int(math.ceil(x0))
    ib = int(math.floor(x1))
    if alpha >= 0.999 and ib > ia:
        row[3 * ia:3 * ib] = bytes(colour) * (ib - ia)
        left = ia - x0
        if left > 0.001 and ia >= 1:
            _dr_blend(row, ia - 1, colour, left)
        right = x1 - ib
        if right > 0.001 and ib < 128:
            _dr_blend(row, ib, colour, right)
        return
    # Narrow or translucent span: cover every touched pixel by hand.
    for i in range(int(math.floor(x0)), int(math.ceil(x1))):
        cov = min(x1, i + 1.0) - max(x0, float(i))
        if cov > 0.0:
            _dr_blend(row, i, colour, cov * alpha)


def _dr_blend(row: bytearray, i: int, colour, a: float) -> None:
    """Alpha-blend ``colour`` over pixel ``i`` of an RGB row."""
    if a <= 0.0:
        return
    o = 3 * i
    if a >= 1.0:
        row[o] = colour[0]
        row[o + 1] = colour[1]
        row[o + 2] = colour[2]
        return
    inv = 1.0 - a
    row[o] = int(row[o] * inv + colour[0] * a + 0.5)
    row[o + 1] = int(row[o + 1] * inv + colour[1] * a + 0.5)
    row[o + 2] = int(row[o + 2] * inv + colour[2] * a + 0.5)


class _Driving:
    """First-person view driving a desert highway at dusk.

    One ``_distance`` scalar (world units along +Z) advances every tick
    and everything else is derived from it through the pinhole camera
    described above. Nothing in the scene has its own private notion of
    perspective, so the road, the dashes, the poles, the wires between
    them and the cacti all agree about where the vanishing point is.

    What sells the motion is not the dashes -- it is the scrolling
    ground speckle. A flat sand slab with dashes sliding down the middle
    reads as a moving stripe on a still background; a field of gravel
    flecks streaming outward from the vanishing point reads as ground
    passing under you. That is the single biggest change here.

    The road bends. ``X_center(Z) = 0.5 * c * Z**2`` is a
    constant-curvature arc, which projects to a screen offset of
    ``_DR_F * c * Z / 2`` -- linear in Z, so the bend opens up toward
    the horizon exactly like a real road seen from inside it. ``c``
    itself is a slow sinusoid, so the highway sweeps left and right over
    about half a minute instead of running dead straight forever.

    Cost: the sky band and the road band are each built as one
    ``bytearray`` and pushed with a single :meth:`Canvas.blit_rgb`, so
    the 2176 background pixels cost 2 blits instead of 2176 Python
    calls. Only the props that cross the horizon go through
    ``canvas.pixel``. Measure with ``scripts/bench_driving.py``.
    """

    # Speed in world units per tick, with a slow breath so the drive
    # doesn't feel like cruise control. 0.10 units/tick at 30 fps means
    # a dash period (1.2 units) passes in ~0.4 s.
    _SPEED_BASE   = 0.10
    _SPEED_AMP    = 0.035
    _SPEED_PERIOD = 900      # ticks per breath cycle (~30 s at 30 fps)

    # Road curvature: amplitude of ``c`` and its period in ticks.
    _CURVE_AMP    = 0.115    # ~22 px of bend at the far edge of the window
    _CURVE_PERIOD = 1100

    # Roadside furniture, in world units.
    _POLE_X       = 5.4
    _POLE_SPACING = 3.5
    _POLE_HEIGHT  = 2.6
    _POLE_ARM     = 0.32     # half-width of the crossarm
    _CACTUS_X     = 7.0
    _CACTUS_SPACING = 3.5
    _CACTUS_OFFSET  = 1.75   # half a spacing, so a cactus never shares a Z
    _CACTUS_HEIGHT  = 1.8

    # Surface speckle: a fixed set of flecks recycled through a Z window.
    # Half of them land on the asphalt and half out on the sand, because
    # near the camera the road fills the panel edge to edge -- ground
    # flecks alone would leave the bottom third of the frame motionless.
    _FLECKS_ROAD   = 130
    _FLECKS_GROUND = 130
    _FLECK_PERIOD  = 45.0    # world units before a fleck wraps around

    # Mesa layers scroll at a fraction of the ground speed.
    _MESA_FAR_PARALLAX  = 0.35
    _MESA_NEAR_PARALLAX = 1.0

    # Traffic. Two lanes: ours at +1.75 world units off the centreline,
    # oncoming at -1.75. Both are inside _DR_ROAD_HALF (3.5) so a vehicle
    # sits on tarmac, not on the shoulder.
    _LANE_OURS      = 1.75
    _LANE_ONCOMING  = -1.75
    _CAR_W, _CAR_H     = 1.55, 1.15
    _TRUCK_W, _TRUCK_H = 2.05, 2.10
    # Lamp track width. These have to be comfortably narrower than
    # _CAR_W: at 1.40 against a 1.55-wide car the headlights landed on
    # the body's own outline and punched two holes in it.
    _HEADLIGHT_SEP  = 1.05
    _TAIL_SEP       = 1.00
    # Retire a vehicle while it still looks like one. Z = 1 lands on the
    # bottom road row, so anything closer projects its base *below* the
    # panel: the body becomes a slab with no bottom and the lamps fall
    # off the screen entirely, which is exactly how a passing car turned
    # into a grey rectangle with no headlights. At 1.30 the lamps are
    # still on row ~23 and the body is already two thirds off the side
    # of the frame, so the exit reads as a car whipping past.
    _EXIT_Z         = 1.30

    # Oncoming: spawns at the far clip and closes at our speed plus its
    # own, so it crosses the whole window in about 6 s. Frequent enough
    # to feel like a road, rare enough that the pass still registers.
    _ONCOMING_MIN_TICKS = 5 * 30
    _ONCOMING_MAX_TICKS = 17 * 30
    _ONCOMING_MAX       = 2
    _ONCOMING_SPEED_LO  = 0.80       # their speed as a multiple of ours
    _ONCOMING_SPEED_HI  = 1.25

    # Same-direction traffic ahead. It spawns mid-window because closing
    # on it from Z=34 at a realistic speed difference would take a full
    # minute of nothing happening.
    _AHEAD_MIN_TICKS = 9 * 30
    _AHEAD_MAX_TICKS = 26 * 30
    # Spawn inside Z=16. Past that the whole depth range compresses onto
    # the horizon row -- Z=1 lands on the bottom road row and K is 16, so
    # everything from 16 out to the far clip shares one or two rows. A
    # car spawned at 24 therefore sat as a stationary red dot on the
    # skyline for ten seconds before it grew at all.
    _AHEAD_SPAWN_Z   = (9.0, 15.0)
    _AHEAD_CLOSE_LO  = 0.025         # world units per tick, closing
    _AHEAD_CLOSE_HI  = 0.060
    _AHEAD_PULL_LO   = 0.012         # ...and pulling away again
    _AHEAD_PULL_HI   = 0.030
    _FOLLOW_Z        = 2.30          # where we stop closing and just follow
    _FOLLOW_HOLD     = (2 * 30, 7 * 30)
    _TRUCK_CHANCE    = 0.30

    def __init__(self) -> None:
        # Deterministic layout RNG so the roadside is stable across
        # frames, plus an unseeded one for traffic cadence so no two
        # sessions have the same traffic.
        self._layout_rng = random.Random(0xD1E5E11)
        self._rng = random.Random()

        self._distance = 0.0
        self._curve = 0.0
        self._mesa_far = self._build_mesa(2, 5, 3, 9, 10, 26)
        self._mesa_near = self._build_mesa(3, 8, 2, 6, 8, 22)
        self._mesa_far_offset = 0.0
        self._mesa_near_offset = 0.0
        self._flecks = self._build_flecks()
        # Per-slot jitter for poles and cacti, materialized on demand.
        self._pole_jitter: dict[int, tuple[float, float]] = {}
        self._cactus_slots: dict[int, tuple[float, float, int, int]] = {}
        # Live vehicles, each a small dict. A list beats a class here:
        # there are never more than three, and the render loop only ever
        # reads fields.
        self._traffic: list[dict[str, float | int | str]] = []
        # First spawns are drawn from a short window rather than the full
        # steady-state one. Drawing the first gap from the full range put
        # an empty road on screen for up to 17 seconds after the mode
        # started, which reads as "the traffic is broken", not as "the
        # highway is quiet". After the first vehicle the normal spacing
        # takes over.
        self._oncoming_cooldown = self._rng.randint(
            45, self._ONCOMING_MIN_TICKS,
        )
        self._ahead_cooldown = self._rng.randint(
            self._AHEAD_MIN_TICKS // 3, self._AHEAD_MIN_TICKS,
        )
        # Row -> depth table. Fixed geometry, so compute it once.
        self._row_z = {
            y: _DR_K / (y - _DR_HORIZON_Y)
            for y in range(_DR_ROAD_TOP_Y, _DR_BOTTOM_Y + 1)
        }

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_mesa(self, h_lo: int, h_hi: int, talus_lo: int, talus_hi: int,
                    gap_lo: int, gap_hi: int) -> list[int]:
        """A 256-column mesa profile: rows of rise above the horizon.

        Mesas are flat-topped with a scree ramp on both sides. The ramp
        is the fix for the old version's "floating brown planks": a bare
        2-4 px bar with vertical ends has no base, so the eye reads it
        as a plank hanging in the sky rather than rock sitting on the
        desert floor.
        """
        rng = self._layout_rng
        out = [0] * 256
        x = 0
        while x < 256:
            x += rng.randint(gap_lo, gap_hi)
            if x >= 256:
                break
            plateau = rng.randint(h_lo, h_hi)
            top_w = rng.randint(10, 26)
            left_talus = rng.randint(talus_lo, talus_hi)
            right_talus = rng.randint(talus_lo, talus_hi)
            # Left scree ramp.
            for k in range(left_talus):
                col = x + k
                if col < 256:
                    out[col] = max(out[col], 1 + int(
                        (plateau - 1) * (k + 1) / (left_talus + 1)))
            x += left_talus
            # Plateau.
            for k in range(top_w):
                col = x + k
                if col < 256:
                    out[col] = max(out[col], plateau)
            x += top_w
            # Right scree ramp.
            for k in range(right_talus):
                col = x + k
                if col < 256:
                    out[col] = max(out[col], 1 + int(
                        (plateau - 1) * (right_talus - k) / (right_talus + 1)))
            x += right_talus
        return out

    def _build_flecks(self) -> list[tuple[float, float, bool]]:
        """``(X, Z0, on_road)`` for the scrolling surface speckle.

        Flecks are recycled through a fixed Z window, so the field is
        finite but never visibly repeats within a glance. The ``on_road``
        flag only picks the colour and opacity -- the projection is the
        same one everything else uses.
        """
        rng = self._layout_rng
        out: list[tuple[float, float, bool]] = []
        for _ in range(self._FLECKS_ROAD):
            side = rng.choice((-1.0, 1.0))
            x = side * rng.uniform(0.35, _DR_ROAD_HALF - 0.25)
            out.append((x, rng.uniform(0.0, self._FLECK_PERIOD), True))
        for _ in range(self._FLECKS_GROUND):
            side = rng.choice((-1.0, 1.0))
            x = side * rng.uniform(_DR_SHOULDER_HALF + 0.6, 20.0)
            out.append((x, rng.uniform(0.0, self._FLECK_PERIOD), False))
        return out

    def _pole_slot(self, slot: int) -> tuple[float, float]:
        """``(x_jitter, height_scale)`` for pole ``slot``, cached."""
        cached = self._pole_jitter.get(slot)
        if cached is None:
            r = random.Random(0xB00 ^ slot)
            cached = (r.uniform(-0.35, 0.35), r.uniform(0.85, 1.15))
            self._pole_jitter[slot] = cached
        return cached

    def _cactus_slot(self, slot: int) -> tuple[float, float, int, int]:
        """``(x_jitter, height_scale, arm_side, arm_drop)`` for a cactus.

        ``arm_side`` is -1/+1 for one arm, 0 for none, 2 for both.
        ``arm_drop`` is how far below the crown the arm attaches, in
        fractions of the plant's height.
        """
        cached = self._cactus_slots.get(slot)
        if cached is None:
            r = random.Random(0xCAC ^ slot)
            roll = r.random()
            if roll < 0.12:
                arm = 0
            elif roll < 0.68:
                arm = r.choice((-1, 1))
            else:
                arm = 2
            cached = (r.uniform(-0.5, 0.5), r.uniform(0.85, 1.3), arm,
                      r.randint(30, 45))
            self._cactus_slots[slot] = cached
        return cached

    # ------------------------------------------------------------------
    # Motion
    # ------------------------------------------------------------------

    def _step(self, tick: int) -> None:
        speed = (
            self._SPEED_BASE
            + self._SPEED_AMP * math.sin(tick * (2 * math.pi / self._SPEED_PERIOD))
        )
        self._distance += speed
        self._mesa_far_offset += speed * self._MESA_FAR_PARALLAX
        self._mesa_near_offset += speed * self._MESA_NEAR_PARALLAX
        self._curve = self._CURVE_AMP * math.sin(
            tick * (2 * math.pi / self._CURVE_PERIOD))

        self._step_traffic(speed)

    # ------------------------------------------------------------------
    # Traffic
    # ------------------------------------------------------------------

    def _step_traffic(self, speed: float) -> None:
        """Advance every vehicle and spawn new ones on their cooldowns.

        Depth is in the same world units as everything else, so a vehicle
        needs no screen-space state at all -- it carries a Z and a lane,
        and the shared projection places it. That is the payoff from the
        camera rewrite: traffic is about forty lines because it does not
        have to invent its own perspective.

        The two directions behave differently on purpose. Oncoming
        traffic closes at our speed plus its own, so it crosses the whole
        window in a few seconds and exits behind the camera.
        Same-direction traffic closes only on the speed *difference*,
        which is slow, and cannot simply be passed -- overtaking on a
        two-lane road means crossing the centre line into the oncoming
        lane, which is far more scene than 32 rows can carry. So we close
        on it, match speed and follow for a few seconds, then it pulls
        away again. That is what actually happens on a highway, and it
        costs one state flag.
        """
        survivors = []
        for v in self._traffic:
            if v["dir"] == -1:
                v["z"] -= speed * (1.0 + v["rel"])
                if v["z"] > self._EXIT_Z:
                    survivors.append(v)
                continue
            # Same direction: signed drift, with a follow hold at the end
            # of the approach.
            if v["hold"] > 0:
                v["hold"] -= 1
                if v["hold"] == 0:
                    v["rel"] = self._rng.uniform(
                        self._AHEAD_PULL_LO, self._AHEAD_PULL_HI)
            else:
                v["z"] += v["rel"]
                if v["rel"] < 0.0 and v["z"] <= self._FOLLOW_Z:
                    v["z"] = self._FOLLOW_Z
                    v["hold"] = self._rng.randint(*self._FOLLOW_HOLD)
            if v["z"] < _DR_Z_FAR + 2.0:
                survivors.append(v)
        self._traffic = survivors

        oncoming = sum(1 for v in self._traffic if v["dir"] == -1)
        ahead = sum(1 for v in self._traffic if v["dir"] == 1)

        self._oncoming_cooldown -= 1
        if self._oncoming_cooldown <= 0:
            self._oncoming_cooldown = self._rng.randint(
                self._ONCOMING_MIN_TICKS, self._ONCOMING_MAX_TICKS)
            # Never stack two oncoming vehicles at nearly the same depth:
            # the far one hides behind the near one's body and is only
            # ever seen as it emerges, which reads as a rendering glitch
            # rather than as traffic.
            crowded = any(
                v["dir"] == -1 and v["z"] > _DR_Z_FAR - 8.0
                for v in self._traffic
            )
            if oncoming < self._ONCOMING_MAX and not crowded:
                self._traffic.append(self._spawn(-1))

        self._ahead_cooldown -= 1
        if self._ahead_cooldown <= 0:
            self._ahead_cooldown = self._rng.randint(
                self._AHEAD_MIN_TICKS, self._AHEAD_MAX_TICKS)
            # One vehicle ahead at a time. A second queued behind the
            # first in the same lane is invisible anyway.
            if ahead == 0:
                self._traffic.append(self._spawn(1))

    def _spawn(self, direction: int) -> dict:
        truck = self._rng.random() < self._TRUCK_CHANCE
        if direction == -1:
            z = _DR_Z_FAR
            rel = self._rng.uniform(
                self._ONCOMING_SPEED_LO, self._ONCOMING_SPEED_HI)
        else:
            z = self._rng.uniform(*self._AHEAD_SPAWN_Z)
            rel = -self._rng.uniform(self._AHEAD_CLOSE_LO, self._AHEAD_CLOSE_HI)
        return {
            "z": z,
            "dir": direction,
            "rel": rel,
            "hold": 0,
            "truck": 1 if truck else 0,
            "lane": self._LANE_ONCOMING if direction == -1 else self._LANE_OURS,
            "w": self._TRUCK_W if truck else self._CAR_W,
            "h": self._TRUCK_H if truck else self._CAR_H,
            # A little lateral offset so a vehicle is not welded to the
            # exact lane centre for its whole life.
            "drift": self._rng.uniform(-0.22, 0.22),
        }

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------

    def _centre_x(self, z: float) -> float:
        """Screen x of the road centreline at depth ``z``.

        ``X_center(Z) = c * Z**2 / 2`` projected through ``F * X / Z``
        collapses to a term linear in Z, which is why a constant-
        curvature road looks like it peels away near the horizon and is
        straight under the bumper.
        """
        return _DR_VP_X + _DR_F * self._curve * z * 0.5

    def _ground_y(self, z: float) -> float:
        return _DR_HORIZON_Y + _DR_K / z

    def _project(self, x: float, z: float) -> float:
        return self._centre_x(z) + _DR_F * x / z

    # ------------------------------------------------------------------
    # Sky
    # ------------------------------------------------------------------

    def _paint_sky(self, canvas: Canvas) -> None:
        rows = _DR_HORIZON_Y + 1
        sun_x = _DR_VP_X + 22.0
        sun_y = float(_DR_HORIZON_Y) - 1.0
        band: list[bytearray] = []
        for y in range(rows):
            t = y / max(1, _DR_HORIZON_Y)
            base = _dr_lerp(_DR_SKY_TOP, _DR_SKY_HORIZ, t)
            row = bytearray(bytes(base) * 128)
            # Sun glow: a soft radial falloff, then the hot core on top.
            dy = y - sun_y
            span = 14.0 * 14.0 - dy * dy
            if span > 0.0:
                reach = math.sqrt(span)
                x_lo = max(0, int(sun_x - reach))
                x_hi = min(127, int(sun_x + reach))
                for x in range(x_lo, x_hi + 1):
                    dx = x - sun_x
                    d = math.sqrt(dx * dx + dy * dy)
                    if d <= 3.2:
                        _dr_blend(row, x, _DR_SUN, 1.0)
                    else:
                        # Quadratic falloff reads as light bleeding into
                        # haze; a linear one reads as a painted disc.
                        k = max(0.0, 1.0 - (d - 3.2) / 10.8)
                        _dr_blend(row, x, _DR_SUN_GLOW, 0.62 * k * k)
            band.append(row)
        self._paint_mesa_layer(band, self._mesa_far, self._mesa_far_offset,
                               _DR_MESA_FAR, _DR_TALUS_FAR)
        self._paint_mesa_layer(band, self._mesa_near, self._mesa_near_offset,
                               _DR_MESA_NEAR, _DR_TALUS_NEAR)
        canvas.blit_rgb(0, 0, band)

    def _paint_mesa_layer(self, band: list[bytearray], profile: list[int],
                          offset: float, rock, talus) -> None:
        """Stamp one mesa layer into the sky band, occluding the sun."""
        shift = int(offset) % 256
        for x in range(128):
            h = profile[(x + shift) % 256]
            if h <= 0:
                continue
            for k in range(h):
                y = _DR_HORIZON_Y - k
                if y < 0:
                    break
                # Bottom row of the silhouette is the scree colour, so
                # the rock meets the desert instead of ending in a
                # hard line.
                _dr_blend(band[y], x, talus if k == 0 else rock, 1.0)

    # ------------------------------------------------------------------
    # Road
    # ------------------------------------------------------------------

    def _paint_road(self, canvas: Canvas) -> None:
        rows = _DR_BOTTOM_Y - _DR_ROAD_TOP_Y + 1
        band: list[bytearray] = []
        span_rows = float(_DR_BOTTOM_Y - _DR_ROAD_TOP_Y)
        for y in range(_DR_ROAD_TOP_Y, _DR_BOTTOM_Y + 1):
            z = self._row_z[y]
            t = (y - _DR_ROAD_TOP_Y) / span_rows
            # Haze ramps out with distance, so blend on the row index
            # rather than on Z (which is hyperbolic and would put the
            # entire gradient in the top two rows).
            ground = _dr_lerp(_DR_GROUND_FAR, _DR_GROUND, t)
            # Asphalt darkens faster than the sand hazes, so bias the
            # ramp; a straight lerp left the middle of the road a flat
            # mid-grey that read as a second ground plane.
            road = _dr_lerp(_DR_ROAD_FAR, _DR_ROAD, t ** 0.55)
            row = bytearray(bytes(ground) * 128)
            cx = self._centre_x(z)
            scale = _DR_F / z
            # The row BELOW this one, in world terms nearer. Markings
            # narrower than the per-row horizontal step have to be
            # stretched to meet their own continuation on the next
            # scanline, or they come out as a dotted diagonal instead of
            # a line -- the road edge moves ~4 px per row down here and
            # a 1 px stripe simply cannot reach across that.
            z_next = self._row_z.get(y + 1, z)
            cx_next = self._centre_x(z_next)
            scale_next = _DR_F / z_next
            _dr_span(row, cx - _DR_SHOULDER_HALF * scale,
                     cx + _DR_SHOULDER_HALF * scale, _DR_SHOULDER)
            _dr_span(row, cx - _DR_ROAD_HALF * scale,
                     cx + _DR_ROAD_HALF * scale, road)
            # Solid edge lines. On a 128 px panel these are the single
            # strongest road cue there is, and the old version simply
            # did not have them.
            half_edge = _DR_EDGE_W * 0.5 * scale
            offset = (_DR_ROAD_HALF - _DR_EDGE_W)
            for side in (-1.0, 1.0):
                ex = cx + side * offset * scale
                ex_next = cx_next + side * offset * scale_next
                lo = min(ex, ex_next) - half_edge
                hi = max(ex, ex_next) + half_edge
                _dr_span(row, lo, hi, _DR_LANE_EDGE)
            # Centre dashes. The dash ENDS are horizontal, so unlike
            # every other span in the scene they alias vertically:
            # sample the row's depth interval and use the in-dash
            # fraction as alpha, which turns the 1-row pop into a fade.
            cov = self._dash_coverage(y, z)
            if cov > 0.01:
                half_dash = _DR_DASH_W * 0.5 * scale
                lo = min(cx, cx_next) - half_dash
                hi = max(cx, cx_next) + half_dash
                _dr_span(row, lo, hi, _DR_LANE_LINE, cov)
            band.append(row)
        self._paint_flecks(band)
        canvas.blit_rgb(0, _DR_ROAD_TOP_Y, band)

    def _dash_coverage(self, y: int, z: float) -> float:
        """Fraction of output row ``y`` that a centre dash covers.

        A screen row spans a whole depth interval, and near the horizon
        that interval is several dash periods wide. Averaging the
        in-dash test over the interval is what stops the far dashes from
        strobing as the pattern slides through them; it also makes them
        settle into a dim continuous line at distance, which is exactly
        what a real dashed centre line does.
        """
        z_near = _DR_K / (y + 1 - _DR_HORIZON_Y)
        samples = 4
        hit = 0.0
        for i in range(samples):
            zz = z_near + (z - z_near) * (i + 0.5) / samples
            phase = (zz + self._distance) % _DR_DASH_PERIOD
            if phase < _DR_DASH_LEN:
                hit += 1.0
        return hit / samples

    def _paint_flecks(self, band: list[bytearray]) -> None:
        """Scrolling surface speckle -- the scene's real motion cue.

        Dashes sliding down the middle of a flat slab read as a moving
        stripe on a still background. A field of flecks streaming
        outward from the vanishing point reads as ground passing under
        you, and it is the one thing the old driving scene had no
        version of at all.
        """
        d = self._distance
        for x_world, z0, on_road in self._flecks:
            z = (z0 - d) % self._FLECK_PERIOD
            if z < _DR_Z_NEAR or z > _DR_Z_FAR:
                continue
            y = self._ground_y(z)
            iy = int(y)
            if iy < _DR_ROAD_TOP_Y or iy > _DR_BOTTOM_Y:
                continue
            px = self._project(x_world, z)
            if not (0.0 <= px < 128.0):
                continue
            # Fade with distance so the far field is texture, not noise.
            near = min(1.0, 4.0 / z)
            if on_road:
                colour, a = _DR_ROAD_FLECK, 0.16 + 0.30 * near
            else:
                colour, a = _DR_FLECK, 0.35 + 0.45 * near
            _dr_blend(band[iy - _DR_ROAD_TOP_Y], int(px), colour, a)

    # ------------------------------------------------------------------
    # Roadside props (these cross the horizon, so they go on the canvas)
    # ------------------------------------------------------------------

    def _paint_poles(self, canvas: Canvas) -> None:
        """Pole line down both shoulders, with wires strung between them.

        Endpoints are collected over a WIDER depth range than the poles
        are drawn over. That is the fix for wires that vanished: the old
        version skipped a pole entirely when it fell outside
        ``[_DR_Z_NEAR, _DR_PROP_Z_FAR]``, which also dropped its wire
        endpoint, so the moment the nearest pole slid past the camera the
        whole near span of wire popped out of existence -- and past the
        far clip there was no next pole to string to, so the run never
        reached the vanishing point either. Wires now anchor to poles the
        camera cannot see at both ends, which is what makes the line
        continuous.
        """
        spacing = self._POLE_SPACING
        base = int(self._distance // spacing)
        phase = self._distance % spacing
        # (x, arm_row_y, arm_half_width) per pole, near to far, per side.
        anchors: dict[int, list[tuple[float, float, float]]] = {-1: [], 1: []}
        count = int((_DR_WIRE_Z_FAR + spacing) / spacing) + 2
        for i in range(count):
            z = i * spacing - phase
            if z < _DR_WIRE_Z_NEAR or z > _DR_WIRE_Z_FAR:
                continue
            jitter, hscale = self._pole_slot(base + i)
            y_base = self._ground_y(z)
            height = _DR_F * self._POLE_HEIGHT * hscale / z
            y_top = y_base - height
            arm = self._POLE_ARM * _DR_F / z
            pole = self._hazed(_DR_POLE, z)
            visible = _DR_Z_NEAR <= z <= _DR_PROP_Z_FAR
            for side in (-1, 1):
                px = self._project(side * self._POLE_X + jitter, z)
                anchors[side].append((px, y_top + 1.0, arm))
                if not visible:
                    continue
                ix = int(round(px))
                if not (0 <= ix < 128):
                    continue
                iy_top = int(round(y_top))
                y0 = max(0, iy_top)
                y1 = min(31, int(round(y_base)))
                for y in range(y0, y1 + 1):
                    canvas.pixel(ix, y, pole)
                # Crossarm, scaled by the same perspective as the pole
                # itself -- a real crossarm is about an eighth of the
                # pole's height across, which is why the old fixed
                # 3-px bar read as a hammer head on the near poles.
                iarm = max(1, int(round(arm)))
                ay = iy_top + 1
                if height >= 4.0 and 0 <= ay < 32:
                    for dx in range(-iarm, iarm + 1):
                        cxp = ix + dx
                        if 0 <= cxp < 128:
                            canvas.pixel(cxp, ay, pole)
        for side in (-1, 1):
            self._paint_wires(canvas, anchors[side])

    def _paint_wires(self, canvas: Canvas, anchors) -> None:
        """Catenary wires between consecutive pole crossarms.

        Sampled along whichever axis is longer, not stepped in x. Near
        spans are steep -- the pole tops climb off the top of the panel
        while barely moving sideways -- so an x-stepped line drew a few
        scattered pixels and read as dashes, and far spans compress below
        one pixel of horizontal run, where the old ``abs(dx) < 1.0``
        guard skipped them outright and cut the line short of the
        vanishing point. Sampling on ``max(|dx|, |dy|)`` covers both.

        Two wires per side, anchored at the crossarm tips, collapsing
        onto each other with distance. Two pixels of sag is all 32 rows
        can show, but it is the difference between "power line" and
        "sticks in the sky".
        """
        img = canvas.image_buffer
        for (x0, ay0, arm0), (x1, ay1, arm1) in zip(anchors, anchors[1:]):
            if abs(x1 - x0) > 260.0:
                continue    # a pole wrapped; do not draw a wire across the panel
            for tip in (-1.0, 1.0):
                tx0 = x0 + tip * arm0
                tx1 = x1 + tip * arm1
                dx = tx1 - tx0
                dy = ay1 - ay0
                sag = min(2.0, abs(dx) / 14.0)
                steps = min(160, max(2, int(max(abs(dx), abs(dy))) + 1))
                for s in range(steps + 1):
                    u = s / steps
                    ix = int(round(tx0 + dx * u))
                    if not (0 <= ix < 128):
                        continue
                    iy = int(round(
                        ay0 + dy * u + 4.0 * sag * u * (1.0 - u)))
                    if 0 <= iy <= _DR_HORIZON_Y:
                        canvas.pixel(ix, iy, self._wire_colour(img, ix, iy))

    @staticmethod
    def _wire_colour(img, ix: int, iy: int):
        """Pick a wire colour that contrasts with whatever is behind it.

        This is the fix for wires that seemed to vanish over the ridges.
        They were being drawn the whole time -- 71 pixels a frame -- but
        ``_DR_WIRE`` (34, 24, 38) is within a couple of units of
        ``_DR_MESA_NEAR`` (46, 24, 38), and nearly half the wire run
        crosses the mesas. It was painting a dark line onto an equally
        dark hill.

        A single fixed colour cannot work here, because the same wire
        crosses bright sky, a dark ridge, and the sun within one span. So
        pick per pixel: dark where the background is bright, rim-lit
        where the background is dark. That is also what the eye actually
        sees -- a wire is thinner than a pixel, so what lands on the
        panel is a blend, and against a dark hill the blend is dominated
        by the sky glow scattering around the wire rather than by the
        wire itself.
        """
        r, g, b = img.getpixel((ix, iy))[:3]
        # Rec. 601 luma is close enough and avoids a multiply-heavy path
        # in a loop that runs a few hundred times a frame.
        return _DR_WIRE if (r * 77 + g * 151 + b * 28) >> 8 > 72 else _DR_WIRE_LIT

    def _hazed(self, colour, z: float):
        """Fade a prop colour toward the sky behind it with distance.

        Aerial perspective, and the cheapest depth cue available: the
        far end of the pole line has to be paler than the near end or
        the receding row reads as a flat cutout. The target is the sky
        just above the horizon, which is what a distant object is
        actually seen through.
        """
        if z <= _DR_HAZE_Z:
            return colour
        k = min(0.62, 0.62 * (z - _DR_HAZE_Z) / (_DR_PROP_Z_FAR - _DR_HAZE_Z))
        return _dr_lerp(colour, _DR_SKY_HORIZ, k)

    def _paint_cacti(self, canvas: Canvas) -> None:
        spacing = self._CACTUS_SPACING
        shifted = self._distance + self._CACTUS_OFFSET
        base = int(shifted // spacing)
        phase = shifted % spacing
        for i in range(int(_DR_PROP_Z_FAR / spacing) + 2):
            z = i * spacing - phase
            if z < _DR_Z_NEAR or z > _DR_PROP_Z_FAR:
                continue
            jitter, hscale, arm, arm_drop = self._cactus_slot(base + i)
            y_base = self._ground_y(z)
            height = _DR_F * self._CACTUS_HEIGHT * hscale / z
            if height < 3.0:
                continue    # too far to read as a plant
            colour = self._hazed(
                _DR_CACTUS_NEAR if z < 12.0 else _DR_CACTUS_FAR, z)
            trunk_w = 2 if height >= 8.0 else 1
            for side in (-1, 1):
                px = self._project(side * self._CACTUS_X + jitter, z)
                ix = int(round(px))
                if not (0 <= ix < 128):
                    continue
                y0 = max(0, int(round(y_base - height)))
                y1 = min(31, int(round(y_base)))
                for y in range(y0, y1 + 1):
                    for dx in range(trunk_w):
                        if 0 <= ix + dx < 128:
                            canvas.pixel(ix + dx, y, colour)
                if arm == 0 or height < 4.0:
                    continue
                # Saguaro arms: out along the elbow row, then straight
                # up. Length scales with the plant, so a near cactus
                # gets a real limb rather than the single stray pixel
                # the old version drew (which is why every cactus read
                # as a bare green post).
                arm_len = max(1, int(round(height * 0.30)))
                elbow_y = y0 + int(round(height * arm_drop / 100.0))
                for aside in ((-1, 1) if arm == 2 else (arm,)):
                    ex = ix + (trunk_w - 1 if aside > 0 else 0)
                    for k in range(1, arm_len + 1):
                        axp = ex + aside * k
                        if 0 <= axp < 128 and 0 <= elbow_y < 32:
                            canvas.pixel(axp, elbow_y, colour)
                    tip_x = ex + aside * arm_len
                    for k in range(1, arm_len + 2):
                        ty = elbow_y - k
                        if 0 <= tip_x < 128 and 0 <= ty < 32:
                            canvas.pixel(tip_x, ty, colour)

    def _paint_traffic(self, canvas: Canvas) -> None:
        """Draw every live vehicle, far to near.

        Painter's algorithm on Z. There is no depth buffer and there does
        not need to be: at most three vehicles exist, and the only
        overlap that matters is a near one hiding a far one.
        """
        for v in sorted(self._traffic, key=lambda d: -d["z"]):
            self._paint_vehicle(canvas, v)

    def _paint_vehicle(self, canvas: Canvas, v) -> None:
        z = v["z"]
        if z < self._EXIT_Z or z > _DR_Z_FAR:
            return
        cx = self._project(v["lane"] + v["drift"], z)
        w = _DR_F * v["w"] / z
        h = _DR_F * v["h"] / z
        y_base = self._ground_y(z)
        truck = bool(v["truck"])
        oncoming = v["dir"] == -1

        if h >= 2.0:
            # Big enough to be a shape rather than a smudge, so give it a
            # body. Below that threshold the lights alone are the honest
            # read: a car half a mile off at dusk IS two points of light.
            body = self._hazed(_DR_TRUCK_BODY if truck else _DR_CAR_BODY, z)
            upper = self._hazed(_DR_TRUCK_BODY if truck else _DR_CAR_ROOF, z)
            edge = self._hazed(_DR_CAR_EDGE, z)
            x0 = int(round(cx - w * 0.5))
            x1 = int(round(cx + w * 0.5))
            y_top = int(round(y_base - h))
            y_bot = int(round(y_base))
            # A car's cabin is narrower than its body; a trailer is a
            # slab all the way up. Skipping that inset is what makes a
            # pixel car read as a shipping container.
            cab_h = max(1, int(round(h * (0.55 if truck else 0.42))))
            inset = 0 if truck else max(0, int(round(w * 0.16)))
            for y in range(max(0, y_top), min(31, y_bot) + 1):
                in_cab = y < y_top + cab_h
                rx0 = x0 + inset if in_cab else x0
                rx1 = x1 - inset if in_cab else x1
                colour = upper if in_cab else body
                for x in range(max(0, rx0), min(127, rx1) + 1):
                    canvas.pixel(x, y, colour)
            # Rim light along the roof and both shoulders. Without it the
            # body is unreadable: a dark silhouette is invisible against
            # the near asphalt (18, 18, 28), which is nearly as dark, and
            # only the roof band showed -- a car ahead read as a small
            # floating grey smudge instead of a car. The rim is also the
            # real thing to draw, since the sky glow behind the vehicle
            # catches its top edge and shoulders.
            if w >= 3.0 and h >= 3.0:
                for x in range(max(0, x0 + inset), min(127, x1 - inset) + 1):
                    if 0 <= y_top < 32:
                        canvas.pixel(x, y_top, edge)
                for y in range(max(0, y_top), min(31, y_bot) + 1):
                    in_cab = y < y_top + cab_h
                    lx_e = x0 + inset if in_cab else x0
                    rx_e = x1 - inset if in_cab else x1
                    if 0 <= lx_e < 128:
                        canvas.pixel(lx_e, y, edge)
                    if 0 <= rx_e < 128:
                        canvas.pixel(rx_e, y, edge)
            if not truck and cab_h >= 2 and h >= 5.0:
                # Glass: windscreen on an oncoming car, rear window on
                # one ahead. Same band either way at this size.
                glass = self._hazed(_DR_CAR_GLASS, z)
                gy = y_top + 1
                if 0 <= gy < 32:
                    for x in range(max(0, x0 + inset + 1), min(127, x1 - inset - 1) + 1):
                        canvas.pixel(x, gy, glass)
            if truck and w >= 5.0 and 0 <= y_top < 32:
                # Trailer marker lights along the top edge -- the single
                # cue that separates a truck from a tall car at a glance.
                trim = self._hazed(_DR_TRUCK_TRIM, z)
                for frac in (0.15, 0.5, 0.85):
                    mx = int(round(x0 + (x1 - x0) * frac))
                    if 0 <= mx < 128:
                        canvas.pixel(mx, y_top, trim)

        # Lamps sit low: headlights are near the bumper, tail lights a
        # little higher on the tailgate. Putting them at mid-body height
        # made every vehicle read as a tram.
        if oncoming:
            lamp, halo, sep, frac = (
                _DR_HEADLIGHT, _DR_HEADLIGHT_HALO, self._HEADLIGHT_SEP, 0.18)
        else:
            lamp, halo, sep, frac = (
                _DR_TAIL, _DR_TAIL_HALO, self._TAIL_SEP, 0.34)
        # Lift the lamps off the base row only when the vehicle is big
        # enough for the lift to mean something. The old unconditional
        # 1 px lift put the far red dot on row 13 -- the horizon row,
        # which is sky -- so a distant car ahead read as a tail light
        # hovering above the road. Clamping into the road band fixes the
        # same thing from the other side: rows 16 units out and beyond
        # all compress onto row 14, and that is where a vehicle at the
        # vanishing point belongs.
        lift = h * frac
        ly = int(round(y_base - lift if lift >= 1.5 else y_base))
        ly = max(_DR_ROAD_TOP_Y, ly)
        lsep = _DR_F * sep * 0.5 / z
        # Bloom radius by proximity. Oncoming headlights get more of it
        # than tail lights because they are pointed at the camera: an
        # approaching car at dusk is the brightest thing in the frame by
        # a wide margin, and a fixed 3x3 block of halo does not carry
        # that -- it reads as a lit window punched into the body.
        if oncoming:
            radius = 3 if z < 2.0 else (2 if z < 4.5 else 1)
        else:
            radius = 2 if z < 3.0 else 1
        img = canvas.image_buffer
        for side in (-1.0, 1.0):
            lx = int(round(cx + side * lsep))
            if not (0 <= lx < 128 and 0 <= ly < 32):
                continue
            self._paint_lamp(canvas, img, lx, ly, lamp, halo, radius)

    @staticmethod
    def _paint_lamp(canvas: Canvas, img, lx: int, ly: int,
                    lamp, halo, radius: int) -> None:
        """One lamp: hot core, then a radial bloom into the background.

        The bloom blends with whatever is already on the panel rather
        than painting a flat halo colour, which is what makes it read as
        light instead of as a second, paler lamp. Falloff is on true
        radial distance, so the glow is round; a rectangular block of
        halo pixels is instantly recognisable as a sprite.
        """
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                x, y = lx + dx, ly + dy
                if not (0 <= x < 128 and 0 <= y < 32):
                    continue
                if dx == 0 and dy == 0:
                    canvas.pixel(x, y, lamp)
                    continue
                d = math.hypot(dx, dy)
                if d > radius + 0.35:
                    continue
                # Inside ~1.5 px the filament still dominates; past that
                # it is scatter, which is dimmer and warmer.
                target = lamp if d <= 1.45 else halo
                k = min(0.92, (1.0 - d / (radius + 1.0)) ** 1.6 * 1.35)
                if k <= 0.02:
                    continue
                canvas.pixel(x, y, _dr_lerp(img.getpixel((x, y))[:3], target, k))

    def _paint_dashboard(self, canvas: Canvas) -> None:
        canvas.fill_rect(0, _DR_DASH_Y, 128, 1, _DR_DASH_TRIM)
        canvas.fill_rect(0, _DR_DASH_Y + 1, 128, 1, _DR_DASH)

    def render(self, canvas: Canvas, tick: int) -> None:
        self._step(tick)
        self._paint_sky(canvas)
        self._paint_road(canvas)
        # Props last: poles and cacti straddle the horizon row, so they
        # cannot live inside either band.
        self._paint_poles(canvas)
        self._paint_cacti(canvas)
        self._paint_traffic(canvas)
        self._paint_dashboard(canvas)


# ---------------------------------------------------------------------------
# Registry + mode wrapper
# ---------------------------------------------------------------------------

# Ordered dict so the webapp picker shows the vibes in a stable order;
# campfire is first because it's the flagship of this mode.
_VIBES: dict[str, tuple[str, type]] = {
    "campfire": ("Campfire", _Campfire),
    "rain":     ("Rain",     _Rain),
    "aquarium": ("Aquarium", _Aquarium),
    "driving":  ("Driving",  _Driving),
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
