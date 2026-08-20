# MIT License — Copyright (c) 2026 John Kuok
"""Local Raspberry Pi health: temperature, load, memory, disk, uptime, services.

The panel is normally busy telling you about the outside world -- markets,
trains, weather. Every other mode blows up on a bad DNS resolver or a stale
API. This mode reads only from the Pi itself, so it's the honest one when
everything else has drifted: if the panel lights up at all, this card still
tells the truth about what's underneath it.

All readings come from procfs, sysfs, or ``shutil.disk_usage``. There's no
``psutil`` dependency because the surface area we need is small enough to
read directly, and the extra install shouldn't gate a diagnostic mode.

Three panels rotate through the mode: temperature + load, memory + disk,
uptime + services. Each holds long enough to actually read, short enough
that a glance covers the whole picture in under a rotation.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ticker.canvas import HUGE, LARGE, SMALL, Canvas
from ticker.modes.base import Mode

LOGGER = logging.getLogger(__name__)

WHITE = (235, 240, 250)
DIM = (108, 122, 148)
GOOD = (0, 228, 0)
WARN = (255, 176, 0)
BAD = (255, 76, 76)
LOADING = (130, 180, 255)

RIGHT_INSET = 1
HERO_WIDTH = 24
COLUMN_X = HERO_WIDTH + 5

# Seven seconds per slide is a touch longer than AQI. The numbers here don't
# change fast enough to reward faster rotation, and the third slide (services)
# rewards a moment of dwell so a red service actually alarms rather than
# flickering past.
SLIDE_SECONDS = 7

# Thresholds for the small colour ladder. Pi 5 with the active cooler hovers
# around 45-55C idle and hits 70-75C under sustained load; the SoC's own
# throttling kicks in at 80C. So amber at 70, red at 78 leaves a two-degree
# buffer before the OS starts throttling on us.
TEMP_WARN_C = 70.0
TEMP_BAD_C = 78.0
# Pi 5 has 4 cores. Load averaging steady above 3 means we're falling
# behind; above 4 means we're queueing work.
LOAD_WARN = 3.0
LOAD_BAD = 4.0
# Memory pressure: warn when < 20% free, red at < 10%.
MEM_WARN_FRAC = 0.20
MEM_BAD_FRAC = 0.10
# Disk pressure at the classic 90 / 95% marks.
DISK_WARN_FRAC = 0.10
DISK_BAD_FRAC = 0.05

# The services the ticker actually owns. Kept as a tuple so the rendering
# order is stable and matches how the units are enumerated in the systemd
# folder.
SERVICES: tuple[str, ...] = ("ticker", "ticker-web")

# Refresh cadence. Reading /proc is nearly free but doing it every frame is
# still wasteful; once a second is fast enough for temp and load to feel
# live, and slow enough that no other mode notices this one exists.
REFRESH_SECONDS = 1.0
# systemctl calls are more expensive (they fork), so pace those separately.
SERVICES_REFRESH_SECONDS = 5.0


@dataclass(frozen=True)
class HealthReading:
    temp_c: float | None
    load1: float | None
    load5: float | None
    load15: float | None
    mem_used_bytes: int | None
    mem_total_bytes: int | None
    disk_used_bytes: int | None
    disk_total_bytes: int | None
    uptime_seconds: float | None


def _read_temp_c() -> float | None:
    """Read the SoC temperature from the standard Linux thermal zone.

    Millidegrees C on Raspberry Pi OS; return None if the sysfs node is
    missing (e.g. running tests on a laptop) so the render path can show
    "--" rather than crash.
    """
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        raw = path.read_text().strip()
        return float(raw) / 1000.0
    except (OSError, ValueError):
        return None


def _read_loadavg() -> tuple[float | None, float | None, float | None]:
    try:
        one, five, fifteen = os.getloadavg()
        return one, five, fifteen
    except (OSError, AttributeError):
        return None, None, None


def _read_meminfo() -> tuple[int | None, int | None]:
    """(used_bytes, total_bytes) from /proc/meminfo, or (None, None).

    Uses MemAvailable (kernel 3.14+) rather than MemFree because free is
    only the never-touched pool - most Linux systems keep a lot of memory
    as reclaimable cache, and quoting free would make every Pi look
    starving.
    """
    try:
        raw = Path("/proc/meminfo").read_text()
    except OSError:
        return None, None
    total_kb: int | None = None
    available_kb: int | None = None
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            if key == "MemTotal":
                total_kb = int(parts[1])
            elif key == "MemAvailable":
                available_kb = int(parts[1])
        if total_kb is not None and available_kb is not None:
            break
    if total_kb is None or available_kb is None:
        return None, None
    total = total_kb * 1024
    used = max(0, total - available_kb * 1024)
    return used, total


def _read_disk() -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage("/")
        return usage.used, usage.total
    except OSError:
        return None, None


def _read_uptime() -> float | None:
    try:
        raw = Path("/proc/uptime").read_text().strip()
        return float(raw.split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _systemctl_active(unit: str) -> bool:
    """Return True if systemctl reports the unit as active.

    ``systemctl is-active`` exits 0 on active, nonzero on anything else,
    and the output line ("active"/"inactive"/"failed") is more useful than
    the exit code because it doesn't blur "failed" and "inactive" together.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _format_bytes(value: int) -> str:
    """Human-readable bytes with a single-letter unit, fitting in the small column."""
    if value < 1024:
        return f"{value}B"
    if value < 1024**2:
        return f"{value / 1024:.0f}K"
    if value < 1024**3:
        return f"{value / (1024 ** 2):.0f}M"
    return f"{value / (1024 ** 3):.1f}G"


