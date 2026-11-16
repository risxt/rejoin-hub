"""Android operations through the root shell. Every command here is what a rooted cloud phone runs."""
from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass

from .shell import Shell, q

log = logging.getLogger("rh.device")

STD_CLONE_LEN = len("com.roblox.client")
VIEW = "android.intent.action.VIEW"

# Logcat lines that prove a game finished loading (PandoraGuard's verifier table; the Roblox build may change them).
JOIN_OK = [
    (re.compile(r"GameJoinLoadTime.*join_time:"), 100),
    (re.compile(r"onGameLoaded\(\).*SessionReporterState_GameLoaded"), 100),
    (re.compile(r"onGameLoaded: placeId ="), 100),
    (re.compile(r"submitStartGameTask: scrips are loaded"), 90),
    (re.compile(r"UgcExperienceController: join: initialized dm"), 70),
    (re.compile(r"replaceDataModel"), 70),
    (re.compile(r"RenderView created\[\d+]"), 60),
]
JOIN_FAIL = re.compile(r"Error Code.*27[0-9]|You have been kicked|Failed to join game|Failed to connect to game|"
                       r"Connection to server lost|Game server disconnected|Unable to join this experience")
JOIN_NOISE = ("SignalRCoreError", "Internal error", "DFLog::", "Warning:")

# Vendor apps a cloud phone needs to stay reachable. "Close other apps" never touches these.
VENDOR_PREFIXES = ("com.redfinger", "com.cloudemulator", "com.ld", "com.ldmnq", "com.android", "com.google",
                   "com.termux", "android", "com.qualcomm", "com.mediatek", "com.huawei", "com.samsung",
                   "com.baidu.cloud", "com.vmos", "com.gc.", "com.cloud")

LOW_GFX_FLAGS = {
    "FIntDebugFRMQualityLevelOverride": 1,
    "DFFlagTextureQualityOverrideEnabled": True,
    "DFIntTextureQualityOverride": 0,
    "FIntDebugForceMSAASamples": 0,
    "FIntFRMMinGrassDistance": 0,
    "FIntFRMMaxGrassDistance": 0,
    "FIntRenderGrassDetailStrands": 0,
    "DFIntCSGLevelOfDetailSwitchingDistance": 0,
    "DFIntCSGLevelOfDetailSwitchingDistanceL12": 0,
    "DFIntCSGLevelOfDetailSwitchingDistanceL23": 0,
    "DFIntCSGLevelOfDetailSwitchingDistanceL34": 0,
}


@dataclass
class Identity:
    user: str | None
    user_id: str | None


def clone_letter(pkg: str) -> str:
    """The badge letter / database key for a clone. com.roblox.clienp -> 'p'; odd names keep a safe suffix."""
    if pkg.startswith("com.roblox.clien") and len(pkg) == STD_CLONE_LEN:
        return pkg[-1].lower()
    tail = pkg.split("com.roblox.", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_-]", "_", tail).lower()[:24] or "x"


