"""Wires the engine to Firebase: reports, commands, config, heartbeat. Also pairing and local files."""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import VERSION
from .device import Device
from .engine import Engine, parse_command
from .firebase import SERVER_TIMESTAMP, Firebase, FirebaseError

log = logging.getLogger("rh.agent")

HOME = Path(os.environ.get("RH_HOME", Path.home() / ".rejoinhub"))
CONFIG = HOME / "agent.json"
STATE = HOME / "state.json"
PIDFILE = HOME / "agent.pid"
STALE_COMMAND_MS = 10 * 60 * 1000


# ---- pairing ------------------------------------------------------------------------------------------------
class PairingError(ValueError):
    pass


def decode_pairing(code: str) -> dict:
    """RH2.<base64url {"d": database url, "f": fleet key}>, made by the dashboard's install command."""
    code = code.strip()
    if not code.startswith("RH2."):
        raise PairingError("this is not a current Rejoin Hub install code (it starts with RH2.). Copy it again from the dashboard")
    raw = code[4:]
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        raise PairingError("the install code is damaged: copy it again from the dashboard") from None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ("d", "f")):
        raise PairingError("the install code is incomplete: copy it again from the dashboard")
    if not re.match(r"^https://[a-z0-9-]+\.([a-z0-9-]+\.)?(firebaseio\.com|firebasedatabase\.app)$", data["d"].rstrip("/")):
        raise PairingError("the install code points at an unknown database")
    if not re.match(r"^[A-Za-z0-9]{32,64}$", data["f"]):
        raise PairingError("the install code is damaged: copy it again from the dashboard")
    return {"dbUrl": data["d"].rstrip("/"), "fleetKey": data["f"]}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (s or "phone")[:28]


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)  # holds the fleet key
    except OSError:
        pass


def pair(code: str, name: str | None, dev: Device | None) -> dict:
    """Check the code against Firebase, pick a free device id, register the phone. Returns the saved config."""
    cfg = decode_pairing(code)
    fb = Firebase(cfg["dbUrl"], cfg["fleetKey"])
    try:
        fb.get("devices", shallow="true")
    except FirebaseError as e:
        if e.status in (401, 403):
            raise PairingError("this install code is no longer valid (the phone key was changed). Copy a fresh one from the dashboard") from None
        raise PairingError(f"could not reach Firebase ({e})") from None
    old = load_config()
    install_id = old.get("installId") or secrets.token_hex(6)
    model = dev.model() if dev else "Android"
    name = (name or old.get("name") or f"{model} {install_id[:4]}").strip()[:40]
    base = old.get("deviceId") or slug(name)
    device_id = base
    for n in range(2, 50):
        existing = fb.get(f"devices/{device_id}")
        if not existing or existing.get("installId") in (None, install_id):
            break
        device_id = f"{base[:26]}-{n}"
    cfg.update({"deviceId": device_id, "name": name, "installId": install_id})
    fb.patch(f"devices/{device_id}", {"name": name, "installId": install_id, "android": dev.android() if dev else "?",
                                      "model": model, "agent": VERSION, "lastReport": SERVER_TIMESTAMP})
    save_config(cfg)
    return cfg


