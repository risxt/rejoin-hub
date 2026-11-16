"""Turn a dashboard join target into a link that `am start -d` can open inside one clone.

Formats (Roblox deep links, create.roblox.com/docs/production/promotion/deeplinks):
  roblox://experiences/start?placeId=P                       join a place
  roblox://experiences/start?placeId=P&gameInstanceId=J      join one server (JobId); protocol form only
  roblox://experiences/start?placeId=P&linkCode=C            join a private server by its link code
Share links (`roblox.com/share?code=...&type=Server`) carry no place id, so they are passed through as https links;
the Roblox app (and every renamed clone) claims them through its intent filters.
"""
from __future__ import annotations

import re
import urllib.parse

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DIGITS_RE = re.compile(r"^\d{3,15}$")


class TargetError(ValueError):
    pass


def build_link(target: dict | None, fallback_place: str | None = None) -> str | None:
    """Return the deep link for a target, or None when there is no target (plain app launch)."""
    if not target or not str(target.get("value", "")).strip():
        return None
    kind = target.get("kind")
    value = str(target["value"]).strip()

    if kind == "place":
        if not DIGITS_RE.match(value):
            raise TargetError(f"place id must be digits: {value!r}")
        return start_link(value)

    if kind == "job":
        place = str(target.get("place") or fallback_place or "").strip()
        if not UUID_RE.match(value):
            raise TargetError(f"job id must be a server UUID: {value!r}")
        if not DIGITS_RE.match(place):
            raise TargetError("a job id needs the place id too")
        return start_link(place, gameInstanceId=value.lower())

    if kind == "ps":
        return private_server_link(value)

    raise TargetError(f"unknown target kind {kind!r}")


def start_link(place_id: str, **params: str) -> str:
    q = {"placeId": place_id, **params}
    return "roblox://experiences/start?" + urllib.parse.urlencode(q)


def private_server_link(value: str) -> str:
    v = value.strip()
    if v.startswith("roblox://"):
        return v
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    u = urllib.parse.urlparse(v)
    host = (u.hostname or "").lower()
    if host not in ("roblox.com", "www.roblox.com", "web.roblox.com", "ro.blox.com"):
        raise TargetError("private server links must be roblox.com links")
    qs = urllib.parse.parse_qs(u.query)
    m = re.match(r"^/games/(\d+)", u.path)
    code = (qs.get("privateServerLinkCode") or qs.get("linkCode") or [None])[0]
    if m and code:
        # Old-style private server link: convert to the protocol form, which skips the browser hop.
        return start_link(m.group(1), linkCode=code)
    if u.path.rstrip("/") == "/share" and qs.get("code"):
        return urllib.parse.urlunparse(("https", "www.roblox.com", "/share", "", u.query, ""))
    raise TargetError("paste a roblox.com private server or share link")
