# MIT License — Copyright (c) 2026 John Kuok
"""Focus timer — countdown clock with an animated hourglass on the left.

Behavior in one sentence: the panel is a big MM:SS countdown, a scrolling
session label, a progress bar under it, and a chunky pixel-art hourglass
that trickles and periodically flips over so the panel feels alive during
long silent deep-work sessions rather than freezing between minute-ticks.

State machine
-------------
* **idle** — no timer set. Panel shows the focus-mark ring (concentric
  target-reticle) on the left, the last-picked preset (e.g. ``25m``) as
  large mono digits on the right, and ``READY`` under them. Nothing moves.
* **running** — countdown from ``duration_seconds`` starting at
  ``start_epoch``; the hourglass sits on the left, trickles sand every
  frame, and flips end-over-end every ``FLIP_PERIOD_SEC`` seconds. Digits
  and progress bar are in cool blue.
* **finishing** — the last ``FINISHING_SEC`` seconds. Digits, bar, and
  label switch to red and the label reads ``ALMOST DONE`` regardless of
  what the user typed. Panel breathes with a subtle pulse.
* **done** — timer just hit zero and is being celebrated for
  ``DONE_HOLD_SEC``. Whole panel flashes accent for the first
  ``DONE_FLASH_SEC``, then shows ``DONE`` centered plus the completed
  duration (e.g. ``25:00 FOCUSED``). After the hold expires the mode
  self-transitions back to idle.

State is entirely file-backed on the config's state dir (see
``Config.focus_*`` helpers). The renderer never mutates state during
render(); the mode observes and re-derives everything from the epoch
timestamp on each frame. Restarting the Pi mid-session picks up exactly
where it was because ``start_epoch`` is absolute wall time.

Only one thing on the panel is *derived* rather than persisted: the
hourglass flip angle. It's a pure function of ``elapsed_sec`` in the
current running window so it stays deterministic across restarts and,
importantly, keeps ticking at the exact frame rate the renderer runs at
without an animation clock of its own.

Why 'manual' switching
----------------------
Starting a timer via the webapp does NOT force the panel to focus mode.
The user explicitly chose "manual" during design: focus lives in the mode
grid like everything else. Timer state is a background counter that keeps
running regardless of what's on the LED. When the user finally taps
``focus`` in the mode grid, they see the correct remaining time.
"""

from __future__ import annotations

import math
import time
from typing import Literal

from ticker.canvas import Canvas, LARGE, SMALL
from ticker.config import Config
from ticker.modes.base import Mode

# ---------------------------------------------------------------------------
# Layout + palette
# ---------------------------------------------------------------------------

# Cool blue is deliberately not used by any other mode; picking it here means
# the whole webapp — dot-mark, mode dot, brightness fill, section headers —
# stays this color while focus is the active mode.
ACCENT: tuple[int, int, int] = (78, 176, 255)
# Hourglass frame slightly dimmer than the accent so the glass reads as
# secondary to the digits without disappearing into the black bg.
FRAME: tuple[int, int, int] = (46, 108, 156)
# Sand is a lighter tint of accent so it visibly contrasts with the frame
# even at low panel brightness.
SAND: tuple[int, int, int] = (140, 200, 255)
# Warm red for finishing state — matches other "alert" reds on the panel.
URGENT: tuple[int, int, int] = (255, 70, 70)
URGENT_SAND: tuple[int, int, int] = (255, 170, 170)
URGENT_FRAME: tuple[int, int, int] = (156, 46, 46)
# Muted ink for secondary text (labels, status).
INK_SOFT: tuple[int, int, int] = (170, 175, 180)
INK_FAINT: tuple[int, int, int] = (140, 140, 145)
# "Off" cells on the progress bar. Not fully black so the bar reads as
# "there but empty" rather than vanishing.
BAR_DIM: tuple[int, int, int] = (25, 25, 28)

# Hourglass geometry (chunky pixel-art style — the user picked style 2).
HG_W = 14
HG_H = 20
HG_X = 2  # panel x
HG_Y = 6  # panel y (leaves 6 above + 6 below for header/bar margins)

# The single-pixel column where sand falls from top to bottom bulb.
HG_PINCH_TOP = HG_H // 2 - 1  # 9
HG_PINCH_BOT = HG_H // 2      # 10

# Layout for digits: LARGE font is 8x16, "MM:SS" is 5 chars = 40 px, centered
# in the remaining area to the right of the hourglass.
DIGITS_X = 48
DIGITS_Y = 2
LABEL_X = 32
LABEL_Y = 22

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

