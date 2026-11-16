"""Delta key refresh through the izen bypass API, with the owner's own API key.

Flow: find the Delta key link (auth.platorelay.com) on the phone, send it to izen, write the returned key to Delta's
license file, verify by reading it back. The key file is shared by every clone, so one refresh covers all of them.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .device import Device
from .shell import q

log = logging.getLogger("rh.key")

LICENSE_DIRS = ["/sdcard/Delta/Internals/Cache", "/storage/emulated/0/Delta/Internals/Cache"]
LINK_STRICT = re.compile(r"https?://auth\.platorelay\.com/a\?d=[A-Za-z0-9+/=_-]+")
LINK_LOOSE = re.compile(r"https?://auth\.platorelay\.com/[^\s\"'<>]+")
IZEN_URL = "https://api.izen.lol/v1/bypass?url={url}"


@dataclass
class KeyResult:
    ok: bool
    message: str


def license_dir(dev: Device) -> str | None:
    for d in LICENSE_DIRS:
        if dev.exists_dir(d):
            return d
    return None


def license_state(dev: Device) -> str:
    """'ok' | 'missing' | 'empty' | 'no-delta'."""
    d = license_dir(dev)
    if not d:
        return "no-delta"
    content = dev.read_file(f"{d}/license")
    if content is None:
        return "missing"
    return "ok" if content.strip() else "empty"


def pick_link(text: str) -> str | None:
    hits = LINK_STRICT.findall(text)
    if hits:
        return hits[-1]
    loose = [h for h in LINK_LOOSE.findall(text) if len(h) > 30]
    return loose[-1] if loose else None


def find_link(dev: Device, packages: list[str]) -> str | None:
    """Search the places a Delta key link shows up, newest source first."""
    grep_url = "grep -oE 'https://auth[.]platorelay[.]com/[^ \"]+'"
    cmds = [
        "dumpsys clipboard",
        f"dumpsys activity activities | {grep_url} | tail -3",
        f"dumpsys activity recents | {grep_url} | tail -3",
        f"dumpsys activity starter | {grep_url} | tail -3",
        "logcat -d | grep -iE 'platorelay|platoboost' | tail -20",
        f"dumpsys activity providers | {grep_url} | tail -3",
    ]
    # Every clone has its own webview cache (PandoraGuard only searched com.roblox.client).
    for pkg in packages:
        cmds.append(f"timeout 6 grep -roaE 'https://auth.platorelay.com/a[?]d=[A-Za-z0-9+/=_-]*' "
                    f"/data/data/{q(pkg)}/app_webview/ /data/data/{q(pkg)}/cache/ 2>/dev/null | tail -3")
    cmds.append("timeout 6 grep -roaE 'https://auth.platorelay.com/[^ ]*' /proc/*/cmdline 2>/dev/null | tail -3")
    for cmd in cmds:
        link = pick_link(dev.out(cmd, 20))
        if link:
            return link
    return None


def call_izen(link: str, api_key: str, timeout: float = 95.0) -> tuple[str | None, str]:
    """Returns (key, message). The izen response carries status/result/message/error."""
    req = urllib.request.Request(IZEN_URL.format(url=urllib.parse.quote(link, safe="")),
                                 headers={"x-api-key": api_key, "Accept": "application/json",
                                          "User-Agent": "RejoinHub-Agent"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body, status = resp.read().decode("utf-8", "replace"), resp.status
    except urllib.error.HTTPError as e:
        body, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        return None, f"no connection to izen ({e.__class__.__name__})"
    try:
        data = json.loads(body)
    except ValueError:
        return None, f"izen HTTP {status}: unreadable reply"
    if not isinstance(data, dict):
        return None, f"izen HTTP {status}: unexpected reply"
    result = data.get("result")
    if str(data.get("status", "")).lower() == "success" and isinstance(result, str) and result.strip():
        return result.strip(), "ok"
    msg = result if isinstance(result, str) else data.get("message") or data.get("error") or f"HTTP {status}"
    return None, str(msg)[:120]


def write_key(dev: Device, key: str) -> bool:
    d = license_dir(dev) or LICENSE_DIRS[0]
    return dev.write_file(f"{d}/license", key)


def refresh(dev: Device, packages: list[str], api_key: str) -> KeyResult:
    if not api_key:
        return KeyResult(False, "no izen API key set in the dashboard")
    link = find_link(dev, packages)
    if not link:
        return KeyResult(False, "no Delta key link found: tap 'Get Key' in Delta once")
    key, msg = call_izen(link, api_key)
    if not key:
        return KeyResult(False, f"izen: {msg}")
    if not write_key(dev, key):
        return KeyResult(False, "got a key but could not write Delta's license file")
    return KeyResult(True, "new Delta key written")
