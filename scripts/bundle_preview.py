# MIT License — Copyright (c) 2026 John Kuok
"""Inline the preview manifest and spritesheets into one self-contained HTML file.

Sandboxed iframes and file:// pages often block fetch() of sibling assets, which
leaves the simulator blank. Embedding everything as data URIs removes all network
dependencies so the page works anywhere.

Usage:
    python scripts/bundle_preview.py --site preview_site --out preview_site/index.html
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "scripts" / "preview_template.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="preview_site", help="directory holding manifest.json")
    parser.add_argument("--out", default=None, help="output HTML path")
    args = parser.parse_args()

    site = Path(args.site).resolve()
    manifest = json.loads((site / "manifest.json").read_text(encoding="utf-8"))

    # Replace each sheet path with an inline data URI.
    for name, meta in manifest["modes"].items():
        png_bytes = (site / meta["sheet"]).read_bytes()
        encoded = base64.b64encode(png_bytes).decode("ascii")
        meta["sheet"] = f"data:image/png;base64,{encoded}"
        print(f"  inlined {name}: {len(png_bytes) / 1024:.0f} KB -> {len(encoded) / 1024:.0f} KB base64")

    html = TEMPLATE.read_text(encoding="utf-8")

    # Swap the fetch()-based loader for a literal object.
    needle = 'const MANIFEST_URL = "manifest.json";'
    if needle not in html:
        raise SystemExit("template no longer contains the manifest URL hook")
    html = html.replace(needle, f"const INLINE_MANIFEST = {json.dumps(manifest)};")

    fetch_line = "  manifest = await (await fetch(MANIFEST_URL)).json();"
    if fetch_line not in html:
        raise SystemExit("template no longer contains the manifest fetch call")
    html = html.replace(fetch_line, "  manifest = INLINE_MANIFEST;")

    out = Path(args.out) if args.out else site / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"\nwrote {out} ({out.stat().st_size / 1024:.0f} KB, fully self-contained)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
