# MIT License — Copyright (c) 2026 John Kuok
"""Offline checks for Wi-Fi status, the fallback state machine, and the setup screen.

None of this touches a real radio. nmcli is replaced with a shell script that
prints captured real output, which is the only honest way to test the parsing:
the terse format's colon escaping and the "connecting (getting IP
configuration)" style states are exactly what hand-written fixtures get wrong.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO / "src"))

from ticker import icons, net
from ticker.canvas import SMALL, Canvas
from ticker.config import VALID_MODES, Config
from ticker.modes import MODE_TYPES, build_mode
from ticker.modes.airquality import _luminance
from ticker.modes.network import NetworkMode
from ticker.modes.network import BAR_OFF, GREEN, ICON_WIDTH, TITLE_X, VALUE_X

PASS = 0
FAIL = 0

STATE = Path("/tmp/ticker-wifi-test-state")
FAKE_DIR = Path("/tmp/ticker-fake-nmcli")


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def section(title: str) -> None:
    print(f"\n== {title}")


def make_config(**overrides) -> Config:
    base = dict(
        width=128,
        height=32,
        fps=30,
        timezone="America/Los_Angeles",
        state_dir=STATE,
    )
    base.update(overrides)
    return Config(**base)


def install_fake_nmcli(script: str) -> None:
    """Point ticker.net at a shell script that impersonates nmcli."""
    FAKE_DIR.mkdir(parents=True, exist_ok=True)
    path = FAKE_DIR / "nmcli"
    path.write_text("#!/usr/bin/env bash\n" + script, encoding="utf-8")
    path.chmod(0o755)
    net.NMCLI = str(path)


# Real captured shapes. The device-status line for a hotspot reports the
# connection name, which is how the hotspot state is distinguished from a normal
# connection at all -- there is no separate nmcli field for "I am an AP".
# Branching on the -f field list, not just the subcommand: nmcli prints only the
# fields it was asked for, and a fake that ignores that would let a column-order
# bug pass. The signal lookup asks for two fields, the scan list for four.
CONNECTED = r'''
case "$*" in
  *"device status"*) echo 'wlan0:wifi:connected:Kuok 5G';;
  *"device show wlan0"*) echo 'IP4.ADDRESS[1]:192.168.86.247/24';;
  *"IN-USE,SIGNAL"*) printf '*:82\n:61\n:33\n';;
  *"device wifi list"*) printf '*:Kuok 5G:82:WPA2\n:Neighbour\\:Net:61:WPA2\n:Guest:33:\n:Kuok 5G:70:WPA2\n';;
  *"connection show"*) printf 'Kuok 5G:802-11-wireless\nWired connection 1:802-3-ethernet\nticker-setup:802-11-wireless\n';;
  *) echo '';;
esac
exit 0
'''

OFFLINE = r'''
case "$*" in
  *"device status"*) echo 'wlan0:wifi:disconnected:';;
  *"IN-USE,SIGNAL"*) printf ':44\n';;
  *"device wifi list"*) printf ':Neighbour:44:WPA2\n';;
  *"connection show"*) printf 'Kuok 5G:802-11-wireless\n';;
  *) echo '';;
esac
exit 0
'''

HOTSPOT = r'''
case "$*" in
  *"device status"*) echo 'wlan0:wifi:connected:ticker-setup';;
  *"device show wlan0"*) echo 'IP4.ADDRESS[1]:10.42.0.1/24';;
  *"connection show"*) printf 'ticker-setup:802-11-wireless\n';;
  *) echo '';;
esac
exit 0
'''

CONNECTING = r'''
case "$*" in
  *"device status"*) echo 'wlan0:wifi:connecting (getting IP configuration):Kuok 5G';;
  *) echo '';;
esac
exit 0
'''

NO_RADIO = r'''
case "$*" in
  *"device status"*) echo 'eth0:ethernet:connected:Wired connection 1';;
  *) echo '';;
esac
exit 0
'''

BROKEN = r'''
echo "Error: NetworkManager is not running." >&2
exit 8
'''

section("terse field splitting")
check("plain fields split", net._fields("a:b:c") == ["a", "b", "c"])
# The whole reason for a hand-rolled splitter: an SSID or a MAC containing a
# colon arrives escaped, and str.split would shred it.
check("escaped colon survives", net._fields(r"a:Neighbour\:Net:c") == ["a", "Neighbour:Net", "c"])
check("empty trailing field kept", net._fields("wlan0:wifi:disconnected:") == ["wlan0", "wifi", "disconnected", ""])
check("no separator yields one field", net._fields("solo") == ["solo"])
check("escaped backslash", net._fields(r"a\\:b") == ["a\\", "b"])

section("status parsing")
install_fake_nmcli(CONNECTED)
status = net.status()
check("connected state", status.state == "connected", status.state)
check("ssid read", status.ssid == "Kuok 5G", status.ssid)
check("ipv4 without prefix", status.ip == "192.168.86.247", status.ip)
check("signal from the in-use row", status.signal == 82, str(status.signal))
check("device name", status.device == "wlan0", status.device)
check("online property", status.online)
check("does not want setup", not status.needs_setup)

install_fake_nmcli(CONNECTING)
status = net.status()
# nmcli appends a parenthesised phase, so matching the whole string would leave
# this in "unknown" and the panel would claim it cannot reach nmcli.
check("connecting with a phase suffix", net.status().state == "connecting", status.state)

install_fake_nmcli(OFFLINE)
status = net.status()
check("offline state", status.state == "offline", status.state)
check("offline reports no ssid", status.ssid == "", status.ssid)
check("offline wants setup", status.needs_setup)

install_fake_nmcli(HOTSPOT)
status = net.status()
check("hotspot recognised by profile name", status.state == "hotspot", status.state)
check("hotspot address read", status.ip == "10.42.0.1", status.ip)
check("hotspot is not online", not status.online)

install_fake_nmcli(NO_RADIO)
check("no wifi device", net.status().state == "unavailable", net.status().state)

install_fake_nmcli(BROKEN)
status = net.status()
check("nmcli failure is unknown, not offline", status.state == "unknown", status.state)
# This distinction carries weight: "unknown" must never trigger the hotspot,
# because a tool this module cannot read might be sitting on a working link.
check("unknown never raises a hotspot",
      net.next_action("unknown", 99, None, 0.0) == "wait")

net.NMCLI = "/nonexistent/nmcli-does-not-exist"
check("missing binary is unknown", net.status().state == "unknown")
check("available() false when missing", not net.available())

section("scan list")
install_fake_nmcli(CONNECTED)
found = net.scan()
names = [n.ssid for n in found]
check("in-use network sorts first", names[0] == "Kuok 5G", str(names))
check("duplicate ssid collapsed", names.count("Kuok 5G") == 1, str(names))
check("strongest duplicate wins", found[0].signal == 82, str(found[0].signal))
check("escaped ssid recovered", "Neighbour:Net" in names, str(names))
check("own hotspot hidden from the list", net.HOTSPOT_SSID not in names, str(names))
check("saved flag set", found[0].saved)
check("secured network locked", found[0].locked)
open_network = [n for n in found if n.ssid == "Guest"][0]
check("open network not locked", not open_network.locked)
check("hotspot profile excluded from saved", net.HOTSPOT_CONNECTION not in net.saved_networks())
check("saved list excludes ethernet", net.saved_networks() == ["Kuok 5G"], str(net.saved_networks()))

section("signal bars")
for signal, expected in ((0, 0), (10, 1), (34, 1), (35, 2), (54, 2), (55, 3), (74, 3), (75, 4), (100, 4)):
    got = net.Network(ssid="x", signal=signal).bars
    check(f"{signal}% -> {expected} bars", got == expected, str(got))

section("join validation")
install_fake_nmcli(CONNECTED)
ok, message = net.join("")
check("blank ssid refused", not ok and "no network name" in message, message)
ok, message = net.join("Somewhere", "short")
# Refused locally rather than by the router, which fails slowly and blames the
# password only after a long association timeout.
check("short password refused before dialling", not ok and "8 characters" in message, message)
ok, message = net.join("Kuok 5G")
check("saved network joins without a password", ok, message)

FAILURES = {
    "Error: Connection activation failed: (7) Secrets were required, but not provided.":
        "rejected the password",
    "Error: No network with SSID 'Nope' found.": "not in range",
    "sudo: a password is required": "sudoers rule for nmcli is missing",
    "Error: Timeout expired (45 seconds)": "Timed out",
}
for raw, expected in FAILURES.items():
    check(f"explains: {expected}", expected in net._explain(False, raw, "Nope"), net._explain(False, raw, "Nope"))
check("success message names the network", net._explain(True, "", "Kuok 5G") == "Joined Kuok 5G")

check("refuses to forget the setup profile",
      not net.forget(net.HOTSPOT_CONNECTION)[0])
check("refuses to forget nothing", not net.forget("  ")[0])

section("hotspot password")
shutil.rmtree(STATE, ignore_errors=True)
os.environ.pop("WIFI_SETUP_PASSWORD", None)
first = net.hotspot_password(STATE)
check("password long enough for WPA2", len(first) >= 8, first)
# Stability is the whole point: it gets read off the panel and typed on a phone,
# and a value that rotated would mean re-reading the panel on every trip.
check("password is stable across calls", net.hotspot_password(STATE) == first)
check("password file is not world readable",
      oct((STATE / "hotspot_password").stat().st_mode)[-3:] == "600",
      oct((STATE / "hotspot_password").stat().st_mode))
# Every one of these pairs is a documented misread on a 5x8 pixel font, and this
# password exists to be transcribed from one.
for bad in "01l5S8BOI":
    check(f"alphabet excludes ambiguous {bad!r}", bad not in net._PASSWORD_ALPHABET)
os.environ["WIFI_SETUP_PASSWORD"] = "override-me"
check("explicit override wins", net.hotspot_password(STATE) == "override-me")
os.environ["WIFI_SETUP_PASSWORD"] = "tiny"
check("too-short override ignored", net.hotspot_password(STATE) == first)
os.environ.pop("WIFI_SETUP_PASSWORD")

section("fallback state machine")
G = net.GRACE_CHECKS
check("connected clears nothing when no hotspot", net.next_action("connected", 0, None, 0.0) == "wait")
check("connected withdraws a live hotspot", net.next_action("connected", 0, 5.0, 99.0) == "clear_hotspot")
check("connecting also withdraws", net.next_action("connecting", 0, 5.0, 99.0) == "clear_hotspot")
# The grace period is the part that matters: a Pi that just booted, or whose
# router just rebooted, is legitimately disconnected for a few seconds, and an
# access point raised in that window takes it off a network it was about to join.
for misses in range(G):
    check(f"offline miss {misses} waits out the grace period",
          net.next_action("offline", misses, None, 0.0) == "wait")
check("offline raises after the grace period",
      net.next_action("offline", G, None, 0.0) == "raise_hotspot")
check("radio off also raises", net.next_action("unavailable", G, None, 0.0) == "raise_hotspot")
check("fresh hotspot waits", net.next_action("hotspot", G, 100.0, 100.0) == "wait")
check("hotspot below the retry window waits",
      net.next_action("hotspot", G, 100.0, 100.0 + net.RETRY_AFTER - 1) == "wait")
check("hotspot retries after the window",
      net.next_action("hotspot", G, 100.0, 100.0 + net.RETRY_AFTER) == "retry_known")
check("adopted hotspot waits one round",
      net.next_action("hotspot", G, None, 1e6) == "wait")

section("notice file")
shutil.rmtree(STATE, ignore_errors=True)
config = make_config()
check("absent notice is empty", config.network_notice() == {})
config.set_network_notice({"state": "hotspot", "ssid": "TICKER-SETUP", "password": "abc", "url": "10.42.0.1:8080"})
check("notice round-trips", config.network_notice()["ssid"] == "TICKER-SETUP")
config.network_notice_file.write_text("{not json", encoding="utf-8")
# A half-written file must not take the renderer down, and this one is read from
# the render loop every second.
check("corrupt notice reads as empty", config.network_notice() == {})
config.network_notice_file.write_text('["a list"]', encoding="utf-8")
check("non-object notice reads as empty", config.network_notice() == {})
config.set_network_notice(None)
check("clearing removes the file", not config.network_notice_file.exists())
config.set_network_notice(None)
check("clearing twice is harmless", config.network_notice() == {})

section("mode registration")
check("net is a valid mode", "net" in VALID_MODES)
check("net is in the registry", MODE_TYPES.get("net") is NetworkMode)
check("build_mode returns it", isinstance(build_mode("net", make_config()), NetworkMode))

section("wifi icon")
check("icon is 8 wide", ICON_WIDTH == 8, str(ICON_WIDTH))
check("icon is 7 rows", len(icons.WIFI) == 7, str(len(icons.WIFI)))
check("icon rows are all 8 wide", all(len(row) == 8 for row in icons.WIFI))
check("icon glyphs are all in the palette",
      set("".join(icons.WIFI)) <= set(icons.WIFI_PALETTE) | {"."})
# Same box as the train, so the network header and the BART header share geometry
# and the title column does not shift when the mode changes.
check("icon matches the train's box",
      (len(icons.WIFI), len(icons.WIFI[0])) == (len(icons.TRAIN), len(icons.TRAIN[0])))
widths = [row.count("W") for row in icons.WIFI[:5]]
check("chevrons narrow going inward", widths[0] > widths[2] > widths[4], str(widths))
check("dot is detached from the chevrons", icons.WIFI[5].strip(".") == "", icons.WIFI[5])
check("dot present on the last row", "W" in icons.WIFI[6])
check("title clears the icon", TITLE_X >= ICON_WIDTH, f"{TITLE_X} vs {ICON_WIDTH}")


def lit(canvas: Canvas) -> list[tuple[int, int]]:
    pixels = canvas.image_buffer.load()
    return [(x, y) for y in range(canvas.height) for x in range(canvas.width) if pixels[x, y] != (0, 0, 0)]


def render(mode: NetworkMode, tick: int = 0) -> Canvas:
    canvas = Canvas(128, 32)
    mode.render(canvas, tick)
    return canvas


def posed(state: str, **fields) -> NetworkMode:
    """A mode with its status frozen, so no nmcli call happens during a render."""
    mode = NetworkMode(make_config())
    mode.status = net.Status(state=state, **fields)
    mode._last_refresh = 1e18  # never refresh
    return mode


section("setup screen")
shutil.rmtree(STATE, ignore_errors=True)
config = make_config()
config.set_network_notice({
    "state": "hotspot", "ssid": "TICKER-SETUP",
    "password": "kq7mfp4r", "url": "10.42.0.1:8080",
})
mode = NetworkMode(config)
# Deliberately poisoned: the setup screen must come entirely from the notice, so
# that it works while the radio is busy being an access point.
net.NMCLI = "/nonexistent/nmcli-does-not-exist"
canvas = render(mode)
text = canvas.image_buffer
check("setup screen drew something", len(lit(canvas)) > 100, str(len(lit(canvas))))


def row_has_content(canvas: Canvas, y0: int, y1: int) -> bool:
    return any(y0 <= y < y1 for _, y in lit(canvas))


for label, (y0, y1) in {"header": (0, 8), "ssid": (8, 16), "key": (16, 24), "url": (24, 32)}.items():
    check(f"setup {label} row is drawn", row_has_content(canvas, y0, y1))

# The panel is the only channel left when the hotspot is up, so nothing on this
# screen may be clipped: a truncated password is unusable.
xs = [x for x, _ in lit(canvas)]
check("setup screen stays inside the panel", max(xs) < 128 and min(xs) >= 0, f"{min(xs)}..{max(xs)}")
canvas_pixels = canvas.image_buffer.load()


def text_width_at(text: str, font: int = SMALL) -> int:
    probe = Canvas(128, 32)
    return probe.text_width(text, font)


check("password fits beside its label",
      VALUE_X + text_width_at("kq7mfp4r") <= 128,
      str(VALUE_X + text_width_at("kq7mfp4r")))
check("hotspot url fits beside its label",
      VALUE_X + text_width_at("10.42.0.1:8080") <= 128,
      str(VALUE_X + text_width_at("10.42.0.1:8080")))
# The widest address NetworkManager's shared mode could realistically hand out.
check("a full dotted quad with a port still fits",
      VALUE_X + text_width_at("192.168.100.100:8080") <= 128,
      str(VALUE_X + text_width_at("192.168.100.100:8080")))
check("the setup ssid fits beside its label",
      VALUE_X + text_width_at(net.HOTSPOT_SSID) <= 128)

# An empty notice must fall back rather than draw blank rows, and a notice that
# lost its password must still name the network so the user knows what they are
# looking at.
config.set_network_notice({"state": "hotspot"})
fallback = render(NetworkMode(config))
check("notice missing fields still draws rows", len(lit(fallback)) > 80, str(len(lit(fallback))))

section("connected screen")
config.set_network_notice(None)
mode = posed("connected", ssid="Kuok 5G", ip="192.168.86.247", signal=82, device="wlan0")
canvas = render(mode)
xs = [x for x, _ in lit(canvas)]
check("connected screen inside the panel", max(xs) < 128, str(max(xs)))
check("address row is drawn", row_has_content(canvas, 10, 22))
check("ticker.local hint is drawn", row_has_content(canvas, 24, 32))
# Bars live in the bottom-right corner, which is the only space the address row
# leaves; a lit pixel there proves they were not pushed off the panel.
pixels = canvas.image_buffer.load()
corner = [pixels[x, y] for x in range(112, 128) for y in range(24, 32)]
check("signal bars drawn in the corner", any(c == GREEN for c in corner))
# Checked on a weak signal: a full-strength reading has no unlit bars to draw.
weak_corner = [render(posed("connected", ssid="Far", ip="10.0.0.9", signal=20)).image_buffer.load()[x, y]
               for x in range(112, 128) for y in range(24, 32)]
check("unlit bars present as a reference", any(c == BAR_OFF for c in weak_corner))
check("bars do not touch the right edge",
      all(pixels[127, y] == (0, 0, 0) for y in range(24, 32)))

weak = posed("connected", ssid="Far", ip="10.0.0.9", signal=20)
weak_pixels = render(weak).image_buffer.load()
weak_lit = sum(1 for x in range(112, 128) for y in range(24, 32) if weak_pixels[x, y] == GREEN)
strong_lit = sum(1 for x in range(112, 128) for y in range(24, 32) if pixels[x, y] == GREEN)
check("weaker signal lights fewer bars", weak_lit < strong_lit, f"{weak_lit} vs {strong_lit}")

# Verified at the 20% night step, where a dim reference colour is the first thing
# to disappear -- and an unlit bar that vanishes turns a 2-bar reading into an
# apparent full-strength one.
check("unlit bars still visible at 20% brightness",
      _luminance(tuple(c * 0.20 for c in BAR_OFF)) > 3,
      f"{_luminance(tuple(c * 0.20 for c in BAR_OFF)):.1f}")
check("lit bars clearly brighter than unlit",
      _luminance(GREEN) > _luminance(BAR_OFF) * 3,
      f"{_luminance(GREEN):.1f} vs {_luminance(BAR_OFF):.1f}")

check("no address still draws a line",
      row_has_content(render(posed("connected", ssid="X", ip="", signal=50)), 10, 22))

section("waiting screens")
for state in ("connecting", "offline", "unavailable", "hotspot", "unknown"):
    canvas = render(posed(state, ssid="Kuok 5G"))
    drawn = lit(canvas)
    check(f"{state} draws a headline", len(drawn) > 40, str(len(drawn)))
    check(f"{state} stays inside the panel", max(x for x, _ in drawn) < 128)

section("no mode raises")
# The renderer's contract: a mode never raises on network failure, because a
# traceback in the render loop replaces the panel with an error string.
net.NMCLI = "/nonexistent/nmcli-does-not-exist"
shutil.rmtree(STATE, ignore_errors=True)
for state in ("connected", "connecting", "offline", "unavailable", "hotspot", "unknown"):
    try:
        render(posed(state))
        check(f"{state} renders without raising", True)
    except Exception as error:  # noqa: BLE001
        check(f"{state} renders without raising", False, repr(error))

try:
    # The live path, with nmcli absent: the background sweep must also swallow it.
    live = NetworkMode(make_config())
    render(live)
    render(live, tick=60)
    check("live mode renders with nmcli missing", True)
except Exception as error:  # noqa: BLE001
    check("live mode renders with nmcli missing", False, repr(error))

section("renderer override")
source = (REPO / "src/ticker/renderer.py").read_text(encoding="utf-8")
check("renderer consults the notice", "network_notice()" in source)
check("renderer forces the net mode", '"net" if notice else config.current_mode()' in source)
# The selection on disk must be left alone, so the panel returns to whatever was
# chosen once the network is back.
check("renderer does not write the mode file", "set_mode" not in source)

# The night schedule bottoms out at 8%, which is below the point where an
# eight-character password can be read off the panel -- and while the hotspot is
# up the panel is the only place that password exists.
from ticker.renderer import SETUP_BRIGHTNESS_FLOOR  # noqa: E402

check("setup floor is above the dimmest schedule step", SETUP_BRIGHTNESS_FLOOR >= 0.35,
      str(SETUP_BRIGHTNESS_FLOOR))
check("setup floor does not force full brightness", SETUP_BRIGHTNESS_FLOOR <= 0.6,
      str(SETUP_BRIGHTNESS_FLOOR))
check("renderer applies the floor", source.count("SETUP_BRIGHTNESS_FLOOR") >= 3)
# max(), not assignment: a user who has deliberately turned the panel up must not
# be turned back down by the setup screen.
check("floor raises rather than overrides",
      "max(target_brightness, SETUP_BRIGHTNESS_FLOOR)" in source)
check("floor also applied before the loop starts",
      source.index("SETUP_BRIGHTNESS_FLOOR", source.index("brightness = target_brightness") - 200)
      < source.index("while True"))
# Amber on black at the floor: the password is the smallest, most transcription-
# sensitive text on the panel, so it is the thing that decided the floor.
check("password legible at the setup floor",
      _luminance(tuple(c * SETUP_BRIGHTNESS_FLOOR for c in (255, 190, 60))) > 60,
      f"{_luminance(tuple(c * SETUP_BRIGHTNESS_FLOOR for c in (255, 190, 60))):.1f}")

section("daemon decisions")
from ticker import wifid  # noqa: E402 - imported here so a failure is attributed to this section

shutil.rmtree(STATE, ignore_errors=True)
daemon = wifid.Daemon(make_config())
install_fake_nmcli(CONNECTED)
check("connected tick does nothing", daemon.tick() == "wait")
check("connected tick clears misses", daemon.misses == 0)

install_fake_nmcli(OFFLINE)
actions = [daemon.tick() for _ in range(net.GRACE_CHECKS)]
check("offline waits out the grace period", actions[:-1] == ["wait"] * (net.GRACE_CHECKS - 1), str(actions))
check("offline counts misses", daemon.misses == net.GRACE_CHECKS, str(daemon.misses))

install_fake_nmcli(HOTSPOT)
daemon = wifid.Daemon(make_config())
action = daemon.tick()
check("a hotspot raised by someone else is adopted", daemon.hotspot_since is not None, action)
check("adopting publishes a notice", daemon.config.network_notice().get("state") == "hotspot")
notice = daemon.config.network_notice()
check("notice carries the observed address", notice.get("url") == "10.42.0.1:8080", str(notice))
check("notice carries the ssid", notice.get("ssid") == net.HOTSPOT_SSID, str(notice))
check("notice carries a usable password", len(notice.get("password", "")) >= 8, str(notice))

install_fake_nmcli(CONNECTED)
check("reconnecting withdraws the hotspot", daemon.tick() == "clear_hotspot")
check("withdrawal clears the notice", daemon.config.network_notice() == {})
check("withdrawal resets the hotspot timer", daemon.hotspot_since is None)

install_fake_nmcli(BROKEN)
daemon = wifid.Daemon(make_config())
check("broken nmcli does nothing", daemon.tick() == "wait")
check("broken nmcli publishes no notice", daemon.config.network_notice() == {})

section("web routes")
os.environ["TICKER_NMCLI"] = str(FAKE_DIR / "nmcli")
install_fake_nmcli(CONNECTED)
net.NMCLI = str(FAKE_DIR / "nmcli")
from ticker.web.app import create_app  # noqa: E402

app = create_app()
app.config.update(TESTING=True)
client = app.test_client()

page = client.get("/wifi")
check("wifi page renders", page.status_code == 200, str(page.status_code))
body = page.get_data(as_text=True)
check("page names the setup network", net.HOTSPOT_SSID in body)
check("page warns about losing the connection", "stop responding" in body)
check("page links back to the panel", 'href="/"' in body)

state = client.get("/api/wifi").get_json()
check("state endpoint reports connected", state["state"] == "connected", str(state))
check("state endpoint lists saved networks", state["saved"] == ["Kuok 5G"], str(state["saved"]))
# Scanning is several seconds of radio time, so a polling page must not do it by
# default.
check("state endpoint does not scan by default", "networks" not in state)
scanned = client.get("/api/wifi?scan=1").get_json()
check("scan on request", len(scanned["networks"]) == 3, str(scanned.get("networks")))
check("scan marks the active network", scanned["networks"][0]["active"])
check("scan reports bars", scanned["networks"][0]["bars"] == 4)

joined = client.post("/wifi/join", json={"ssid": "Kuok 5G"})
check("join accepts a saved network", joined.status_code == 200, str(joined.status_code))
bad = client.post("/wifi/join", json={"ssid": ""})
check("join rejects a blank name with 400", bad.status_code == 400)
check("join error is a sentence", "no network name" in bad.get_json()["message"])
short = client.post("/wifi/join", json={"ssid": "X", "password": "abc"})
check("join rejects a short password with 400", short.status_code == 400)

forgot = client.post("/wifi/forget", json={"ssid": net.HOTSPOT_CONNECTION})
check("forget refuses the setup profile", forgot.status_code == 400, str(forgot.status_code))

index = client.get("/")
check("control panel renders with the new mode", index.status_code == 200)
index_body = index.get_data(as_text=True)
check("control panel links to wifi", 'href="/wifi"' in index_body)
check("control panel labels the mode Wi-Fi", "Wi-Fi" in index_body)
# .title() on "net" would give "Net"; the label map exists to stop that, the same
# way it stops BART becoming "Bart".
check("mode button is not labelled 'net'", ">\n        net\n      <" not in index_body)

status_payload = client.get("/api/status").get_json()
check("status exposes the notice", "network_notice" in status_payload)
check("status reports no notice when connected", status_payload["network_notice"] == {},
      str(status_payload["network_notice"]))

# Caught in a browser, not by reading the code: .netbanner sets display:block at
# the same specificity as .hidden, so the later rule won and the "no Wi-Fi" banner
# showed permanently -- on a ticker that was online.
css = (REPO / "src/ticker/web/static/style.css").read_text(encoding="utf-8")
check("the hidden utility cannot be overridden by a component",
      ".hidden { display: none !important; }" in css)
for selector in (".netbanner", ".netstatus", ".netlist", ".netrow", ".netbars", ".netname",
                 ".nettags", ".forget", ".joinform", ".checkline", ".wide", ".backlink",
                 ".warn", ".netstate", ".mode-net", ".wifilink"):
    check(f"stylesheet defines {selector}", selector in css)

section("service unit and sudoers")
unit = (REPO / "systemd/ticker-wifi.service").read_text(encoding="utf-8")
# Waiting for the network to come up would deadlock precisely when the daemon is
# needed, since its whole job is reacting to a network that never arrives.
check("unit does not wait for the network",
      "After=network-online.target" not in unit and "Wants=network-online.target" not in unit)
check("unit requires NetworkManager", "Requires=NetworkManager.service" in unit)
check("unit restarts always", "Restart=always" in unit)
check("unit runs as root", "User=root" in unit)

sudoers = (REPO / "systemd/ticker-nmcli.sudoers").read_text(encoding="utf-8")
check("sudoers grants pi only", sudoers.strip().endswith("pi ALL=(root) NOPASSWD: TICKER_WIFI"))
check("sudoers uses an absolute nmcli path", "/usr/bin/nmcli" in sudoers)
# A bare "nmcli" or an unrestricted wildcard would make this rule equivalent to
# passwordless root.
check("sudoers does not grant a bare wildcard", "NOPASSWD: ALL" not in sudoers)
for subcommand in ("device wifi connect", "device wifi hotspot", "connection up id",
                   "connection down id", "connection delete id"):
    check(f"sudoers covers: {subcommand}", subcommand in sudoers)

installer = (REPO / "scripts/install.sh").read_text(encoding="utf-8")
check("installer validates sudoers before installing", "visudo -c" in installer)
check("installer installs sudoers read-only", "-m 0440" in installer)
check("installer skips wifi without NetworkManager", "NetworkManager not detected" in installer)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