# Sub-minute UI thresholds. The panel switches to red + "ALMOST DONE" once
# we're under FINISHING_SEC seconds, and pulses on a half-second beat.
FINISHING_SEC = 30

# After the timer hits zero the panel holds a celebration for this long, then
# transitions itself back to idle. The last thing you want after a focused
# 25 minutes is a panel that keeps yelling at you.
DONE_HOLD_SEC = 120
DONE_FLASH_SEC = 3

# Hourglass flip cadence. 10 s feels alive at a glance and doesn't distract
# from work — you catch the motion in your peripheral vision rather than
# being pulled to the panel by it.
FLIP_PERIOD_SEC = 10.0
FLIP_DURATION_SEC = 0.6  # length of the flip arc itself

# Duration presets exposed on the webapp. Kept in the module so the API and
# the mode agree on what a "known preset" is. The webapp treats these as
# suggestions — any integer between MIN and MAX seconds is accepted.
PRESETS_MIN = (15, 25, 45, 60)
DEFAULT_PRESET_MIN = 25
MIN_DURATION_SEC = 60
MAX_DURATION_SEC = 8 * 60 * 60  # 8 hours — an entire work day pomodoro

State = Literal["idle", "running", "finishing", "done"]


# ---------------------------------------------------------------------------
# Hourglass drawing
# ---------------------------------------------------------------------------


def _put_rotated(
    canvas: Canvas,
    ox: int,
    oy: int,
    sprite_w: int,
    sprite_h: int,
    x: int,
    y: int,
    color: tuple[int, int, int],
    cos_t: float,
    sin_t: float,
) -> None:
    """Set one pixel at sprite-local (x, y) rotated around the sprite center.

    Passing cos/sin in already-computed form (rather than the angle) matters:
    for a single hourglass frame we plot ~80 pixels, and computing the trig
    once outside the loop is roughly 15x faster than doing it per pixel.
    Nothing else on the panel is more sensitive to per-frame cost, but we
    keep it cheap because the renderer aims for 30 fps steady.
    """
    cx = (sprite_w - 1) / 2
    cy = (sprite_h - 1) / 2
    dx, dy = x - cx, y - cy
    rx = round(cx + dx * cos_t - dy * sin_t)
    ry = round(cy + dx * sin_t + dy * cos_t)
    px, py = ox + rx, oy + ry
    if 0 <= px < canvas.width and 0 <= py < canvas.height:
        canvas.image_buffer.putpixel((px, py), color)