# ---- reporting ----------------------------------------------------------------------------------------------
class FirebaseReporter:
    """Engine -> Firebase. Writes go through one background queue so the engine never waits on the network."""

    def __init__(self, fb: Firebase, device_id: str, device_name: str):
        self.fb = fb
        self.dev_id = device_id
        self.name = device_name
        self.sent: dict[str, dict] = {}
        self.q: queue.Queue[tuple[str, str, Any]] = queue.Queue(maxsize=2000)
        threading.Thread(target=self._pump, name="fb-writer", daemon=True).start()

    def _put(self, op: str, path: str, value: Any = None) -> None:
        try:
            self.q.put_nowait((op, path, value))
        except queue.Full:
            log.warning("write queue full, dropping %s %s", op, path)

    def _pump(self) -> None:
        while True:
            op, path, value = self.q.get()
            for attempt in range(6):
                try:
                    if op == "patch":
                        self.fb.patch(path, value)
                    elif op == "post":
                        self.fb.post(path, value)
                    elif op == "delete":
                        self.fb.delete(path)
                    break
                except FirebaseError as e:
                    if e.status in (400, 403):  # rejected by the rules: retrying won't help
                        log.warning("%s %s rejected: %s", op, path, e)
                        break
                    time.sleep(min(2 ** attempt, 30))
                except Exception as e:
                    log.warning("%s %s failed (%s), retrying", op, path, e)
                    time.sleep(min(2 ** attempt, 30))

    def account(self, letter: str, data: dict) -> None:
        if self.sent.get(letter) == data:
            return
        self.sent[letter] = dict(data)
        self._put("patch", f"accounts/{self.dev_id}/{letter}", data)

    def forget_account(self, letter: str) -> None:
        self.sent.pop(letter, None)
        self._put("delete", f"accounts/{self.dev_id}/{letter}")

    def device(self, data: dict) -> None:
        self._put("patch", f"devices/{self.dev_id}", {**data, "lastReport": SERVER_TIMESTAMP})

    def heartbeat(self) -> None:
        self._put("patch", f"devices/{self.dev_id}", {"lastReport": SERVER_TIMESTAMP, "agent": VERSION})

    def log(self, text: str, tone: str, letter: str | None = None) -> None:
        log.info("[%s] %s", (letter or "-").upper(), text)
        entry = {"t": int(time.time() * 1000), "deviceId": self.dev_id, "deviceName": self.name,
                 "text": text[:290], "tone": tone}
        if letter:
            entry["letter"] = letter
        self._put("post", "log", entry)

    def delete_command(self, cmd_id: str) -> None:
        self._put("delete", f"commands/{self.dev_id}/{cmd_id}")


# ---- the running agent --------------------------------------------------------------------------------------
class Agent:
    def __init__(self, cfg: dict, dev: Device):
        self.cfg = cfg
        self.fb = Firebase(cfg["dbUrl"], cfg["fleetKey"])
        self.dev_id = cfg["deviceId"]
        self.rep = FirebaseReporter(self.fb, self.dev_id, cfg.get("name", self.dev_id))
        self.engine = Engine(dev, self.rep, str(STATE))
        self.stop = self.engine.stop_event
        self.seen: set[str] = set()
        self.reload = threading.Event()

    def load_remote_config(self) -> None:
        cfg = self.fb.get("config") or {}
        key = self.fb.get("secrets/izenKey")
        self.engine.apply_config(cfg if isinstance(cfg, dict) else {}, key if isinstance(key, str) else "")

    def _on_command_event(self, event: str, path: str, data: Any) -> None:
        if data is None:
            return
        if path == "/":
            items = data.items() if isinstance(data, dict) else []
        else:
            items = [(path.strip("/").split("/")[0], data)] if "/" not in path.strip("/") else []
        now_ms = time.time() * 1000
        for cmd_id, raw in items:
            if cmd_id in self.seen:
                continue
            self.seen.add(cmd_id)
            self.rep.delete_command(cmd_id)
            cmd = parse_command(raw)
            if not cmd:
                continue
            at = cmd.get("at")
            if isinstance(at, (int, float)) and now_ms - at > STALE_COMMAND_MS:
                log.info("skipping stale command %s (%s)", cmd_id, cmd["type"])
                continue
            if cmd["type"] == "reload-config":
                self.reload.set()
            self.engine.submit(cmd)

    def _before_tick(self) -> None:
        if self.reload.is_set():
            self.reload.clear()
            try:
                self.load_remote_config()
                self.rep.log("Settings reloaded", "plain")
            except Exception as e:
                log.warning("config reload failed: %s", e)

    def _heartbeat_loop(self) -> None:
        while not self.stop.wait(30):
            self.rep.heartbeat()

    def run(self) -> None:
        try:
            self.fb.get("devices", shallow="true")
        except FirebaseError as e:
            if e.status in (401, 403):
                raise SystemExit("The phone key was changed in the dashboard. Copy the install command again and run it.") from None
            raise
        self.load_remote_config()
        self.rep.device({"agent": VERSION, "android": self.engine.dev.android()})
        threading.Thread(target=self.fb.stream, args=(f"commands/{self.dev_id}", self._on_command_event, self.stop),
                         name="commands", daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        self.engine.run(before_tick=self._before_tick)
