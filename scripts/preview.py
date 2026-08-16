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
import tempfile
import sys
from dataclasses import replace
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ticker.canvas import Canvas  # noqa: E402
from ticker.config import load_config  # noqa: E402
from ticker import net  # noqa: E402
from ticker.modes import MODE_TYPES  # noqa: E402

MAX_FRAMES = 900  # 30 seconds at 30fps

# Screens the preview offers, which is not the same list as the Pi's runtime
# modes: the stocks layouts are alternative designs for one mode, shown side by
# side so they can be compared before one is chosen.
#
# ``stride`` samples every Nth tick and ``frames`` fixes the count, together
# replacing loop detection. A layout that holds a still image for seconds at a
# time defeats hash-based detection, which would see frame 2 repeat frame 1 and
# declare the loop closed after two frames.
PREVIEW_SCREENS: dict[str, dict[str, object]] = {
    # Four frames of 15 ticks played at 2 fps is a two-second loop that runs in
    # real time, so the clock colon blinks here at the same 1 Hz it does on the
    # panel. Hash detection alone would stop at two frames and freeze it lit.
    "weather": {"mode": "weather", "frames": 4, "fps": 2, "stride": 15},
    "stocks": {"mode": "stocks", "layout": "card", "stride": 15, "frames": 36, "fps": 2},
    "stocks-scroll": {"mode": "stocks", "layout": "scroll"},
    "news": {"mode": "news"},
    "market": {"mode": "market", "frames": 4, "fps": 2, "stride": 15},
    "crypto": {"mode": "crypto"},
    # The BART header carries the same blinking colon as weather and market.
    "bart": {"mode": "bart", "frames": 4, "fps": 2, "stride": 15},
    "aqi": {"mode": "aqi"},
    # Both network screens are posed rather than read from the radio: this script
    # runs off-Pi, where there is no nmcli to ask, and the setup screen is by
    # definition a state a machine with a working network cannot be in.
    "wifi": {"mode": "net", "pose": "connected", "frames": 4, "fps": 2, "stride": 15},
    "wifi-setup": {"mode": "net", "pose": "setup", "frames": 4, "fps": 2, "stride": 15},
    # The detail field rotates every few seconds, so a single frame would hide
    # the gate and baggage readouts. Walk one full rotation instead.
    "flights": {
        "mode": "flights",
        # One item holds for DETAIL_ROTATE_SECONDS * config.fps == 120 ticks, so
        # a stride of 60 gives two captured frames per item.
        "frames": 12,
        "fps": 2,
        "stride": 60,
        "live_flight": True,
    },
    # Bay Wheels: static content, one representative station. Live GBFS may or
    # may not be reachable from the preview host, so the mode is pre-seeded
    # with a fake station rather than trusted to fetch here.
    "bikes": {"mode": "bikes", "seed_bikes": True, "frames": 4, "fps": 2, "stride": 15},
    # Name tag: single static frame with a sample name so the simulator shows
    # what the coworker's desk plate will look like.
    "nametag": {"mode": "nametag", "seed_nametag": True, "frames": 1, "fps": 1, "stride": 15},
    # Costco: Akamai blocks fetches from datacenter IPs (the preview builds
    # in one), so the mode is pre-seeded with three plausible Bay Area
    # warehouses. Walk one full slide rotation so the browser preview shows
    # all three warehouses rather than freezing on the first card.
    "costco": {"mode": "costco", "seed_costco": True, "frames": 18, "fps": 2, "stride": 15},
}


def find_live_flight(latitude: float, longitude: float) -> str:
    """An airline flight airborne near a point that also has a known route.

    A flight number written into the preview would be a dead number within hours,
    leaving the simulator showing NO ADS-B CONTACT forever. Picking a live one at
    build time keeps the flights screen populated whenever the preview is rebuilt.
    """
    import requests

    try:
        payload = requests.get(
            f"https://api.adsb.lol/v2/point/{latitude}/{longitude}/150", timeout=20
        ).json()
    except Exception as error:
        print(f"  could not reach the live feed ({error}); leaving the flight unset")
        return ""

    for aircraft in payload.get("ac", []):
        callsign = (aircraft.get("flight") or "").strip()
        if not callsign or aircraft.get("alt_baro") == "ground":
            continue
        if not (callsign[:3].isalpha() and callsign[3:].isdigit()):
            continue
        if not aircraft.get("gs") or float(aircraft["gs"]) < 200:
            continue
        try:
            route = requests.get(
                f"https://api.adsbdb.com/v0/callsign/{callsign}", timeout=15
            ).json()
        except Exception:
            continue
        if isinstance(route.get("response"), dict):
            print(f"  using live flight {callsign}")
            return callsign
    print("  no airborne flight with a known route nearby; leaving the flight unset")
    return ""


