"""A simulated rooted phone with Roblox clones, a detector, and a clock, for engine tests."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from rejoinhub.device import Device, Identity


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@dataclass
class Clone:
    pkg: str
    user: str
    uid: str
    running: bool = False
    pid: int = 0
    join_after: float | None = 20.0   # seconds after launch until the detector reports in game; None = never
    place: str = "8737899170"
    job: str = "11111111-2222-3333-4444-555555555555"
    launched_at: float = 0.0
    frozen: bool = False              # process alive but the detector stops writing
    error: dict | None = None
    links: list = field(default_factory=list)


class FakeDevice(Device):
    WS = "/storage/emulated/0/Delta/Workspace"

    def __init__(self, clock: Clock, clones: list[Clone], delta: bool = True, license: str | None = "KEY"):
        self.clock = clock
        self.clones = {c.pkg: c for c in clones}
        self.files: dict[str, str] = {}
        self.dirs = {"/storage/emulated/0/Delta/Autoexecute", self.WS, "/sdcard/Delta/Internals/Cache"} if delta else set()
        if delta and license is not None:
            self.files["/sdcard/Delta/Internals/Cache/license"] = license
        self.calls: list[str] = []
        self.next_pid = 1000
        self.logcat_ok: set[str] = set()
        self._sdk = 30
        self._launchers = {}

    # -- world simulation -----------------------------------------------------------------------------------
    def advance(self, seconds: float, step: float = 1.0) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            self._write_heartbeats()

    def _write_heartbeats(self) -> None:
        now = self.clock.t
        for c in self.clones.values():
            if not c.running or c.join_after is None or c.frozen:
                continue
            if now - c.launched_at < c.join_after:
                continue
            path = f"{self.WS}/RejoinHub/hb_{c.uid}.json"
            prev = json.loads(self.files.get(path, "{}") or "{}")
            if prev.get("ts", 0) and now - prev["ts"] < 10 and not c.error:
                continue
            boot = prev.get("boot") if prev.get("boot", 0) >= c.launched_at else now
            self.files[path] = json.dumps({"v": 1, "ts": int(now), "boot": int(boot), "userId": c.uid, "user": c.user,
                                           "placeId": c.place, "jobId": c.job, "err": c.error})

    def crash(self, pkg: str) -> None:
        self.clones[pkg].running = False

    # -- Device API used by the engine ----------------------------------------------------------------------
    def run(self, cmd: str, timeout: float = 30.0):
        self.calls.append(cmd)
        return 0, ""

    def out(self, cmd: str, timeout: float = 30.0) -> str:
        self.calls.append(cmd)
        if "RejoinHub')}/hb_" in cmd or "/RejoinHub/hb_*.json" in cmd or "RejoinHub/hb_" in cmd:
            parts = []
            for p, v in self.files.items():
                if "/RejoinHub/hb_" in p:
                    parts.append(f"@@RH@@{p}\n{v}\n")
            return "".join(parts)
        return ""

    def android(self) -> str:
        return "11"

    def model(self) -> str:
        return "FakePhone"

    def ram_mb(self):
        return 2000, 4000

    def roblox_packages(self):
        return sorted(self.clones)

    def identity(self, pkg: str) -> Identity:
        c = self.clones[pkg]
        return Identity(c.user, None)  # like a real device: username known, user id often not

    def pids(self, pkg: str):
        c = self.clones[pkg]
        return [c.pid] if c.running else []

    def force_stop(self, pkg: str) -> None:
        self.calls.append(f"force-stop {pkg}")
        self.clones[pkg].running = False

    def launch(self, pkg: str, link, freeform: bool) -> None:
        self.calls.append(f"launch {pkg} {link}")
        c = self.clones[pkg]
        c.running, c.pid, c.launched_at, c.error, c.frozen = True, self.next_pid, self.clock.t, None, False
        c.links.append(link)
        self.next_pid += 1

    def join_verdict(self, pid: int) -> str:
        for c in self.clones.values():
            if c.pid == pid and c.pkg in self.logcat_ok and self.clock.t - c.launched_at > 20:
                return "ok"
        return "unknown"

    def enable_freeform(self) -> bool:
        return False

    def set_rotation(self, landscape: bool) -> None: ...
    def tune(self) -> None: ...
    def raise_process_limit(self) -> None: ...
    def drop_caches(self) -> None: ...
    def close_other_apps(self, keep): return []
    def apply_low_gfx(self, pkg, on): return True
    def focus(self, pkg, freeform): self.calls.append(f"focus {pkg}")

    def screen_size(self):
        return (1280, 720)

    def task_ids(self):
        return {p: 10 + i for i, p in enumerate(sorted(self.clones)) if self.clones[p].running}

    def resize_task(self, task_id, rect) -> bool:
        self.calls.append(f"resize {task_id} {rect}")
        return True

    def exists_dir(self, path: str) -> bool:
        return path in self.dirs

    def write_file(self, path: str, content: str) -> bool:
        self.files[path] = content
        return True

    def read_file(self, path: str):
        return self.files.get(path)


class MemReporter:
    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.logs: list[tuple[str, str, str | None]] = []
        self.devices: list[dict] = []

    def account(self, letter, data):
        self.accounts[letter] = dict(data)

    def device(self, data):
        self.devices.append(data)

    def log(self, text, tone, letter=None):
        self.logs.append((text, tone, letter))

    def forget_account(self, letter):
        self.accounts.pop(letter, None)

    def texts(self, letter=None):
        return [t for t, _, l in self.logs if letter is None or l == letter]
