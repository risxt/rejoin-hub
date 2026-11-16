"""Discord webhook messages and Roblox game names (both plain HTTPS, best effort, never block the engine long)."""
from __future__ import annotations

import json
import logging
import re
import threading
import urllib.request

log = logging.getLogger("rh.notify")

COLORS = {"ok": 0x22C55E, "warn": 0xF59E0B, "bad": 0xEF4444, "info": 0x3B82F6, "plain": 0x6B7280, "off": 0x6B7280}
WEBHOOK_RE = re.compile(r"^https://(discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)/api/webhooks/\d+/[\w-]+$")


def mask(user: str | None) -> str:
    if not user:
        return "?"
    if len(user) <= 4:
        return user[0] + "***"
    return user[:3] + "*" * (len(user) - 5) + user[-2:]


class Discord:
    def __init__(self) -> None:
        self.url = ""

    def set_url(self, url: str | None) -> None:
        url = (url or "").strip()
        self.url = url if WEBHOOK_RE.match(url) else ""

    def send(self, title: str, text: str, tone: str = "plain", fields: list[tuple[str, str]] | None = None) -> None:
        if not self.url:
            return
        embed = {"title": title[:250], "description": text[:3000], "color": COLORS.get(tone, COLORS["plain"])}
        if fields:
            embed["fields"] = [{"name": k[:250], "value": v[:1000] or "-", "inline": True} for k, v in fields[:20]]
        body = json.dumps({"username": "Rejoin Hub", "embeds": [embed]}).encode()
        url = self.url

        def post() -> None:
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                                      "User-Agent": "RejoinHub-Agent"})
                urllib.request.urlopen(req, timeout=15).close()
            except Exception as e:
                log.warning("discord: %s", e)

        threading.Thread(target=post, daemon=True).start()


_names: dict[str, str] = {}


def game_name(place_id: str | None) -> str | None:
    """Place id -> experience name through the public Roblox APIs (cached)."""
    if not place_id or place_id == "0":
        return None
    if place_id in _names:
        return _names[place_id]
    try:
        with urllib.request.urlopen(f"https://apis.roblox.com/universes/v1/places/{int(place_id)}/universe", timeout=10) as r:
            uni = json.loads(r.read())["universeId"]
        with urllib.request.urlopen(f"https://games.roblox.com/v1/games?universeIds={int(uni)}", timeout=10) as r:
            name = json.loads(r.read())["data"][0]["name"]
        _names[place_id] = name
        return name
    except Exception as e:
        log.debug("game name %s: %s", place_id, e)
        return None