class Device:
    def __init__(self, sh: Shell):
        self.sh = sh
        self._sdk: int | None = None
        self._launchers: dict[str, str] = {}

    def run(self, cmd: str, timeout: float = 30.0) -> tuple[int, str]:
        return self.sh.run(cmd, timeout)

    def out(self, cmd: str, timeout: float = 30.0) -> str:
        return self.sh.run(cmd, timeout)[1].strip()

    # ---- facts ----------------------------------------------------------------------------------------------
    @property
    def sdk(self) -> int:
        if self._sdk is None:
            try:
                self._sdk = int(self.out("getprop ro.build.version.sdk") or 0)
            except ValueError:
                self._sdk = 0
        return self._sdk

    def android(self) -> str:
        return self.out("getprop ro.build.version.release") or "?"

    def model(self) -> str:
        return self.out("getprop ro.product.model") or "Android"

    def ram_mb(self) -> tuple[int, int]:
        """(used, total) in MB, from MemTotal - MemAvailable."""
        txt = self.out("cat /proc/meminfo")
        vals = {k: int(v) for k, v in re.findall(r"^(MemTotal|MemAvailable):\s+(\d+)", txt, re.M)}
        total = vals.get("MemTotal", 0) // 1024
        avail = vals.get("MemAvailable", 0) // 1024
        return max(total - avail, 0), total

    def roblox_packages(self) -> list[str]:
        txt = self.out("pm list packages", 60)
        pkgs = sorted({m for m in re.findall(r"package:([\w.]+)", txt) if "roblox" in m.lower()
                       and not m.lower().startswith(("com.roblox.studio", "com.roblox.robloxstudio"))})
        return sorted(pkgs, key=lambda p: (p != "com.roblox.client", p))

    def identity(self, pkg: str) -> Identity:
        """Read the signed-in Roblox user from the clone's shared_prefs (root only)."""
        txt = self.out(f"cat /data/data/{pkg}/shared_prefs/*.xml 2>/dev/null", 20)
        user = None
        for key in ("username", "Username", "DisplayName", "RobloxUsername"):
            m = re.search(rf'<string name="{key}">([^<]+)</string>', txt)
            if m and m.group(1).strip():
                user = m.group(1).strip()
                break
        uid = None
        for pat in (r'<(?:long|int) name="(?:UserId|userId|user_id|USER_ID)" value="(\d+)"',
                    r'<string name="(?:UserId|userId|user_id|USER_ID)">(\d+)</string>'):
            m = re.search(pat, txt)
            if m:
                uid = m.group(1)
                break
        return Identity(user, uid)

    # ---- processes ------------------------------------------------------------------------------------------
    def pids(self, pkg: str) -> list[int]:
        return [int(x) for x in self.out(f"pidof {q(pkg)}").split() if x.isdigit()]

    def alive(self, pkg: str) -> bool:
        return bool(self.pids(pkg))

    def force_stop(self, pkg: str) -> None:
        self.run(f"am force-stop {q(pkg)}")
        for _ in range(6):
            pids = self.pids(pkg)
            if not pids:
                return
            self.run("kill -9 " + " ".join(map(str, pids)))
            time.sleep(0.5)

    def launcher_component(self, pkg: str) -> str | None:
        if pkg not in self._launchers:
            comp = None
            rc, out = self.run(f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {q(pkg)}")
            for line in out.splitlines()[::-1]:
                if "/" in line and line.strip().startswith(pkg):
                    comp = line.strip()
                    break
            if comp:
                self._launchers[pkg] = comp
            return comp
        return self._launchers[pkg]

    def _mode(self, freeform: bool) -> str:
        return "--windowingMode 5 " if freeform and self.sdk >= 28 else ""

    def open_app(self, pkg: str, freeform: bool) -> bool:
        comp = self.launcher_component(pkg)
        if comp:
            rc, out = self.run(f"am start {self._mode(freeform)}-n {q(comp)}")
            return rc == 0 and "Error" not in out
        rc, _ = self.run(f"monkey -p {q(pkg)} -c android.intent.category.LAUNCHER 1")
        return rc == 0

    def open_link(self, pkg: str, link: str, freeform: bool) -> bool:
        rc, out = self.run(f"am start {self._mode(freeform)}-a {VIEW} -d {q(link)} -p {q(pkg)}")
        return rc == 0 and "Error" not in out

    def launch(self, pkg: str, link: str | None, freeform: bool) -> None:
        """Cold start: open the app first, then hand it the link (the order PandoraGuard proved on devices)."""
        self.run("logcat -c")
        self.open_app(pkg, freeform)
        if link:
            time.sleep(3)
            self.open_link(pkg, link, freeform)

    def focus(self, pkg: str, freeform: bool) -> None:
        comp = self.launcher_component(pkg)
        if comp:
            self.run(f"am start {self._mode(freeform)}-n {q(comp)}")

    def join_verdict(self, pid: int) -> str:
        """'ok' | 'fail' | 'unknown' from the instance's own logcat."""
        txt = self.out(f"logcat -d --pid={pid} -v brief -b main -t 800", 20)
        score, strong = 0, False
        for line in txt.splitlines():
            if any(n in line for n in JOIN_NOISE):
                continue
            if JOIN_FAIL.search(line):
                return "fail"
            for rx, conf in JOIN_OK:
                if rx.search(line):
                    score += conf
                    strong = strong or conf >= 90
            if strong and score >= 300:
                return "ok"
        return "unknown"

    # ---- windows --------------------------------------------------------------------------------------------
    def enable_freeform(self) -> bool:
        """Turn on freeform windows. Returns True when the flag was off before (a reboot may be needed once)."""
        was = self.out("settings get global enable_freeform_support")
        for k in ("enable_freeform_support", "force_resizable_activities", "enable_non_resizable_multi_window"):
            self.run(f"settings put global {k} 1")
        return was.strip() != "1"

    def set_rotation(self, landscape: bool) -> None:
        self.run("settings put system accelerometer_rotation 0")
        self.run(f"settings put system user_rotation {1 if landscape else 0}")

    def screen_size(self) -> tuple[int, int] | None:
        txt = self.out("wm size")
        m = re.search(r"Override size:\s*(\d+)x(\d+)", txt) or re.search(r"(\d+)x(\d+)", txt)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def task_ids(self) -> dict[str, int]:
        """package -> task id of its (top) task."""
        ids: dict[str, int] = {}
        txt = self.out("dumpsys activity activities", 30)
        for line in txt.splitlines():
            if "Task{" not in line and "TaskRecord{" not in line:
                continue
            m = re.search(r"#(\d+)\s.*?\bA=(?:\d+:)?([\w.]+)", line)
            if m and m.group(2) not in ids:
                ids[m.group(2)] = int(m.group(1))
        return ids

    def resize_task(self, task_id: int, rect: tuple[int, int, int, int]) -> bool:
        l, t, r, b = rect
        for cmd in (f"am task resize {task_id} {l} {t} {r} {b}", f"cmd activity task resize {task_id} {l} {t} {r} {b}"):
            rc, out = self.run(cmd)
            if rc == 0 and not re.search(r"Error|Unknown|usage|not found|Exception", out, re.I):
                return True
        return False

    # ---- performance ----------------------------------------------------------------------------------------
    def tune(self) -> None:
        self.run("for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $f; done 2>/dev/null")
        self.run("settings put global low_power 0")
        for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            self.run(f"settings put global {k} 0")

    def drop_caches(self) -> None:
        self.run("sync; echo 3 > /proc/sys/vm/drop_caches")

    def raise_process_limit(self) -> None:
        """Stop Android 12+ from killing clones and the agent (phantom process killer, cached process cap)."""
        if self.sdk >= 31:
            self.run("device_config set_sync_disabled_for_tests persistent")
            self.run("device_config put activity_manager max_phantom_processes 2147483647")
            self.run("settings put global settings_enable_monitor_phantom_procs false")
        self.run("settings put global activity_manager_constants max_cached_processes=256")

    def close_other_apps(self, keep: set[str]) -> list[str]:
        txt = self.out("pm list packages -3", 60)
        closed = []
        running = set(self.out("ps -A -o NAME 2>/dev/null || ps").split())
        for pkg in re.findall(r"package:([\w.]+)", txt):
            if pkg in keep or pkg.startswith(VENDOR_PREFIXES) or "roblox" in pkg.lower():
                continue
            if pkg in running:
                self.run(f"am force-stop {q(pkg)}")
                closed.append(pkg)
        return closed

    def apply_low_gfx(self, pkg: str, on: bool) -> bool:
        """Write (or remove) ClientAppSettings.json with the low graphics flags for one clone."""
        import json
        d = f"/data/data/{pkg}/files/exe/ClientSettings"
        f = f"{d}/ClientAppSettings.json"
        if not on:
            self.run(f"rm -f {q(f)}")
            return True
        owner = self.out(f"stat -c %u:%g /data/data/{q(pkg)}")
        ok = self.write_file(f, json.dumps(LOW_GFX_FLAGS, indent=1))
        if ok and re.match(r"^\d+:\d+$", owner):
            self.run(f"chown -R {owner} {q(d)} && chmod 755 {q(d)} && chmod 644 {q(f)}")
        return ok

    # ---- files ----------------------------------------------------------------------------------------------
    def write_file(self, path: str, content: str) -> bool:
        """Write through base64, so any byte (quotes, newlines, $) is safe. Verified by reading it back."""
        b64 = base64.b64encode(content.encode()).decode()
        d = path.rsplit("/", 1)[0]
        self.run(f"mkdir -p {q(d)} && printf %s {q(b64)} | base64 -d > {q(path)}")
        return self.read_file(path) == content

    def read_file(self, path: str) -> str | None:
        rc, out = self.run(f"cat {q(path)} 2>/dev/null")
        return out if rc == 0 else None

    def exists_dir(self, path: str) -> bool:
        return self.out(f"[ -d {q(path)} ] && echo y") == "y"