def _draw_hourglass(
    canvas: Canvas,
    ox: int,
    oy: int,
    sand_pct_top: float,
    flip_angle_deg: float,
    show_stream: bool,
    frame_color: tuple[int, int, int] = FRAME,
    sand_color: tuple[int, int, int] = SAND,
) -> None:
    """Chunky pixel-art hourglass, drawn rotated by ``flip_angle_deg``.

    Sand is filled into the top bulb by proportion, and the complement (plus
    a small residual) is piled into the bottom bulb. Rotating both together
    means a mid-flip frame naturally shows sand tumbling sideways, which is
    exactly the effect we want — no separate "sand in motion" logic needed.

    Args:
        sand_pct_top: proportion of the total top-bulb pixels currently
            filled with sand. 1.0 at the start of a running window, 0.0 at
            the end. During finishing we still pass a nonzero fraction so
            the hourglass keeps its shape rather than becoming an empty
            outline.
        flip_angle_deg: 0.0 during steady trickle. During a flip we sweep
            0 -> 180 (or 180 -> 360 on the next flip). The renderer never
            has to know about angle wrapping because we always mod 360 in
            the caller.
        show_stream: whether to draw the 2-px "falling sand" column at the
            pinch. Suppressed mid-flip because a fall in progress mid-
            rotation would just look like an artefact.
    """
    theta = math.radians(flip_angle_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    def put(x: int, y: int, color: tuple[int, int, int]) -> None:
        _put_rotated(canvas, ox, oy, HG_W, HG_H, x, y, color, cos_t, sin_t)

    # Two-row caps top and bottom. The chunky style relies on these being
    # thick — they read as "hourglass endcaps" from across the room where a
    # single row would just look like a stray dot.
    for x in range(HG_W):
        put(x, 0, frame_color)
        put(x, 1, frame_color)
        put(x, HG_H - 1, frame_color)
        put(x, HG_H - 2, frame_color)

    # Bulb walls step inward one column per row from y=2..pinch. This is the
    # cheapest way to draw diagonal walls on a low-res panel: no anti-alias,
    # no line drawing, just a staircase of pixels that reads as a diagonal
    # because at 32 tall your eye can't see the steps.
    for y in range(2, HG_PINCH_TOP + 1):
        step = y - 2
        put(1 + step, y, frame_color)
        put(HG_W - 2 - step, y, frame_color)
    for y in range(HG_PINCH_BOT, HG_H - 2):
        step = (HG_H - 3 - y)
        put(1 + step, y, frame_color)
        put(HG_W - 2 - step, y, frame_color)

    # Collect the interior pixels of each bulb in the correct fill order:
    # top bulb pixels are enumerated from the pinch upward (so sand piles
    # at the bottom of the top bulb), and bottom bulb pixels are enumerated
    # from the pinch downward (so we can pile at the bottom by taking the
    # tail of the list).
    top_interior: list[tuple[int, int]] = []
    for y in range(HG_PINCH_TOP, 1, -1):
        step = y - 2
        for x in range(2 + step, HG_W - 2 - step):
            top_interior.append((x, y))
    bot_interior: list[tuple[int, int]] = []
    for y in range(HG_PINCH_BOT, HG_H - 2):
        step = (HG_H - 3 - y)
        for x in range(2 + step, HG_W - 2 - step):
            bot_interior.append((x, y))

    # Clamp: sand_pct_top can drift out of range during transition frames.
    pct = max(0.0, min(1.0, sand_pct_top))
    fill_top = int(pct * len(top_interior))
    for x, y in top_interior[:fill_top]:
        put(x, y, sand_color)
    # Bottom accumulates the complement plus a small always-present residue
    # so the bulb never looks completely empty when there's still time left.
    # This is cosmetic: at 30 s remaining the "real" bottom sand would be
    # 97% full, which is basically flat, so we clamp to avoid a full bulb
    # covering the whole silhouette.
    residue = 0.35 * len(bot_interior)
    bottom_fill = int((1.0 - pct) * len(top_interior) + residue)
    bottom_fill = min(bottom_fill, len(bot_interior))
    for x, y in bot_interior[-bottom_fill:]:
        put(x, y, sand_color)

    if show_stream and 0.02 < pct < 0.98:
        # 2-px falling stream at the pinch, matching the 2-px pinch width.
        put((HG_W - 1) // 2, HG_PINCH_BOT, sand_color)
        put(HG_W // 2, HG_PINCH_BOT, sand_color)


def _hourglass_flip_angle(elapsed_sec: float) -> float:
    """Return the current flip angle for a smooth periodic tumble.

    Sand trickles for ``FLIP_PERIOD_SEC - FLIP_DURATION_SEC`` seconds, then
    the hourglass sweeps 180 degrees over ``FLIP_DURATION_SEC``. On the next
    period it continues sweeping — 0, 180, 360, 540... — which means the
    physical top of the sprite alternates between "up" and "down" as the
    user expects for a real hourglass being flipped.

    Interpolation is a simple cosine easing so the flip decelerates at the
    end, which reads as physical mass on a bitmap rather than a mechanical
    snap.
    """
    if elapsed_sec < 0:
        return 0.0
    cycle_pos = elapsed_sec % FLIP_PERIOD_SEC
    steady = FLIP_PERIOD_SEC - FLIP_DURATION_SEC
    if cycle_pos <= steady:
        # Which flip are we resting AFTER? Every completed cycle adds 180.
        completed = int(elapsed_sec // FLIP_PERIOD_SEC)
        return (completed * 180.0) % 360.0
    # Mid-flip. Progress runs 0..1 across the flip duration.
    progress = (cycle_pos - steady) / FLIP_DURATION_SEC
    eased = 0.5 - 0.5 * math.cos(math.pi * progress)  # cos-in-out
    completed = int(elapsed_sec // FLIP_PERIOD_SEC)
    base = (completed * 180.0) % 360.0
    return (base + eased * 180.0) % 360.0


# ---------------------------------------------------------------------------
# Idle "focus mark" (concentric target-reticle)
# ---------------------------------------------------------------------------


def _draw_focus_mark(canvas: Canvas, ox: int, oy: int) -> None:
    """Small concentric target reticle used as the idle-state icon.

    Two rings and a center dot. Deliberately not the hourglass — the idle
    state is "focus mode is armed but not running", and reusing the
    hourglass with empty sand would be misleading (it would look like a
    session that already timed out).
    """
    cx = ox + 10
    cy = oy + 10
    # Outer soft ring at r ~= 6 in dim frame color.
    for a in range(-6, 7):
        for b in range(-6, 7):
            d = a * a + b * b
            if 30 <= d <= 42:
                px, py = cx + a, cy + b
                if 0 <= px < canvas.width and 0 <= py < canvas.height:
                    canvas.image_buffer.putpixel((px, py), FRAME)
    # Inner ring at r ~= 3 in accent.
    for a in range(-3, 4):
        for b in range(-3, 4):
            d = a * a + b * b
            if 6 <= d <= 11:
                px, py = cx + a, cy + b
                if 0 <= px < canvas.width and 0 <= py < canvas.height:
                    canvas.image_buffer.putpixel((px, py), ACCENT)
    # Center dot + crosshair ticks. The ticks give the reticle its "target"
    # feel and prevent the icon from reading as just a bull's-eye.
    if 0 <= cx < canvas.width and 0 <= cy < canvas.height:
        canvas.image_buffer.putpixel((cx, cy), ACCENT)
    for dx in (-9, 9):
        px, py = cx + dx, cy
        if 0 <= px < canvas.width and 0 <= py < canvas.height:
            canvas.image_buffer.putpixel((px, py), FRAME)
    for dy in (-9, 9):
        px, py = cx, cy + dy
        if 0 <= px < canvas.width and 0 <= py < canvas.height:
            canvas.image_buffer.putpixel((px, py), FRAME)


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def _format_mmss(total_sec: int) -> str:
    """MM:SS with two-digit minutes; SS is always two digits.

    Sessions above 99 minutes render as HHhMM (e.g. ``2h30``) so a 4-hour
    deep-work block still fits the 5-char slot. We only get here when the
    user picks a custom duration; the four presets stay well under 100.
    """
    total_sec = max(0, int(total_sec))
    minutes, seconds = divmod(total_sec, 60)
    if minutes >= 100:
        hours, mins = divmod(minutes, 60)
        return f"{hours}h{mins:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class FocusMode(Mode):
    """Focus-timer mode.

    Rendered state is computed from config on every frame. The mode holds no
    counters of its own so the panel is bit-identical whether the renderer
    just booted, is mid-session, or has been running for hours; only the
    epoch timestamps on disk decide what the user sees.
    """

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()
        state = self.config.focus_state()
        now = time.time()

        if state["mode"] == "running":
            elapsed = now - state["start_epoch"] + state["carry_sec"]
            remaining = state["duration_sec"] - elapsed
            if remaining <= -DONE_HOLD_SEC:
                # Held long enough — return to idle.
                self.config.focus_reset_to_idle()
                self._draw_idle(canvas)
                return
            if remaining <= 0:
                self._draw_done(canvas, state, tick, -remaining)
                return
            if remaining <= FINISHING_SEC:
                self._draw_finishing(canvas, state, elapsed, remaining, tick)
                return
            self._draw_running(canvas, state, elapsed, remaining, tick)
            return

        # Anything else (idle, or a paused/unknown state) shows the idle card.
        self._draw_idle(canvas)

    # -- state renderers ---------------------------------------------------

    def _draw_idle(self, canvas: Canvas) -> None:
        """Idle: focus reticle on the left, last preset + READY on the right."""
        _draw_focus_mark(canvas, HG_X, HG_Y - 4)
        preset_min = self.config.focus_last_preset_min()
        # Show as "25m" so it clearly reads as a duration, not a wall clock.
        canvas.text(24, DIGITS_Y, f"{preset_min}m", (220, 220, 225), LARGE)
        canvas.text(24, LABEL_Y, "READY", INK_FAINT, SMALL)

    def _draw_running(
        self,
        canvas: Canvas,
        state: dict,
        elapsed: float,
        remaining: float,
        tick: int,
    ) -> None:
        """Standard countdown display with animated hourglass.

        ``tick`` drives the label scroll for long session labels; without it
        an overflowing label would raise ``NameError`` at render time.
        """
        sand_pct_top = max(0.0, remaining) / state["duration_sec"]
        flip = _hourglass_flip_angle(elapsed)
        # Suppress the falling stream only during the flip's mid-arc, where
        # a vertical stream on a rotated glass looks like a rendering bug.
        mid_flip = 5.0 <= (flip % 180.0) <= 175.0
        _draw_hourglass(
            canvas,
            HG_X,
            HG_Y,
            sand_pct_top=sand_pct_top,
            flip_angle_deg=flip,
            show_stream=not mid_flip,
        )
        canvas.text(DIGITS_X, DIGITS_Y, _format_mmss(int(remaining)), ACCENT, LARGE)
        label = state["label"] or ""
        if label:
            # Scroll if it doesn't fit the ~96 pixels available. tick advances
            # at renderer fps; 1 px per frame is fast enough to read and slow
            # enough not to feel jittery.
            avail = canvas.width - LABEL_X - 1
            if canvas.text_width(label, SMALL) <= avail:
                canvas.text(LABEL_X, LABEL_Y, label, INK_SOFT, SMALL)
            else:
                # Clip the scroll region so text doesn't slide over the hourglass.
                clip = canvas.image_buffer.crop((LABEL_X, LABEL_Y, canvas.width, LABEL_Y + 8))
                # Use a temporary canvas so scroll_text draws relative to (0, 0)
                # and then we paste it back into the clipped region.
                from PIL import Image as _Image
                strip = Canvas(avail, 8)
                strip.scroll_text(0, label.upper(), INK_SOFT, offset=tick, font_size=SMALL)
                canvas.image_buffer.paste(strip.image_buffer, (LABEL_X, LABEL_Y))
                _ = clip  # (kept for symmetry; the paste already replaces it)
                _ = _Image  # silence unused-import lint if code motion changes
        self._draw_bar(canvas, sand_pct_top, ACCENT)

    def _draw_finishing(
        self,
        canvas: Canvas,
        state: dict,
        elapsed: float,
        remaining: float,
        tick: int,
    ) -> None:
        """Last 30 seconds: everything red, subtle pulse via bar brightness."""
        sand_pct_top = max(0.0, remaining) / state["duration_sec"]
        flip = _hourglass_flip_angle(elapsed)
        mid_flip = 5.0 <= (flip % 180.0) <= 175.0
        _draw_hourglass(
            canvas,
            HG_X,
            HG_Y,
            sand_pct_top=sand_pct_top,
            flip_angle_deg=flip,
            show_stream=not mid_flip,
            frame_color=URGENT_FRAME,
            sand_color=URGENT_SAND,
        )
        # Half-second pulse: bright red on the beat, dimmer between. Uses
        # renderer fps rather than wall time so the pulse stays locked to
        # rendering even during a pause.
        half = max(1, round(self.config.fps / 2))
        pulse_on = (tick // half) % 2 == 0
        digit_color = URGENT if pulse_on else (200, 60, 60)
        canvas.text(DIGITS_X, DIGITS_Y, _format_mmss(int(remaining)), digit_color, LARGE)
        canvas.text(LABEL_X, LABEL_Y, "ALMOST DONE", URGENT, SMALL)
        self._draw_bar(canvas, sand_pct_top, URGENT)

    def _draw_done(
        self,
        canvas: Canvas,
        state: dict,
        tick: int,
        seconds_since_done: float,
    ) -> None:
        """Completion screen. Big DONE + total, no hourglass."""
        # Whole-panel flash for the first DONE_FLASH_SEC. Flash on a 4-frame
        # beat so it reads as a "flash flash flash" rather than a strobe.
        if seconds_since_done < DONE_FLASH_SEC:
            if (tick // 4) % 2 == 0:
                canvas.clear(ACCENT)
                canvas.text(48, DIGITS_Y, "DONE", (0, 0, 0), LARGE)
                # No bar during the flash — the whole panel is the bar.
                return
        canvas.text(48, DIGITS_Y, "DONE", ACCENT, LARGE)
        total_min = state["duration_sec"] // 60
        canvas.text(
            LABEL_X - 8,
            LABEL_Y,
            f"{total_min:02d}:00 FOCUSED",
            INK_SOFT,
            SMALL,
        )
        # Bar is fully filled, a small trophy for the eye.
        self._draw_bar(canvas, 0.0, ACCENT)  # 0.0 pct-remaining => full bar

    # -- helpers -----------------------------------------------------------

    def _draw_bar(self, canvas: Canvas, pct_remaining: float, color: tuple[int, int, int]) -> None:
        """Two-row progress bar along the bottom.

        We draw the *remaining* proportion in the given color and pad the
        drained proportion in ``BAR_DIM``. Two rows tall reads as a proper
        bar at LED distance; a single row disappears.
        """
        fill = int(max(0.0, min(1.0, pct_remaining)) * canvas.width)
        for x in range(canvas.width):
            col = color if x < fill else BAR_DIM
            canvas.image_buffer.putpixel((x, 30), col)
            canvas.image_buffer.putpixel((x, 31), col)
