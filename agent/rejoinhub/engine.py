"""The rejoin engine: one supervisor per phone, one Instance per Roblox clone.

Each tick (every `checkS` seconds) it reads the detector status files, checks every clone's process, and decides:
in game, rejoining, stuck in the lobby, crashed, or hit by an error. It relaunches with backoff, refreshes the Delta
key when needed, tiles the windows, and reports every change through the Reporter.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from typing import Any, Callable, Protocol

from . import grid, keybypass
from .device import Device, clone_letter
from .links import TargetError, build_link
from .notify import Discord, game_name, mask
from .shell import q

log = logging.getLogger("rh.engine")

# (name, auto-execute folder, workspace folder). Delta's paths are the ones PandoraGuard used on devices.
EXECUTORS = [
    ("Delta", "/storage/emulated/0/Delta/Autoexecute", "/storage/emulated/0/Delta/Workspace"),
    ("Fluxus", "/storage/emulated/0/Fluxus/Autoexec", "/storage/emulated/0/Fluxus/Workspace"),
    ("Arceus X", "/storage/emulated/0/Arceus X/scripts/Autoexecute", "/storage/emulated/0/Arceus X/Workspace"),
    ("Codex", "/storage/emulated/0/CodeX/scripts/Autoexec", "/storage/emulated/0/CodeX/Workspace"),
    ("Hydrogen", "/storage/emulated/0/Hydrogen/Autoexecute", "/storage/emulated/0/Hydrogen/Workspace"),
    ("Ronix", "/storage/emulated/0/RonixExploit/autoexec", "/storage/emulated/0/RonixExploit/workspace"),
]
DETECTOR_NAME = "rejoinhub_detector.lua"
HB_FRESH_S = 35            # the detector writes every 10 s; 3 missed writes = stale
GRACE_KINDS = {"teleport_failed"}   # Roblox may retry a teleport on its own
OWNER_KINDS = {"banned"}            # never auto-rejoin
NEEDS_ALWAYS = {"same_account"}     # rejoin only when "Always rejoin" is on (it would kick the other session)

DEFAULTS: dict[str, Any] = {
    "checkS": 10, "stuckS": 45, "restartWaitS": 5, "alwaysRejoin": True, "autoKey": True, "lowGfx": True,
    "raiseProcLimit": True, "closeOtherApps": False, "autoGrid": True, "orientation": "Landscape",
    "webhook": "", "statusEvery": True, "focusRotateS": 0, "launchGapS": 8,
}


class Reporter(Protocol):
    def account(self, letter: str, data: dict) -> None: ...
    def device(self, data: dict) -> None: ...
    def log(self, text: str, tone: str, letter: str | None = None) -> None: ...
    def forget_account(self, letter: str) -> None: ...


@dataclass
class Instance:
    pkg: str
    letter: str
    enabled: bool = True
    target: dict | None = None
    user: str | None = None
    user_id: str | None = None
    status: str = "offline"
    since: float | None = None          # when the current in-game stretch began
    launched_at: float = 0.0
    next_try_at: float = 0.0
    last_hb: float = 0.0                 # newest detector ts seen for this clone
    place_id: str | None = None
    job_id: str | None = None
    handled_err_ts: float = 0.0
    pending_err: dict | None = None      # error waiting out its grace period
    pending_until: float = 0.0
    joined_once: bool = False            # reached the game since the last launch
    fail_streak: int = 0
    rejoins: int = 0
    rejoins_today: int = 0
    uptime_today_s: float = 0.0
    day: str = ""
    last_error: str | None = None
    last_error_at: float | None = None
    last_logcat_check: float = 0.0
    awaiting_launch: bool = False        # stopped on purpose, launch at next_try_at
    via_logcat: bool = False             # in game according to logcat (detector not running)
    lobby_since: float | None = None

    def to_state(self) -> dict:
        return {k: getattr(self, k) for k in ("enabled", "target", "user", "user_id", "rejoins", "rejoins_today",
                                              "uptime_today_s", "day", "handled_err_ts", "last_error", "last_error_at")}


@dataclass
class Heartbeat:
    ts: float
    boot: float
    user_id: str
    user: str
    place_id: str
    job_id: str
    err: dict | None
    path: str = ""


class Engine:
    def __init__(self, dev: Device, reporter: Reporter, state_path: str | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.dev = dev
        self.rep = reporter
        self.clock = clock
        self.sleep = sleep
        self.cfg: dict[str, Any] = dict(DEFAULTS)
        self.izen_key = ""
        self.instances: dict[str, Instance] = {}
        self.commands: queue.Queue[dict] = queue.Queue()
        self.state_path = state_path
        self.discord = Discord()
        self.bindings: dict[str, str] = {}       # Roblox userId -> package
        self.workspaces: list[str] = [e[2] for e in EXECUTORS]
        self.detector_installed: list[str] = []
        self.last_detector_install = 0.0
        self.last_discovery = 0.0
        self.key_busy = threading.Event()
        self.key_lock_until = 0.0
        self.key_failures = 0
        self.last_key_result: str | None = None
        self.retile_at = 0.0
        self.tiled_set: tuple[str, ...] = ()
        self.focus_idx = 0
        self.last_focus = 0.0
        self.last_mem_clean = 0.0
        self.last_status_post = 0.0
        self.last_device_report = 0.0
        self.last_close = 0.0
        self.freeform_ready = False
        self.stop_event = threading.Event()
        self._load_state()

    # ---- persistence ----------------------------------------------------------------------------------------
    def _load_state(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.bindings = data.get("bindings", {})
        for pkg, st in data.get("instances", {}).items():
            inst = Instance(pkg=pkg, letter=clone_letter(pkg))
            for k, v in st.items():
                if hasattr(inst, k):
                    setattr(inst, k, v)
            self.instances[pkg] = inst

    def save_state(self) -> None:
        if not self.state_path:
            return
        data = {"bindings": self.bindings, "instances": {p: i.to_state() for p, i in self.instances.items()}}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        import os
        os.replace(tmp, self.state_path)

    # ---- config ---------------------------------------------------------------------------------------------
    def apply_config(self, cfg: dict | None, izen_key: str | None = None) -> None:
        merged = dict(DEFAULTS)
        for k, v in (cfg or {}).items():
            if k in DEFAULTS and v is not None:
                merged[k] = v
        merged["checkS"] = max(5, int(merged["checkS"]))
        merged["stuckS"] = max(30, int(merged["stuckS"]))
        merged["restartWaitS"] = max(0, int(merged["restartWaitS"]))
        old = self.cfg
        self.cfg = merged
        if izen_key is not None:
            self.izen_key = izen_key.strip()
        self.discord.set_url(merged.get("webhook"))
        if old.get("lowGfx") != merged["lowGfx"]:
            for inst in self.instances.values():
                self.dev.apply_low_gfx(inst.pkg, bool(merged["lowGfx"]))
        if old.get("orientation") != merged["orientation"] or old.get("autoGrid") != merged["autoGrid"]:
            self.tiled_set = ()
            self.retile_at = self.clock()

    @property
    def join_timeout(self) -> float:
        return max(self.cfg["stuckS"] * 2, 150)

    # ---- startup --------------------------------------------------------------------------------------------
    def setup(self) -> None:
        pkgs = self.dev.roblox_packages()
        if not pkgs:
            self.rep.log("No Roblox app found on this phone", "bad")
        known = set(self.instances)
        for pkg in pkgs:
            inst = self.instances.get(pkg) or Instance(pkg=pkg, letter=clone_letter(pkg))
            ident = self.dev.identity(pkg)
            inst.user = ident.user or inst.user
            inst.user_id = ident.user_id or inst.user_id
            if inst.user_id:
                self.bindings[inst.user_id] = pkg
            self.instances[pkg] = inst
        for gone in known - set(pkgs):
            self.rep.forget_account(self.instances[gone].letter)
            del self.instances[gone]
        if self.cfg["raiseProcLimit"]:
            self.dev.raise_process_limit()
        self.dev.tune()
        if self.cfg["autoGrid"]:
            if self.dev.enable_freeform():
                self.rep.log("Freeform windows switched on. If windows don't tile, restart the phone once", "warn")
            self.freeform_ready = True
        self.dev.set_rotation(self.cfg["orientation"] != "Portrait")
        if self.cfg["lowGfx"]:
            for inst in self.instances.values():
                self.dev.apply_low_gfx(inst.pkg, True)
        self.install_detector()
        names = ", ".join(f"{i.letter.upper()}={mask(i.user)}" for i in self.instances.values())
        self.rep.log(f"Agent started with {len(self.instances)} clone(s): {names or 'none'}", "info")
        self.discord.send("Agent started", f"{len(self.instances)} clone(s): {names or 'none'}", "info")
        now = self.clock()
        gap = 0
        for inst in self.instances.values():
            if not inst.enabled:
                inst.status = "offline"
            elif self.dev.alive(inst.pkg):
                # adopt a running session: give it the join window to prove itself
                inst.status, inst.launched_at = "rejoining", now - 5
            else:
                inst.status, inst.awaiting_launch = "rejoining", True
                inst.next_try_at = now + gap * self.cfg["launchGapS"]
                gap += 1
            self._report(inst)
        self.save_state()

    def install_detector(self) -> None:
        self.last_detector_install = self.clock()
        code = resources.files("rejoinhub").joinpath("lua", DETECTOR_NAME).read_text(encoding="utf-8")
        done = []
        for name, auto_dir, _ in EXECUTORS:
            if self.dev.exists_dir(auto_dir):
                if self.dev.write_file(f"{auto_dir}/{DETECTOR_NAME}", code):
                    done.append(name)
        if done and done != self.detector_installed:
            self.rep.log(f"Detector installed for {', '.join(done)}", "ok")
        elif not done and not self.detector_installed:
            self.rep.log("No executor auto-execute folder yet (open Delta once). Using logcat checks until then", "warn")
        self.detector_installed = done

    # ---- commands -------------------------------------------------------------------------------------------
    def submit(self, cmd: dict) -> None:
        self.commands.put(cmd)

    def _targets(self, letter: str | None) -> list[Instance]:
        if not letter:
            return list(self.instances.values())
        return [i for i in self.instances.values() if i.letter == str(letter).lower()]

    def handle_command(self, cmd: dict) -> None:
        kind = cmd.get("type")
        insts = self._targets(cmd.get("letter"))
        now = self.clock()
        if kind == "reload-config":
            return  # the agent re-reads config before calling us
        if not insts:
            self.rep.log(f"Command '{kind}' ignored: no clone {cmd.get('letter')!r}", "warn")
            return
        for i, inst in enumerate(insts):
            if kind == "rejoin":
                inst.enabled = True
                self._relaunch(inst, cmd.get("reason") or "rejoin requested from the dashboard",
                               now + i * self.cfg["launchGapS"], manual=True)
            elif kind == "start":
                if not inst.enabled or inst.status == "offline":
                    inst.enabled = True
                    inst.fail_streak = 0
                    inst.next_try_at = now + i * self.cfg["launchGapS"]
                    if self.dev.alive(inst.pkg):
                        inst.status, inst.launched_at = "rejoining", now - 5
                    else:
                        inst.status, inst.awaiting_launch = "rejoining", True
                    self.rep.log("Started from the dashboard", "info", inst.letter)
            elif kind == "stop":
                inst.enabled = False
                self.dev.force_stop(inst.pkg)
                inst.status, inst.since, inst.pending_err = "offline", None, None
                self.rep.log("Stopped from the dashboard", "plain", inst.letter)
            elif kind == "target":
                t = cmd.get("target")
                try:
                    build_link(t, inst.place_id)
                except TargetError as e:
                    self.rep.log(f"Target rejected: {e}", "bad", inst.letter)
                    continue
                inst.target = t
                inst.enabled = True
                self._relaunch(inst, "new join target", now, manual=True)
            self._report(inst)
        self.tiled_set = ()
        self.save_state()

    # ---- main loop ------------------------------------------------------------------------------------------
    def run(self, before_tick: Callable[[], None] | None = None) -> None:
        self.setup()
        while not self.stop_event.is_set():
            started = self.clock()
            try:
                if before_tick:
                    before_tick()
                self.tick()
            except Exception:
                log.exception("tick failed")
            # Wake early for dashboard commands.
            remaining = self.cfg["checkS"] - (self.clock() - started)
            try:
                cmd = self.commands.get(timeout=max(0.5, remaining))
                self.handle_command(cmd)
                while True:
                    self.handle_command(self.commands.get_nowait())
            except queue.Empty:
                pass

    def tick(self) -> None:
        now = self.clock()
        today = date.fromtimestamp(now).isoformat()
        hbs = self.read_heartbeats()
        by_pkg = self._match(hbs, now)
        key_needed: list[Instance] = []
        for inst in self.instances.values():
            if inst.day != today:
                inst.day, inst.rejoins_today, inst.uptime_today_s = today, 0, 0.0
            hb = by_pkg.get(inst.pkg)
            if hb:
                inst.last_hb = max(inst.last_hb, hb.ts)
                if hb.place_id and hb.place_id != "0":
                    inst.place_id = hb.place_id
                inst.job_id = hb.job_id or inst.job_id
                inst.user = inst.user or hb.user
                inst.user_id = inst.user_id or hb.user_id
            if self.key_busy.is_set() or not inst.enabled:
                self._report(inst)
                continue
            if self._step(inst, hb, now):
                key_needed.append(inst)
            self._report(inst)
        if key_needed and not self._maybe_refresh_key(now):
            for inst in key_needed:
                self._relaunch(inst, "did not reach the game", now)
                self._report(inst)
        self._housekeeping(now)
        self.save_state()

    def _step(self, inst: Instance, hb: Heartbeat | None, now: float) -> bool:
        """Advance one clone. Returns True when its state points at an expired Delta key."""
        alive = self.dev.alive(inst.pkg)
        prev = inst.status

        if not alive:
            if inst.awaiting_launch:
                if now >= inst.next_try_at:
                    self._launch(inst, now)
                return False
            if prev in ("ingame", "lobby") or (prev == "rejoining" and inst.launched_at and now - inst.launched_at > 6):
                self._count_uptime(inst, now)
                inst.status, inst.since = "crashed", None
                inst.last_error, inst.last_error_at = "Roblox closed", now
                inst.next_try_at = now + self.cfg["restartWaitS"]
                inst.awaiting_launch = True
                self.rep.log("Roblox closed or crashed, restarting", "bad", inst.letter)
                self.discord.send(f"{inst.letter.upper()} crashed", f"{mask(inst.user)}: Roblox closed. Restarting.", "bad")
            elif prev in ("offline", "crashed") or inst.status == "rejoining":
                inst.awaiting_launch = True
            if inst.awaiting_launch and now >= inst.next_try_at:
                self._launch(inst, now)
            return False

        fresh = hb is not None and now - hb.ts <= HB_FRESH_S and hb.ts >= inst.launched_at - 5
        in_game = fresh and hb is not None and hb.place_id not in ("", "0")

        # 1. Errors reported by the detector
        if fresh and hb and hb.err and float(hb.err.get("ts", 0)) > max(inst.handled_err_ts, inst.launched_at - 5):
            return self._on_error(inst, hb, now)
        if inst.pending_err and now >= inst.pending_until:
            moved = hb is not None and hb.job_id and hb.job_id != inst.pending_err.get("job")
            err, inst.pending_err = inst.pending_err, None
            if moved:
                self.rep.log(f"Recovered by itself after {err['kind']}", "ok", inst.letter)
            else:
                self._relaunch(inst, f"{err['kind']} (code {err.get('code', 0)})", now)
                return False

        # 2. In game (detector)
        if in_game:
            inst.via_logcat = False
            self._enter_game(inst, now, prev)
            return False

        since_launch = now - inst.launched_at

        # 3. No detector signal at all since this launch: fall back to the clone's logcat.
        if not self._detector_seen(inst):
            if inst.status == "ingame" and inst.via_logcat:
                if now - inst.last_logcat_check >= 15:
                    inst.last_logcat_check = now
                    pids = self.dev.pids(inst.pkg)
                    if pids and self.dev.join_verdict(pids[0]) == "fail":
                        self._relaunch(inst, "disconnected (logcat)", now)
                return False
            if since_launch < self.join_timeout:
                inst.status = "rejoining"
                if now - inst.last_logcat_check >= 15:
                    inst.last_logcat_check = now
                    pids = self.dev.pids(inst.pkg)
                    verdict = self.dev.join_verdict(pids[0]) if pids else "unknown"
                    if verdict == "ok":
                        inst.via_logcat = True
                        self._enter_game(inst, now, prev, note=" (logcat; detector not running)")
                    elif verdict == "fail":
                        self._relaunch(inst, "join failed (logcat)", now)
                return False
            # Never reached a running game. On Delta that is usually the key screen.
            return True

        # 4. The detector ran for this launch but is not fresh / not in a place now: lobby, frozen or kicked out.
        if inst.status == "ingame":
            self._count_uptime(inst, now)
            inst.status, inst.since = "lobby", None
            self.rep.log("Left the game (detector went quiet)", "warn", inst.letter)
        elif inst.status != "lobby":
            inst.status = "lobby"
        if fresh:
            # the script runs but reports no place (between places): count from when that started
            inst.lobby_since = inst.lobby_since or now
            stale_for = now - inst.lobby_since
        else:
            stale_for = now - max(inst.last_hb, inst.launched_at)
        if stale_for >= self.cfg["stuckS"]:
            self._relaunch(inst, f"stuck for {int(stale_for)}s", now)
        return False

    def _enter_game(self, inst: Instance, now: float, prev: str, note: str = "") -> None:
        inst.lobby_since = None
        if inst.status != "ingame":
            inst.since = now
            inst.status = "ingame"
            name = game_name(inst.place_id) if inst.place_id else None
            where = f" · {name or 'place ' + str(inst.place_id)}" if inst.place_id else ""
            self.rep.log(f"In game{where}{note}", "ok", inst.letter)
            if prev in ("rejoining", "crashed", "lobby") and inst.rejoins > 1:
                self.discord.send(f"{inst.letter.upper()} back in game", f"{mask(inst.user)}{where}", "ok")
        inst.joined_once = True
        inst.fail_streak = 0

    def _detector_seen(self, inst: Instance) -> bool:
        return inst.last_hb > 0 and inst.last_hb >= inst.launched_at - 5

    def _on_error(self, inst: Instance, hb: Heartbeat, now: float) -> bool:
        err = hb.err or {}
        kind, code, ts = str(err.get("kind", "disconnect")), int(err.get("code") or 0), float(err.get("ts", now))
        inst.handled_err_ts = ts
        text = f"{kind.replace('_', ' ')}" + (f" (code {code})" if code else "")
        inst.last_error, inst.last_error_at = text, now
        self.rep.log(f"Roblox error: {text}", "bad", inst.letter)
        if kind == "key_expired":
            self.discord.send("Delta key expired", f"{mask(inst.user)}", "bad")
            if not self.cfg["autoKey"]:
                self.rep.log("Auto key refresh is off: enter the key in Delta", "warn", inst.letter)
            return True
        if kind in OWNER_KINDS or (kind in NEEDS_ALWAYS and not self.cfg["alwaysRejoin"]):
            inst.enabled = False
            inst.status, inst.since = "offline", None
            self.dev.force_stop(inst.pkg)
            self.rep.log(f"Not rejoining after '{kind}'. Start it again from the dashboard", "warn", inst.letter)
            self.discord.send(f"{inst.letter.upper()} needs you", f"{mask(inst.user)}: {text}", "bad")
            return False
        if kind in GRACE_KINDS:
            inst.pending_err = {"kind": kind, "code": code, "job": hb.job_id}
            inst.pending_until = now + 10
            return False
        self.discord.send(f"{inst.letter.upper()} {text}", f"{mask(inst.user)}: rejoining", "warn")
        self._relaunch(inst, text, now)
        return False

    # ---- launching ------------------------------------------------------------------------------------------
    def _relaunch(self, inst: Instance, reason: str, at: float, manual: bool = False) -> None:
        self._count_uptime(inst, self.clock())
        if not manual and inst.status == "rejoining" and not inst.joined_once:
            inst.fail_streak += 1
        delay = 0.0 if manual else self.cfg["restartWaitS"]
        if inst.fail_streak >= 3:
            delay = min(30 * 2 ** (inst.fail_streak - 3), 600)
            self.rep.log(f"{inst.fail_streak} failed joins in a row, waiting {int(delay)}s", "warn", inst.letter)
        self.dev.force_stop(inst.pkg)
        inst.status, inst.since, inst.pending_err, inst.via_logcat, inst.lobby_since = "rejoining", None, None, False, None
        inst.next_try_at = at + delay
        inst.awaiting_launch = True  # _step launches it once next_try_at passes
        self.rep.log(f"Rejoining: {reason}", "info", inst.letter)

    def _launch(self, inst: Instance, now: float) -> None:
        try:
            link = build_link(inst.target, inst.place_id)
        except TargetError as e:
            self.rep.log(f"Bad target, joining without it: {e}", "warn", inst.letter)
            link = None
        freeform = bool(self.cfg["autoGrid"]) and self.freeform_ready
        self.dev.launch(inst.pkg, link, freeform)
        inst.launched_at = self.clock()
        inst.awaiting_launch = False
        inst.joined_once = False
        inst.status = "rejoining"
        inst.rejoins += 1
        inst.rejoins_today += 1
        inst.next_try_at = inst.launched_at + self.join_timeout
        self.retile_at = inst.launched_at + 8
        self.tiled_set = ()
        # stagger the next clone so they don't load at the same moment
        for other in self.instances.values():
            if other is not inst and other.enabled and other.next_try_at < inst.launched_at + self.cfg["launchGapS"] \
                    and other.status in ("rejoining", "crashed", "offline") and not self.dev.alive(other.pkg):
                other.next_try_at = inst.launched_at + self.cfg["launchGapS"]

    def _count_uptime(self, inst: Instance, now: float) -> None:
        if inst.status == "ingame" and inst.since:
            inst.uptime_today_s += max(0.0, now - max(inst.since, now - 86400))

    # ---- key refresh ----------------------------------------------------------------------------------------
    def _maybe_refresh_key(self, now: float) -> bool:
        """Start a key refresh in the background. False when it can't run now (the caller relaunches instead)."""
        if self.key_busy.is_set():
            return True
        if now < self.key_lock_until or not self.cfg["autoKey"]:
            return False
        if keybypass.license_dir(self.dev) is None:
            return False  # not Delta: nothing to refresh
        self.key_busy.set()
        self.rep.log("Delta key expired, refreshing through izen (up to ~90s)", "info")

        def work() -> None:
            try:
                res = keybypass.refresh(self.dev, list(self.instances), self.izen_key)
            except Exception as e:
                res = keybypass.KeyResult(False, f"error: {e}")
            finally:
                self.key_busy.clear()
            done = self.clock()
            self.last_key_result = res.message
            if res.ok:
                self.key_failures = 0
                self.key_lock_until = done + 120
                waiting = [i for i in self.instances.values() if i.enabled and i.status != "ingame"]
                self.rep.log(f"New Delta key written, restarting {len(waiting)} clone(s) that were not in game", "ok")
                self.discord.send("Delta key refreshed", f"Restarting {len(waiting)} clone(s)", "ok")
                for inst in waiting:
                    self.submit({"type": "rejoin", "letter": inst.letter, "reason": "new Delta key"})
            else:
                self.key_failures += 1
                wait = 120 if self.key_failures < 3 else 900
                self.key_lock_until = done + wait
                self.rep.log(f"Key refresh failed: {res.message}", "bad")
                if self.key_failures in (1, 3):
                    self.discord.send("Key refresh failed", res.message, "bad")

        threading.Thread(target=work, name="key-refresh", daemon=True).start()
        return True

    # ---- detector files -------------------------------------------------------------------------------------
    def read_heartbeats(self) -> list[Heartbeat]:
        globs = " ".join(f"{q(w + '/RejoinHub')}/hb_*.json" for w in self.workspaces)
        txt = self.dev.out(f"for f in {globs}; do [ -f \"$f\" ] && {{ echo \"@@RH@@$f\"; cat \"$f\"; echo; }}; done", 20)
        out: list[Heartbeat] = []
        for chunk in txt.split("@@RH@@")[1:]:
            path, _, body = chunk.partition("\n")
            try:
                d = json.loads(body.strip())
                out.append(Heartbeat(ts=float(d.get("ts", 0)), boot=float(d.get("boot", 0)), user_id=str(d.get("userId", "")),
                                     user=str(d.get("user", "")), place_id=str(d.get("placeId", "0")), job_id=str(d.get("jobId", "")),
                                     err=d.get("err") if isinstance(d.get("err"), dict) else None, path=path.strip()))
            except (ValueError, TypeError):
                continue
        return out

    def _match(self, hbs: list[Heartbeat], now: float) -> dict[str, Heartbeat]:
        """Assign each status file to a clone: by bound userId, by username, then by launch timing."""
        res: dict[str, Heartbeat] = {}
        unbound: list[Heartbeat] = []
        by_name = {(i.user or "").lower(): i.pkg for i in self.instances.values() if i.user}
        for hb in sorted(hbs, key=lambda h: h.ts, reverse=True):
            pkg = self.bindings.get(hb.user_id) or by_name.get(hb.user.lower())
            if pkg and pkg in self.instances:
                self.bindings[hb.user_id] = pkg
                res.setdefault(pkg, hb)
            elif now - hb.ts <= HB_FRESH_S:
                unbound.append(hb)
        for hb in unbound:
            # the clone that launched last before this script booted, and has no file yet
            waiting = [i for i in self.instances.values() if i.pkg not in res and i.launched_at
                       and i.launched_at - 10 <= hb.boot and not (i.user_id and i.user_id != hb.user_id)]
            if len(waiting) >= 1:
                inst = max(waiting, key=lambda i: i.launched_at)
                self.bindings[hb.user_id] = inst.pkg
                inst.user_id, inst.user = hb.user_id, inst.user or hb.user
                res[inst.pkg] = hb
                log.info("bound user %s to %s", hb.user_id, inst.pkg)
        return res

    # ---- housekeeping ---------------------------------------------------------------------------------------
    def _housekeeping(self, now: float) -> None:
        if not self.detector_installed and now - self.last_detector_install > 600:
            self.install_detector()
        if self.detector_installed and not any(i.last_hb for i in self.instances.values()) \
                and now - self.last_discovery > 300 and any(i.status == "rejoining" for i in self.instances.values()):
            self._discover_workspace(now)
        if self.cfg["autoGrid"] and now >= self.retile_at:
            self._retile()
        rot = int(self.cfg.get("focusRotateS") or 0)
        if rot and now - self.last_focus >= rot:
            self._rotate_focus(now)
        if now - self.last_mem_clean > 120:
            used, total = self.dev.ram_mb()
            if total and (total - used) / total < 0.12:
                self.dev.drop_caches()
                self.last_mem_clean = now
                self.rep.log(f"Memory low ({total - used} MB free), cleaned caches", "warn")
        if self.cfg["closeOtherApps"] and now - self.last_close > 300:
            self.last_close = now
            keep = set(self.instances) | {"com.termux", "com.termux.boot", "com.termux.api"}
            closed = self.dev.close_other_apps(keep)
            if closed:
                self.rep.log(f"Closed {len(closed)} other app(s)", "plain")
        if self.cfg["statusEvery"] and now - self.last_status_post >= 1800:
            self.last_status_post = now
            ing = sum(1 for i in self.instances.values() if i.status == "ingame")
            self.discord.send("Status", f"{ing}/{len(self.instances)} in game", "ok" if ing == len(self.instances) else "warn",
                              [(f"{i.letter.upper()} {mask(i.user)}", i.status) for i in self.instances.values()])
        if now - self.last_device_report >= 30:
            self.last_device_report = now
            used, total = self.dev.ram_mb()
            place = next((i.place_id for i in self.instances.values() if i.status == "ingame" and i.place_id), None)
            self.rep.device({"ramUsed": used, "ramTotal": total, "game": game_name(place) or "—"})

    def _discover_workspace(self, now: float) -> None:
        self.last_discovery = now
        txt = self.dev.out("timeout 15 find /storage/emulated/0 -maxdepth 4 -type d -name RejoinHub 2>/dev/null", 25)
        for line in txt.splitlines():
            ws = line.strip().rsplit("/RejoinHub", 1)[0]
            if ws and ws not in self.workspaces:
                self.workspaces.append(ws)
                self.rep.log(f"Detector files found in {ws}", "info")

    def _retile(self) -> None:
        live = tuple(sorted(i.pkg for i in self.instances.values() if i.enabled and self.dev.alive(i.pkg)))
        self.retile_at = self.clock() + 60
        if not live or live == self.tiled_set:
            return
        size = self.dev.screen_size()
        if not size:
            return
        landscape = self.cfg["orientation"] != "Portrait"
        self.dev.set_rotation(landscape)
        rects = grid.layout(len(live), size[0], size[1], landscape)
        tasks = self.dev.task_ids()
        ok = 0
        for pkg, rect in zip(live, rects):
            tid = tasks.get(pkg)
            if tid is not None and self.dev.resize_task(tid, rect):
                ok += 1
        if ok == len(live):
            self.tiled_set = live
        elif ok == 0 and len(live) > 0:
            self.rep.log("Could not arrange windows on this Android version", "warn")
            self.tiled_set = live  # don't retry every minute; a new launch retries

    def _rotate_focus(self, now: float) -> None:
        live = [i for i in self.instances.values() if i.status == "ingame"]
        self.last_focus = now
        if len(live) < 2:
            return
        self.focus_idx = (self.focus_idx + 1) % len(live)
        self.dev.focus(live[self.focus_idx].pkg, bool(self.cfg["autoGrid"]) and self.freeform_ready)

    # ---- reporting ------------------------------------------------------------------------------------------
    def _report(self, inst: Instance) -> None:
        now = self.clock()
        up = inst.uptime_today_s + (now - inst.since if inst.status == "ingame" and inst.since else 0)
        data = {
            "pkg": inst.pkg,
            "user": mask(inst.user) if inst.user else "—",
            "status": inst.status,
            "since": int(inst.since * 1000) if inst.since else None,
            "rejoins": inst.rejoins,
            "rejoinsToday": inst.rejoins_today,
            "uptimeTodayMin": int(up // 60),
            "lastError": inst.last_error,
            "lastErrorAt": int(inst.last_error_at * 1000) if inst.last_error_at else None,
            "target": inst.target or {"kind": "place", "value": inst.place_id or ""},
        }
        self.rep.account(inst.letter, data)

    def letters(self) -> list[str]:
        return [i.letter for i in self.instances.values()]


def parse_command(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    if t not in ("rejoin", "stop", "start", "target", "reload-config"):
        return None
    letter = raw.get("letter")
    if letter is not None and not re.match(r"^[A-Za-z0-9_-]{1,24}$", str(letter)):
        return None
    return {"type": t, "letter": letter, "target": raw.get("target"), "at": raw.get("at")}
