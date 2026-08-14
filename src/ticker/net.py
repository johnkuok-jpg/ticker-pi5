# MIT License — Copyright (c) 2026 John Kuok
"""Wi-Fi state and control, via NetworkManager's nmcli.

Everything here is a thin, defensive wrapper around shelling out to nmcli. Two
rules apply throughout:

* Nothing raises. This module is called from the render loop's mode and from a
  background daemon, and a missing binary, a timeout or an nmcli version that
  prints something unexpected must all degrade to "unknown" rather than take the
  panel down. Every call goes through :func:`_run`, which swallows the lot.
* Nothing is cached. Wi-Fi state is exactly the thing that changes underneath
  you, and a stale "connected" would be worse than a slow answer. Callers that
  need to poll cheaply should poll the notice file the daemon writes, not this.

nmcli's terse mode (``-t``) is used for every read, because the human-readable
tables are aligned with padding that changes between versions. Terse output is
colon-separated with literal colons backslash-escaped, which is why the fields
are split by hand rather than with ``str.split``.

The Pi has a single radio, and a single radio cannot reliably scan for networks
while it is itself acting as an access point -- the comitup FAQ documents the
same limitation and recommends a second adapter to avoid it:
https://github-wiki-see.page/m/davesteele/comitup/wiki/FAQ
Rather than add hardware, the fallback daemon drops the hotspot periodically to
look around; see :func:`next_action`.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Resolved to an absolute path, because the privileged calls go through sudo and
# the sudoers rule names /usr/bin/nmcli: handing sudo a bare name would depend on
# sudo's PATH resolution matching the rule. Overridable so tests can point at a
# script that fakes nmcli, and so a distro that installs it elsewhere works
# without editing code.
NMCLI = os.environ.get("TICKER_NMCLI") or shutil.which("nmcli") or "nmcli"

# The connection profile, and the network name a phone will see. Kept distinct:
# the profile name is what nmcli is addressed by, and renaming the broadcast SSID
# must not orphan the profile the daemon brings up and down.
HOTSPOT_CONNECTION = "ticker-setup"
HOTSPOT_SSID = os.environ.get("WIFI_SETUP_SSID", "TICKER-SETUP")

# WPA2 refuses anything shorter than 8 characters, and the password has to be
# read off a 128x32 panel and typed on a phone, so it is generated from an
# alphabet with no character pairs that are ambiguous in a 5x8 pixel font: no
# 0/O, no 1/l/I, no 5/S, no 8/B.
_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz234679"
_PASSWORD_LENGTH = 8

_TIMEOUT = 10.0
_CONNECT_TIMEOUT = 45.0  # association plus DHCP, on a slow router


@dataclass(frozen=True, slots=True)
class Network:
    """One Wi-Fi network as nmcli sees it."""

    ssid: str
    signal: int = 0
    security: str = ""
    saved: bool = False
    active: bool = False

    @property
    def locked(self) -> bool:
        """Whether a password is needed. nmcli leaves this field empty when open."""
        return bool(self.security.strip()) and self.security.strip() != "--"

    @property
    def bars(self) -> int:
        """Signal as 0-4 bars, for drawing on the panel."""
        if self.signal >= 75:
            return 4
        if self.signal >= 55:
            return 3
        if self.signal >= 35:
            return 2
        if self.signal > 0:
            return 1
        return 0


@dataclass(frozen=True, slots=True)
class Status:
    """What the Wi-Fi radio is doing right now.

    ``state`` is deliberately a small closed vocabulary rather than nmcli's own
    strings, which vary by version and include parenthesised detail:

    * ``connected``    -- on a normal network, has an address
    * ``connecting``   -- associating or waiting on DHCP
    * ``hotspot``      -- running our own access point, no upstream network
    * ``offline``      -- radio is up, joined nothing
    * ``unavailable``  -- radio is off, or there is no Wi-Fi device
    * ``unknown``      -- nmcli could not be reached or could not be parsed
    """

    state: str = "unknown"
    ssid: str = ""
    ip: str = ""
    signal: int = 0
    device: str = ""

    @property
    def online(self) -> bool:
        return self.state == "connected" and bool(self.ip)

    @property
    def needs_setup(self) -> bool:
        """Whether a human has to intervene to get this thing on a network."""
        return self.state in ("hotspot", "offline", "unavailable")


def available() -> bool:
    """Whether nmcli is installed at all."""
    return shutil.which(NMCLI) is not None


def _run(args: list[str], timeout: float = _TIMEOUT, root: bool = False) -> tuple[bool, str]:
    """Run nmcli and return (succeeded, combined output).

    ``root`` requests privilege for the calls that change state. The web app runs
    as an unprivileged user, so those go through ``sudo -n``: non-interactive, so
    a missing sudoers rule fails immediately with a readable error instead of
    hanging a request on a password prompt that nobody can answer.
    """
    command = [NMCLI, *args]
    if root and os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, arguments are not shell-parsed
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, f"{NMCLI} is not installed"
    except subprocess.TimeoutExpired:
        return False, f"{NMCLI} {' '.join(args[:2])} timed out"
    except OSError as error:  # permissions, exec format, anything else the OS objects to
        return False, str(error)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output


def _fields(line: str) -> list[str]:
    """Split one line of nmcli terse output.

    Terse mode separates fields with ``:`` and escapes literal colons in values
    as ``\\:``, which matters constantly here: MAC addresses, IPv6 addresses and
    SSIDs containing a colon all round-trip through this.
    """
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def _first_wifi_device() -> tuple[bool, str, str, str]:
    """Return (nmcli answered, device, nmcli state, connection name) for the radio.

    The first element is not redundant with an empty device name. "nmcli did not
    answer" and "this box has no Wi-Fi" have to stay distinguishable, because the
    fallback daemon raises an access point on the second and must not on the
    first: a NetworkManager that is merely unreadable may well be sitting on a
    working connection, and tearing that down would strand the ticker.
    """
    ok, output = _run(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    if not ok:
        return False, "", "", ""
    for line in output.splitlines():
        parts = _fields(line)
        if len(parts) >= 4 and parts[1] == "wifi":
            return True, parts[0], parts[2], parts[3]
    return True, "", "", ""


def _address(device: str) -> str:
    """First IPv4 address on a device, without its prefix length."""
    if not device:
        return ""
    ok, output = _run(["-t", "-f", "IP4.ADDRESS", "device", "show", device])
    if not ok:
        return ""
    for line in output.splitlines():
        _, _, value = line.partition(":")
        address = value.strip().split("/")[0]
        if address:
            return address
    return ""


def _active_signal() -> int:
    """Signal strength of the network currently in use, 0-100."""
    ok, output = _run(["-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list", "--rescan", "no"])
    if not ok:
        return 0
    for line in output.splitlines():
        parts = _fields(line)
        if parts and parts[0].strip() == "*" and len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                return 0
    return 0


def status() -> Status:
    """Read the current Wi-Fi state. Never raises, never caches."""
    if not available():
        return Status(state="unknown")
    answered, device, raw_state, connection = _first_wifi_device()
    if not answered:
        return Status(state="unknown")
    if not device:
        return Status(state="unavailable")

    # nmcli says things like "connecting (getting IP configuration)", so match on
    # the leading word rather than the whole string.
    head = raw_state.split()[0].lower() if raw_state else ""
    if connection == HOTSPOT_CONNECTION:
        state = "hotspot"
    elif head == "connected":
        state = "connected"
    elif head in ("connecting", "config", "ip-config", "prepare"):
        state = "connecting"
    elif head in ("unavailable", "unmanaged", "asleep"):
        state = "unavailable"
    elif head == "disconnected":
        state = "offline"
    else:
        state = "unknown"

    return Status(
        state=state,
        ssid=connection if state in ("connected", "connecting") else "",
        ip=_address(device) if state in ("connected", "hotspot") else "",
        signal=_active_signal() if state == "connected" else 0,
        device=device,
    )


def saved_networks() -> list[str]:
    """Names of stored Wi-Fi profiles, excluding our own hotspot."""
    ok, output = _run(["-t", "-f", "NAME,TYPE", "connection", "show"])
    if not ok:
        return []
    names = []
    for line in output.splitlines():
        parts = _fields(line)
        if len(parts) >= 2 and parts[1] == "802-11-wireless" and parts[0] != HOTSPOT_CONNECTION:
            names.append(parts[0])
    return names


def scan(rescan: bool = True) -> list[Network]:
    """List visible networks, strongest first, deduplicated by name.

    A mesh or an extender puts the same SSID on air several times, and a phone
    shows that as one entry, so the strongest sighting of each name wins. Hidden
    networks appear with an empty SSID and are dropped: they cannot be joined by
    tapping a list, and the join form takes a typed name for that case.
    """
    def _list(force: bool) -> tuple[bool, str]:
        return _run(
            ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
             "--rescan", "yes" if force else "no"],
            timeout=_TIMEOUT + 10,  # a rescan takes a few seconds on top of the call
        )

    ok, output = _list(rescan)
    if not ok and rescan:
        # Requesting a fresh scan is a privileged operation, and the web app is
        # not privileged, so this legitimately fails as an unprivileged caller on
        # some polkit configurations. NetworkManager's own cached list is usually
        # seconds old, and a slightly stale list beats an empty one.
        ok, output = _list(False)
    if not ok:
        return []
    stored = set(saved_networks())
    best: dict[str, Network] = {}
    for line in output.splitlines():
        parts = _fields(line)
        if len(parts) < 4:
            continue
        in_use, ssid, signal_text, security = parts[0], parts[1], parts[2], parts[3]
        if not ssid or ssid == HOTSPOT_SSID:
            continue
        try:
            signal = int(signal_text)
        except ValueError:
            signal = 0
        found = Network(
            ssid=ssid,
            signal=signal,
            security=security,
            saved=ssid in stored,
            active=in_use.strip() == "*",
        )
        previous = best.get(ssid)
        if previous is None or found.signal > previous.signal:
            best[ssid] = found
    # Whatever is in use sorts first even if a neighbour is momentarily stronger,
    # so the list does not reorder under the user's thumb while they read it.
    return sorted(best.values(), key=lambda n: (not n.active, -n.signal, n.ssid.lower()))


def join(ssid: str, password: str = "", hidden: bool = False) -> tuple[bool, str]:
    """Join a network, saving it for next time. Returns (succeeded, message)."""
    ssid = ssid.strip()
    if not ssid:
        return False, "no network name given"
    if password and len(password) < 8:
        # Rejected here rather than by the router, which would fail slowly and
        # blame the password only after a long association timeout.
        return False, "Wi-Fi passwords are at least 8 characters"

    if ssid in saved_networks() and not password:
        ok, output = _run(["connection", "up", "id", ssid], timeout=_CONNECT_TIMEOUT, root=True)
        return ok, _explain(ok, output, ssid)

    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    if hidden:
        args += ["hidden", "yes"]
    ok, output = _run(args, timeout=_CONNECT_TIMEOUT, root=True)
    if ok:
        # A network the user chose by hand should win over anything already
        # stored, including a hotspot left at default priority.
        _run(["connection", "modify", ssid, "connection.autoconnect-priority", "10"], root=True)
    return ok, _explain(ok, output, ssid)


def _explain(ok: bool, output: str, ssid: str) -> str:
    """Turn nmcli's output into one line worth showing a person."""
    if ok:
        return f"Joined {ssid}"
    lowered = output.lower()
    if "sudo" in lowered and ("password" in lowered or "no tty" in lowered):
        return ("Not allowed to change networks: the sudoers rule for nmcli is missing. "
                "See the Wi-Fi section of the README.")
    if "secrets were required" in lowered or "no secrets" in lowered:
        return f"{ssid} rejected the password"
    if "not found" in lowered or "no network with ssid" in lowered:
        return f"{ssid} is not in range"
    if "timed out" in lowered or "timeout" in lowered:
        return f"Timed out joining {ssid}"
    last = output.strip().splitlines()[-1] if output.strip() else "unknown error"
    return f"Could not join {ssid}: {last}"


