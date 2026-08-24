# MIT License — Copyright (c) 2026 John Kuok
"""Measure the driving vibe's per-frame cost, on the machine it runs on.

Unlike the campfire, the driving scene has no numpy in it at all: it is a
pure-Python software rasterizer. Every frame walks 16 sky rows and 16
road rows, blends anti-aliased span boundaries, and draws poles, wires,
cacti, dashes, flecks and headlights one pixel at a time. That makes its
cost interpreter-bound rather than memory-bound, which is the opposite
profile to the campfire and means a dev-box number translates to the Pi
differently -- a Pi 5 core is roughly 2-3x slower per Python bytecode op
than a modern desktop, so treat an off-Pi result as a lower bound only.
Run this ON the Pi before trusting any number:

    cd /home/pi/ticker-pi5 && python scripts/bench_driving.py

The Pi's dependencies live in the project venv, not in system Python, so
this re-execs itself into ``venv/bin/python`` when it finds one -- plain
``python scripts/...`` otherwise dies on ``No module named 'dotenv'``
before it draws a single mile of road.

It prints median / p95 / max ms per frame and the share of the 30fps
budget (33.3 ms for EVERYTHING, including the panel push) that
represents. If it comes back too tight, the levers in rough order of
payoff-per-ugliness are: drop ``_Driving._FLECKS_ROAD`` and
``_FLECKS_GROUND`` from 130 to 60 (cheapest, barely visible), lower
``_DR_PROP_Z_FAR`` from 18 to 12 (fewer poles and cacti on screen), cut
the dash coverage samples in ``_dash_coverage`` from 4 to 2 (far dashes
start to strobe), and last, skip the AA boundary blend in ``_dr_span``
(the road edges go back to reading as staircases, which is the thing
this rewrite existed to fix).

Options:
    --frames N     timed frames (default 300)
    --warmup N     untimed frames first, so props are mid-scene (default 90)
    --fps N        budget reference (default 30)
    --parts        also break the frame down by paint stage
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _reexec_in_venv() -> None:
    """Restart under ``venv/bin/python`` if we were started outside it.

    Same reasoning as ``bench_campfire.py``: the Pi keeps Pillow,
    python-dotenv and the rest in ``/home/pi/ticker-pi5/venv``, and a
    bare ``python scripts/...`` there fails at the first ``ticker``
    import in a way that looks like a broken script rather than the
    wrong interpreter. The env-var guard stops the second process
    re-execing again.
    """
    if os.environ.get("TICKER_BENCH_REEXEC"):
        return
    venv_python = PROJECT_ROOT / "venv" / "bin" / "python"
    try:
        already_inside = venv_python.resolve() == Path(sys.executable).resolve()
    except OSError:  # pragma: no cover - unreadable path, just carry on
        already_inside = True
    if already_inside or not venv_python.exists():
        return
    env = dict(os.environ, TICKER_BENCH_REEXEC="1")
    # flush=True matters: execve replaces the process image without
    # flushing Python's buffers, so a block-buffered print into a pipe
    # would be lost and the handoff would happen invisibly.
    print(f"re-running under {venv_python}", flush=True)
    os.execve(str(venv_python), [str(venv_python), *sys.argv], env)


def _summarise(label: str, samples: list[float], budget: float) -> float:
    samples = sorted(samples)
    median = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(0.95 * len(samples)))]
    print(f"{label}")
    print(f"  median {median:6.2f} ms   ({median / budget * 100:5.1f}% of budget)")
    print(f"  p95    {p95:6.2f} ms   ({p95 / budget * 100:5.1f}% of budget)")
    print(f"  max    {samples[-1]:6.2f} ms   ({samples[-1] / budget * 100:5.1f}% of budget)")
    return p95


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=90)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument(
        "--parts",
        action="store_true",
        help="also time each paint stage separately, to find where the frame goes",
    )
    args = parser.parse_args()

    _reexec_in_venv()

    from ticker.canvas import Canvas
    from ticker.modes.vibes import _Driving

    scene = _Driving()
    canvas = Canvas(128, 32)
    budget = 1000.0 / args.fps

    for tick in range(args.warmup):
        scene.render(canvas, tick=tick)

    samples = []
    for i in range(args.frames):
        start = time.perf_counter()
        scene.render(canvas, tick=args.warmup + i)
        samples.append((time.perf_counter() - start) * 1000.0)

    label = f"_Driving.render (full frame), {args.frames} frames after {args.warmup} warmup"
    p95 = _summarise(label, samples, budget)
    print(f"  budget {budget:6.2f} ms at {args.fps:g} fps")
    if p95 > budget * 0.66:
        print("  VERDICT: too tight. Cut _FLECKS_* to 60 and _DR_PROP_Z_FAR to 12, then re-measure.")
    elif p95 > budget * 0.33:
        print("  VERDICT: fits, but little headroom for the rest of the frame.")
    else:
        print("  VERDICT: comfortable.")

    if args.parts:
        # Per-stage timings are indicative, not additive: each stage is
        # measured while the others still run, so shared setup (the row
        # walk, the curve, the projection cache) is attributed to
        # whichever stage happens to touch it first.
        stages = [
            ("sky + sun + mesas", "_paint_sky"),
            ("road + lines + dashes", "_paint_road"),
            ("poles + wires", "_paint_poles"),
            ("cacti", "_paint_cacti"),
            ("headlights", "_paint_headlights"),
            ("dashboard", "_paint_dashboard"),
        ]
        print("\nper-stage (indicative, not additive):")
        for name, attr in stages:
            method = getattr(scene, attr, None)
            if method is None:
                print(f"  {name:24s} (not present)")
                continue
            timings: list[float] = []
            original = method

            def timed(*a: object, _orig=original, _sink=timings, **k: object):
                t0 = time.perf_counter()
                try:
                    return _orig(*a, **k)
                finally:
                    _sink.append((time.perf_counter() - t0) * 1000.0)

            setattr(scene, attr, timed)
            try:
                for i in range(args.frames):
                    scene.render(canvas, tick=args.warmup + args.frames + i)
            finally:
                try:
                    delattr(scene, attr)
                except AttributeError:  # pragma: no cover - bound on instance
                    setattr(scene, attr, original)
            if timings:
                med = statistics.median(timings)
                print(f"  {name:24s} {med:6.2f} ms median  ({med / budget * 100:5.1f}% of budget)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
