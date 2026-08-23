# MIT License — Copyright (c) 2026 John Kuok
"""Measure the campfire fluid solver's cost per frame, on the machine it runs on.

The vibes campfire is a real fluid simulation (advection, vorticity
confinement, Jacobi pressure projection) on a supersampled grid. It is
vectorised numpy, so its cost is dominated by memory bandwidth, and a
Pi 5 is nothing like a dev box on that axis. The 30fps budget is 33.3 ms
per frame for EVERYTHING -- solver, log pile, panel push -- so do not
trust an off-Pi estimate. Run this ON the Pi:

    cd /home/pi/ticker-pi5 && python scripts/bench_campfire.py

It prints median / p95 / max ms per frame for the solver plus its palette
mapping, and the share of the 30fps budget that represents. Comfortable
is p95 under about a third of the budget; above two thirds, drop
``_FluidFlame._SS`` from 2 to 1 or ``_JACOBI`` from 10 to 6 and re-measure.

Options:
    --frames N     timed frames (default 300)
    --warmup N     untimed frames first, so the plume is established (default 90)
    --fps N        budget reference (default 30)
    --full         time the whole Campfire.render, log pile and blit included
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=90)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument(
        "--full",
        action="store_true",
        help="time Campfire.render (solver + logs + blit) instead of the solver alone",
    )
    args = parser.parse_args()

    from ticker.modes.vibes import _Campfire, _FluidFlame, _np

    if _np is None:
        print("numpy is not installed, so the campfire runs the automaton fallback.")
        print("Install numpy (pip install -r requirements.txt) and re-run.")
        return 1

    if args.full:
        from ticker.canvas import Canvas

        fire = _Campfire()
        if fire._fluid is None:
            print("solver failed to initialise; the automaton fallback is active")
            return 1
        canvas = Canvas(128, 32)
        label = "Campfire.render (solver + log pile + blit)"

        def frame(tick: int) -> None:
            fire.render(canvas, tick=tick)

    else:
        flame = _FluidFlame()
        label = "_FluidFlame.step + rgb"

        def frame(tick: int) -> None:
            flame.step()
            flame.rgb()

    for tick in range(args.warmup):
        frame(tick)

    samples = []
    for i in range(args.frames):
        start = time.perf_counter()
        frame(args.warmup + i)
        samples.append((time.perf_counter() - start) * 1000.0)

    samples.sort()
    median = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(0.95 * len(samples)))]
    budget = 1000.0 / args.fps

    print(f"{label}, {args.frames} frames after {args.warmup} warmup")
    print(f"  median {median:6.2f} ms   ({median / budget * 100:5.1f}% of budget)")
    print(f"  p95    {p95:6.2f} ms   ({p95 / budget * 100:5.1f}% of budget)")
    print(f"  max    {samples[-1]:6.2f} ms   ({samples[-1] / budget * 100:5.1f}% of budget)")
    print(f"  budget {budget:6.2f} ms at {args.fps:g} fps")
    if p95 > budget * 0.66:
        print("  VERDICT: too tight. Drop _SS to 1 or _JACOBI to 6 and re-measure.")
    elif p95 > budget * 0.33:
        print("  VERDICT: fits, but little headroom for the rest of the frame.")
    else:
        print("  VERDICT: comfortable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