def forget(ssid: str) -> tuple[bool, str]:
    """Delete a stored network so it is no longer joined automatically."""
    if not ssid.strip():
        return False, "no network name given"
    if ssid == HOTSPOT_CONNECTION:
        return False, "that is the ticker's own setup network"
    ok, output = _run(["connection", "delete", "id", ssid], root=True)
    return ok, f"Forgot {ssid}" if ok else f"Could not forget {ssid}: {output.splitlines()[-1] if output else ''}"


def hotspot_password(state_dir: Path) -> str:
    """The setup hotspot's password, generated once and then kept.

    Kept stable on purpose: it gets printed on the panel and typed on a phone,
    and rotating it every time the hotspot comes up would mean re-reading the
    panel on every trip. An operator can override it outright with
    WIFI_SETUP_PASSWORD.
    """
    override = os.environ.get("WIFI_SETUP_PASSWORD", "").strip()
    if len(override) >= 8:
        return override
    path = state_dir / "hotspot_password"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 8:
            return existing
    except OSError:
        pass
    password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_LENGTH))
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{password}\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass  # an unwritable state dir is not a reason to refuse to run an AP
    return password


def hotspot_up(password: str, ssid: str = HOTSPOT_SSID) -> tuple[bool, str]:
    """Raise the setup access point on the Wi-Fi radio."""
    _, device, _, _ = _first_wifi_device()
    args = ["device", "wifi", "hotspot", "con-name", HOTSPOT_CONNECTION, "ssid", ssid,
            "password", password]
    if device:
        args += ["ifname", device]
    ok, output = _run(args, timeout=_CONNECT_TIMEOUT, root=True)
    if ok:
        # Never on boot, and never preferred over a real network: the daemon
        # decides when this profile is wanted, and NetworkManager picking it up on
        # its own would strand the ticker on its own island next to a working
        # router. -999 is the floor NetworkManager documents.
        _run(["connection", "modify", HOTSPOT_CONNECTION,
              "connection.autoconnect", "no",
              "connection.autoconnect-priority", "-999"], root=True)
    return ok, output