def _format_uptime(seconds: float) -> str:
    """Compact uptime: 3d 4h, 4h 12m, or 12m depending on magnitude.

    Never shows seconds - if a Pi has been up for 47 seconds you probably
    know why and don't need the panel to tick that number down.
    """
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _temp_color(temp: float) -> tuple[int, int, int]:
    if temp >= TEMP_BAD_C:
        return BAD
    if temp >= TEMP_WARN_C:
        return WARN
    return GOOD


def _load_color(load: float) -> tuple[int, int, int]:
    if load >= LOAD_BAD:
        return BAD
    if load >= LOAD_WARN:
        return WARN
    return GOOD


def _mem_color(used_frac: float) -> tuple[int, int, int]:
    free_frac = 1.0 - used_frac
    if free_frac <= MEM_BAD_FRAC:
        return BAD
    if free_frac <= MEM_WARN_FRAC:
        return WARN
    return GOOD


def _disk_color(used_frac: float) -> tuple[int, int, int]:
    free_frac = 1.0 - used_frac
    if free_frac <= DISK_BAD_FRAC:
        return BAD
    if free_frac <= DISK_WARN_FRAC:
        return WARN
    return GOOD


class PiHealthMode(Mode):
    """Read-only local diagnostics: three cards, no network, no config."""

    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        super().__init__(config)
        self.reading: HealthReading | None = None
        self._last_refresh = -1e9
        self._service_state: dict[str, bool] = {name: False for name in SERVICES}
        self._last_services_refresh = -1e9

    # -- readings --------------------------------------------------------------

    def _refresh(self) -> None:
        mem_used, mem_total = _read_meminfo()
        disk_used, disk_total = _read_disk()
        load1, load5, load15 = _read_loadavg()
        self.reading = HealthReading(
            temp_c=_read_temp_c(),
            load1=load1,
            load5=load5,
            load15=load15,
            mem_used_bytes=mem_used,
            mem_total_bytes=mem_total,
            disk_used_bytes=disk_used,
            disk_total_bytes=disk_total,
            uptime_seconds=_read_uptime(),
        )
        self._last_refresh = time.monotonic()

    def _refresh_services(self) -> None:
        self._service_state = {name: _systemctl_active(name) for name in SERVICES}
        self._last_services_refresh = time.monotonic()

    # -- render ----------------------------------------------------------------

    def render(self, canvas: Canvas, tick: int) -> None:
        canvas.clear()

        now = time.monotonic()
        if now - self._last_refresh >= REFRESH_SECONDS:
            self._refresh()
        if now - self._last_services_refresh >= SERVICES_REFRESH_SECONDS:
            self._refresh_services()

        if self.reading is None:
            canvas.text_centered(6, "PI HEALTH", LOADING, SMALL)
            canvas.text_centered(18, "LOADING", LOADING, SMALL)
            return

        ticks_per_slide = max(1, int(SLIDE_SECONDS * self.config.fps))
        slide = (tick // ticks_per_slide) % 3
        if slide == 0:
            self._render_temp(canvas)
        elif slide == 1:
            self._render_mem_disk(canvas)
        else:
            self._render_uptime_services(canvas)

    def _render_temp(self, canvas: Canvas) -> None:
        assert self.reading is not None
        r = self.reading

        # Hero: temp in whole degrees. Fractional temperature bounces every
        # refresh; the panel isn't a thermometer, it's a health check.
        if r.temp_c is not None:
            value = int(round(r.temp_c))
            color = _temp_color(r.temp_c)
            value_text = str(value)
        else:
            color = DIM
            value_text = "--"
        value_font = HUGE if len(value_text) <= 2 else LARGE
        value_y = 4 if value_font == HUGE else 9
        value_x = max(0, (HERO_WIDTH - canvas.text_bold_width(value_text, value_font)) // 2)
        canvas.text_bold(value_x, value_y, value_text, color, value_font)

        column_x = COLUMN_X
        column = canvas.width - column_x - RIGHT_INSET

        # Spleen doesn't ship a degree-sign glyph, so "\u00b0C" would render
        # as a blank cell. "C" alone is unambiguous next to the TEMP label
        # and doesn't leave a dead pixel column.
        canvas.text(column_x, 1, "TEMP", DIM, SMALL)
        unit_x = column_x + canvas.text_width("TEMP ", SMALL)
        canvas.text(unit_x, 1, "C", color, SMALL)

        # Load row: 1/5/15 as three small cells so a spike (high 1min against
        # normal 15min) is visible. Colour follows the 1min average.
        if r.load1 is not None:
            load_color = _load_color(r.load1)
            load_text = f"{r.load1:.2f} {r.load5:.2f} {r.load15:.2f}" if (r.load5 is not None and r.load15 is not None) else f"{r.load1:.2f}"
        else:
            load_color = DIM
            load_text = "--"
        canvas.text(column_x, 11, "LD", DIM, SMALL)
        load_x = column_x + canvas.text_width("LD ", SMALL)
        canvas.text(load_x, 11, canvas.fit(load_text, canvas.width - load_x - RIGHT_INSET, SMALL), load_color, SMALL)

        # Third line: a bar showing "how close to throttle". Zero at 40C,
        # full at 80C, so a healthy Pi shows about a third full.
        if r.temp_c is not None:
            frac = max(0.0, min(1.0, (r.temp_c - 40.0) / (80.0 - 40.0)))
            bar_w = column
            fill_w = int(round(bar_w * frac))
            # Track (dim) full width, then fill.
            for x in range(column_x, column_x + bar_w):
                canvas.pixel(x, 26, DIM)
            for x in range(column_x, column_x + fill_w):
                for y in range(24, 30):
                    canvas.pixel(x, y, color)

    def _render_mem_disk(self, canvas: Canvas) -> None:
        assert self.reading is not None
        r = self.reading

        # Hero: memory used percent. A single percentage number in the hero
        # column is the most-glanceable summary of "how tight are we".
        if r.mem_used_bytes is not None and r.mem_total_bytes:
            mem_frac = r.mem_used_bytes / r.mem_total_bytes
            mem_pct = int(round(mem_frac * 100))
            mem_color = _mem_color(mem_frac)
            value_text = f"{mem_pct}"
        else:
            mem_frac = None
            mem_color = DIM
            value_text = "--"
        value_font = HUGE if len(value_text) <= 2 else LARGE
        value_y = 4 if value_font == HUGE else 9
        value_x = max(0, (HERO_WIDTH - canvas.text_bold_width(value_text, value_font)) // 2)
        canvas.text_bold(value_x, value_y, value_text, mem_color, value_font)

        column_x = COLUMN_X
        column = canvas.width - column_x - RIGHT_INSET

        canvas.text(column_x, 1, "MEM", DIM, SMALL)
        unit_x = column_x + canvas.text_width("MEM ", SMALL)
        canvas.text(unit_x, 1, "%", mem_color, SMALL)

        # Absolute memory used / total, so you can tell a 60% number on a
        # 4GB Pi from the same 60% on an 8GB Pi.
        if r.mem_used_bytes is not None and r.mem_total_bytes:
            mem_line = f"{_format_bytes(r.mem_used_bytes)}/{_format_bytes(r.mem_total_bytes)}"
        else:
            mem_line = "--"
        canvas.text(column_x, 11, canvas.fit(mem_line, column, SMALL), DIM, SMALL)

        # Disk in a single line: DISK 12/58G. Disk is the second-most-common
        # thing that fills up on a Pi, so it belongs on the same card.
        if r.disk_used_bytes is not None and r.disk_total_bytes:
            disk_frac = r.disk_used_bytes / r.disk_total_bytes
            disk_color_val = _disk_color(disk_frac)
            disk_line = f"DSK {_format_bytes(r.disk_used_bytes)}/{_format_bytes(r.disk_total_bytes)}"
        else:
            disk_color_val = DIM
            disk_line = "DSK --"
        canvas.text(column_x, 22, canvas.fit(disk_line, column, SMALL), disk_color_val, SMALL)

    def _render_uptime_services(self, canvas: Canvas) -> None:
        assert self.reading is not None
        r = self.reading

        # Uptime: split "3d 4h" across the hero column visually. The days
        # number is the hero if we have days; otherwise hours.
        if r.uptime_seconds is not None:
            uptime_txt = _format_uptime(r.uptime_seconds)
            hero, _, tail = uptime_txt.partition(" ")
        else:
            hero, tail = "--", ""

        # Hero styling: whole hero uses LARGE so "12h" fits in the column.
        # HUGE is reserved for two-character values (99 fits, "12h" doesn't).
        value_font = LARGE
        value_y = 9
        value_x = max(0, (HERO_WIDTH - canvas.text_bold_width(hero, value_font)) // 2)
        canvas.text_bold(value_x, value_y, hero, WHITE, value_font)

        column_x = COLUMN_X
        column = canvas.width - column_x - RIGHT_INSET

        canvas.text(column_x, 1, "UP", DIM, SMALL)
        tail_x = column_x + canvas.text_width("UP ", SMALL)
        canvas.text(tail_x, 1, canvas.fit(tail, canvas.width - tail_x - RIGHT_INSET, SMALL), WHITE, SMALL)

        # Services: each one as a green dot + name, or a red dot + name if
        # inactive. The dot is what alarms; the name identifies the culprit.
        y = 11
        for name in SERVICES:
            active = self._service_state.get(name, False)
            dot_color = GOOD if active else BAD
            # Small filled dot (2x2) at the left of the row.
            for dy in range(2):
                for dx in range(2):
                    canvas.pixel(column_x + dx, y + 2 + dy, dot_color)
            name_x = column_x + 4
            # Ticker services all start with "ticker" so drop that prefix on
            # the panel: "ticker-web" -> "web", "ticker" -> "ticker" (kept
            # so it still reads as a unit, not just blank).
            display = name[len("ticker-") :] if name.startswith("ticker-") else name
            canvas.text(name_x, y, canvas.fit(display, canvas.width - name_x - RIGHT_INSET, SMALL), dot_color if not active else WHITE, SMALL)
            y += 10
