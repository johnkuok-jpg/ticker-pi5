# MIT License — Copyright (c) 2026 John Kuok
"""Render the real display modes off-Pi into a browser-viewable LED simulator.

This imports the same Canvas and Mode classes the renderer uses, so what you see
here is what the panels will draw. No Piomatter, no GPIO, no hardware needed.

Usage:
    python scripts/preview.py --out preview_site
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ticker.canvas import Canvas  # noqa: E402
from ticker.config import load_config  # noqa: E402
from ticker.modes import MODE_TYPES  # noqa: E402

MAX_FRAMES = 900  # 30 seconds at 30fps


def render_mode_frames(name: str, config, max_frames: int = MAX_FRAMES) -> list[Image.Image]:
    """Render frames until the animation visibly repeats, so the loop is seamless."""
    mode = MODE_TYPES[name](config)

    # The live news feed carries ~15 headlines, whose combined marquee is far longer
    # than a browser-friendly spritesheet. Trim to a few so the preview loop closes.
    # The Pi itself always scrolls the full set.
    if name == "news":
        mode._refresh()  # noqa: SLF001 - preview-only priming
        if mode.headlines:
            mode.headlines = mode.headlines[:3]
        mode._last_refresh = float("inf")  # noqa: SLF001 - freeze content while sampling

    frames: list[Image.Image] = []
    seen: dict[str, int] = {}

    for tick in range(max_frames):
        canvas = Canvas(config.width, config.height)
        try:
            mode.render(canvas, tick)
        except Exception as exc:  # a mode should never do this, but never abort the preview
            print(f"  ! {name} raised on tick {tick}: {exc}")
            break

        frame = canvas.image_buffer.copy()
        digest = hashlib.sha1(frame.tobytes()).hexdigest()

        if digest in seen and tick > 1:
            # Frame repeated: the loop closed. Keep everything before the repeat.
            print(f"  loop closes at {len(frames)} frames")
            return frames

        seen[digest] = tick
        frames.append(frame)

    print(f"  no loop detected, using {len(frames)} frames")
    return frames


def build_spritesheet(frames: list[Image.Image]) -> Image.Image:
    """Stack frames vertically into one PNG the browser can sample cheaply."""
    if not frames:
        raise ValueError("no frames to pack")
    width, height = frames[0].size
    sheet = Image.new("RGB", (width, height * len(frames)), (0, 0, 0))
    for index, frame in enumerate(frames):
        sheet.paste(frame, (0, index * height))
    return sheet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="preview_site", help="output directory")
    parser.add_argument("--modes", default=",".join(MODE_TYPES), help="comma-separated modes")
    args = parser.parse_args()

    config = load_config()
    out_dir = Path(args.out).resolve()
    frames_dir = out_dir / "frames"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    frames_dir.mkdir(parents=True)

    manifest: dict[str, object] = {
        "width": config.width,
        "height": config.height,
        "fps": config.fps,
        "brightness": config.brightness,
        "symbols": list(config.symbols),
        "modes": {},
    }

    for name in [m.strip() for m in args.modes.split(",") if m.strip()]:
        if name not in MODE_TYPES:
            print(f"skipping unknown mode: {name}")
            continue
        print(f"rendering {name}...")
        frames = render_mode_frames(name, config)
        if not frames:
            continue
        sheet = build_spritesheet(frames)
        sheet.save(frames_dir / f"{name}.png", optimize=True)
        manifest["modes"][name] = {  # type: ignore[index]
            "frames": len(frames),
            "sheet": f"frames/{name}.png",
        }

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nwrote {out_dir}/manifest.json")
    for name, meta in manifest["modes"].items():  # type: ignore[union-attr]
        print(f"  {name:<10} {meta['frames']:>4} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