def hotspot_down() -> tuple[bool, str]:
    """Drop the setup access point. Succeeds quietly if it was not up."""
    ok, output = _run(["connection", "down", "id", HOTSPOT_CONNECTION], root=True)
    if not ok and "not an active" in output.lower():
        return True, "hotspot was not up"
    return ok, output


def known_network_in_range() -> bool:
    """Whether any stored network is currently visible.

    Used after dropping the hotspot to decide whether it is worth waiting for
    NetworkManager to reconnect, or whether to put the access point straight back.
    """
    stored = set(saved_networks())
    if not stored:
        return False
    return any(network.ssid in stored for network in scan(rescan=True))


# -- fallback state machine --------------------------------------------------
#
# Kept as one pure function so the decisions are testable without a radio, a
# root shell, or a way to make a router disappear. The daemon is then a thin loop
# that reads state, calls this, and performs the action.

GRACE_CHECKS = 3        # ~1 min at the default interval before giving up on a network
RETRY_AFTER = 240.0     # seconds of hotspot before looking around for a known network


def next_action(
    state: str,
    misses: int,
    hotspot_since: float | None,
    now: float,
    *,
    grace: int = GRACE_CHECKS,
    retry_after: float = RETRY_AFTER,
) -> str:
    """Decide what the fallback daemon should do next.

    Returns one of:

    * ``wait``           -- do nothing this round
    * ``raise_hotspot``  -- start the access point and tell the panel
    * ``clear_hotspot``  -- a real network is up; stop advertising and clear the panel
    * ``retry_known``    -- drop the access point briefly and look for known networks

    The grace period matters more than it looks: a Pi that has just booted, or one
    whose router rebooted, is legitimately disconnected for a few seconds, and an
    access point raised in that window would take it off a network it was about to
    rejoin. Waiting three checks costs a minute of blank panel in the rare case and
    avoids fighting NetworkManager in the common one.
    """
    if state in ("connected", "connecting"):
        return "clear_hotspot" if hotspot_since is not None else "wait"
    if state == "hotspot":
        if hotspot_since is None:
            return "wait"  # someone else raised it; adopt it next round
        return "retry_known" if now - hotspot_since >= retry_after else "wait"
    if state == "unknown":
        # nmcli unreachable. Doing nothing is right: raising an access point on a
        # guess could take down a working connection this module cannot see.
        return "wait"
    return "raise_hotspot" if misses >= grace else "wait"