def render_mode_frames(
    name: str,
    config,  # type: ignore[no-untyped-def]
    max_frames: int = MAX_FRAMES,
    stride: int = 1,
    fixed_frames: int | None = None,
    pose: str | None = None,
) -> list[Image.Image]:
    """Render frames until the animation visibly repeats, so the loop is seamless."""
    mode = MODE_TYPES[name](config)

    # The network mode is posed, never live. Off-Pi there is no nmcli to read, and
    # the setup screen describes a hotspot this machine is not broadcasting.
    if pose == "setup":
        config.set_network_notice({
            "state": "hotspot",
            "ssid": net.HOTSPOT_SSID,
            "password": "kq7mfp4r",
            "url": "10.42.0.1:8080",
        })
    elif pose == "connected":
        config.set_network_notice(None)
        mode.status = net.Status(state="connected", ssid="Kuok 5G",
                                 ip="192.168.86.247", signal=82, device="wlan0")
        mode._last_refresh = float("inf")  # noqa: SLF001 - freeze; never touch the radio

    # The live news feed carries ~15 headlines, whose combined marquee is far longer
    # than a browser-friendly spritesheet. Trim to a few so the preview loop closes.
    # The Pi itself always scrolls the full set.
    if name == "news":
        mode._refresh()  # noqa: SLF001 - preview-only priming
        if mode.headlines:
            mode.headlines = mode.headlines[:3]
        mode._last_refresh = float("inf")  # noqa: SLF001 - freeze content while sampling

    if name == "costco":
        # Seed with a fake locator result so the preview never sees a 403.
        from ticker.modes.costco import WarehousePrices  # noqa: WPS433

        mode._prices = {  # noqa: SLF001 - preview seed
            "475": WarehousePrices(
                warehouse_id="475", city="SOUTH SAN Francisco",
                location_name="1600 El Camino Real",
                regular="5.30", premium="5.74", diesel="",
                short_name="El Camino",
            ),
            "422": WarehousePrices(
                warehouse_id="422", city="SOUTH SAN Francisco",
                location_name="451 S Airport Blvd",
                regular="5.30", premium="5.85", diesel="",
                short_name="S Airport",
            ),
            "118": WarehousePrices(
                warehouse_id="118", city="SAN LEANDRO",
                location_name="1900 Davis St",
                regular="5.20", premium="5.65", diesel="",
                short_name="San Leandro",
            ),
        }
        mode._last_refresh = float("inf")  # noqa: SLF001 - preview freeze
        mode._last_ids = ("475", "422", "118")
        # Persist the three-warehouse list so ``current_costco_warehouses()``
        # returns all three on render, not just the default single seed.
        config.set_costco_warehouses(("475", "422", "118"))

    if name == "bikes" and pose == "__seed__":
        # Preview host may be offline from GBFS; hand the mode a plausible
        # station rather than let it draw a "Loading..." panel forever.
        from ticker import baywheels

        mode._station = baywheels.Station(  # noqa: SLF001 - preview seed
            station_id="preview",
            name="Market St & 10th St",
            lat=37.775,
            lon=-122.416,
            capacity=23,
            num_bikes_available=11,
            num_ebikes_available=3,
            num_docks_available=12,
            is_renting=True,
            is_installed=True,
            last_reported=0,
        )
        mode._station_id = "preview"
        mode._checked = float("inf")
        mode._missing = False

    frames: list[Image.Image] = []
    seen: dict[str, int] = {}
    limit = fixed_frames if fixed_frames is not None else max_frames

    for index in range(limit):
        tick = index * stride
        canvas = Canvas(config.width, config.height)
        try:
            mode.render(canvas, tick)
        except Exception as exc:  # a mode should never do this, but never abort the preview
            print(f"  ! {name} raised on tick {tick}: {exc}")
            break

        frame = canvas.image_buffer.copy()
        digest = hashlib.sha1(frame.tobytes()).hexdigest()

        if fixed_frames is None and digest in seen and index > 1:
            # Frame repeated: the loop closed. Keep everything before the repeat.
            print(f"  loop closes at {len(frames)} frames")
            return frames

        seen[digest] = index
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
    parser.add_argument("--modes", default=",".join(PREVIEW_SCREENS), help="comma-separated screens")
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
        screen = PREVIEW_SCREENS.get(name)
        if screen is None:
            print(f"skipping unknown screen: {name}")
            continue
        print(f"rendering {name}...")

        screen_config = config
        layout = screen.get("layout")
        if layout:
            screen_config = replace(config, stocks_layout=str(layout))
        if screen.get("pose"):
            # A private state directory, so posing a preview screen never writes a
            # notice into the state a real renderer would read.
            screen_config = replace(
                screen_config, state_dir=Path(tempfile.mkdtemp(prefix="ticker-preview-")))
        if screen.get("seed_bikes"):
            # Give the bikes mode a configured station id via a temp state dir
            # so it does not fall through to the "pick one" empty state.
            screen_config = replace(
                screen_config,
                state_dir=Path(tempfile.mkdtemp(prefix="ticker-preview-")),
                bike_station_id="preview",
            )
        if screen.get("seed_nametag"):
            # A sample name so the frame isn't just the fallback HELLO. The
            # temp state dir keeps this preview seed out of the real state.
            screen_config = replace(
                screen_config,
                state_dir=Path(tempfile.mkdtemp(prefix="ticker-preview-")),
                nametag_name="JOHN",
                nametag_color="#FFFFFF",
            )
        if screen.get("live_flight") and not config.current_flight():
            latitude = float(config.weather_lat or 37.7749)
            longitude = float(config.weather_lon or -122.4194)
            screen_config = replace(screen_config, flight_number=find_live_flight(latitude, longitude))

        pose_arg = screen.get("pose")
        if screen.get("seed_bikes"):
            pose_arg = "__seed__"
        frames = render_mode_frames(
            str(screen["mode"]),
            screen_config,
            stride=int(screen.get("stride", 1)),  # type: ignore[arg-type]
            fixed_frames=screen.get("frames"),  # type: ignore[arg-type]
            pose=pose_arg,  # type: ignore[arg-type]
        )
        if not frames:
            continue
        sheet = build_spritesheet(frames)
        sheet.save(frames_dir / f"{name}.png", optimize=True)
        manifest["modes"][name] = {  # type: ignore[index]
            "frames": len(frames),
            "sheet": f"frames/{name}.png",
            "fps": int(screen.get("fps", config.fps)),  # type: ignore[arg-type]
        }

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nwrote {out_dir}/manifest.json")
    for name, meta in manifest["modes"].items():  # type: ignore[union-attr]
        print(f"  {name:<10} {meta['frames']:>4} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
