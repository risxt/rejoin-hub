"""`rh` command: pair, run, stop, status, doctor."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys

from . import VERSION


def _setup_logging(to_file: bool) -> None:
    from .agent import HOME
    HOME.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if to_file:
        handlers.append(logging.handlers.RotatingFileHandler(HOME / "agent.log", maxBytes=1_000_000, backupCount=2,
                                                             encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
                        datefmt="%m-%d %H:%M:%S", handlers=handlers)


def _root_device():
    from .device import Device
    from .shell import RootShell, ShellError
    try:
        return Device(RootShell())
    except ShellError as e:
        sys.exit(f"Root needed: {e}")


def cmd_pair(a: argparse.Namespace) -> None:
    from .agent import PairingError, pair
    dev = _root_device()
    try:
        cfg = pair(a.code, a.name, dev)
    except PairingError as e:
        sys.exit(f"Pairing failed: {e}")
    print(f"Paired as '{cfg['name']}' (id {cfg['deviceId']}).")


def cmd_run(a: argparse.Namespace) -> None:
    from .agent import PIDFILE, Agent, load_config
    _setup_logging(True)
    cfg = load_config()
    if not cfg.get("fleetKey"):
        sys.exit("Not paired yet. Run: rh pair <code from the dashboard>")
    dev = _root_device()
    PIDFILE.write_text(str(os.getpid()))
    agent = Agent(cfg, dev)

    def bye(*_: object) -> None:
        agent.stop.set()
        agent.engine.commands.put({"type": "reload-config"})  # wake the loop

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    try:
        agent.run()
    finally:
        agent.engine.save_state()
        try:
            PIDFILE.unlink()
        except OSError:
            pass


def _running_pid() -> int | None:
    from .agent import PIDFILE
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def cmd_stop(a: argparse.Namespace) -> None:
    from .agent import HOME
    (HOME / "stopped").write_text("1")  # tells the keep-alive loop not to restart
    pid = _running_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        print("Agent stopped.")
    else:
        print("Agent was not running.")


def cmd_status(a: argparse.Namespace) -> None:
    from .agent import load_config
    cfg = load_config()
    pid = _running_pid()
    print(f"Rejoin Hub agent {VERSION}")
    print(f"  paired:  {'yes, as ' + cfg.get('name', '?') + ' (' + cfg.get('deviceId', '?') + ')' if cfg.get('fleetKey') else 'no'}")
    print(f"  running: {'yes (pid ' + str(pid) + ')' if pid else 'no'}")


def cmd_doctor(a: argparse.Namespace) -> None:
    """Read-only check of everything the agent depends on. Safe to run any time."""
    from . import keybypass
    from .device import clone_letter
    from .engine import EXECUTORS
    dev = _root_device()
    ok = lambda b: "OK " if b else "-- "
    print(f"Rejoin Hub doctor {VERSION}")
    print(f"{ok(True)}root shell works")
    print(f"    Android {dev.android()} (SDK {dev.sdk}), model {dev.model()}")
    used, total = dev.ram_mb()
    print(f"    RAM {used}/{total} MB used")
    pkgs = dev.roblox_packages()
    print(f"{ok(bool(pkgs))}Roblox packages: {len(pkgs)}")
    for p in pkgs:
        ident = dev.identity(p)
        print(f"    {clone_letter(p).upper():>3}  {p}  user={ident.user or '?'} id={ident.user_id or '?'}  running={dev.alive(p)}"
              f"  launcher={dev.launcher_component(p) or '?'}")
    for name, auto_dir, ws in EXECUTORS:
        if dev.exists_dir(auto_dir) or dev.exists_dir(ws):
            print(f"{ok(dev.exists_dir(auto_dir))}{name}: autoexec={dev.exists_dir(auto_dir)} workspace={dev.exists_dir(ws)}")
    print(f"    Delta license: {keybypass.license_state(dev)}")
    print(f"    freeform flag: {dev.out('settings get global enable_freeform_support')}, "
          f"resizable: {dev.out('settings get global force_resizable_activities')}")
    size = dev.screen_size()
    print(f"{ok(bool(size))}screen: {size}")
    tasks = dev.task_ids()
    print(f"    task ids: { {k: v for k, v in tasks.items() if 'roblox' in k} or 'none running' }")
    rc, out = dev.run("am task 2>&1 | head -5")
    print(f"    'am task' help: {out.strip().splitlines()[0] if out.strip() else 'none'}")
    print(f"    phantom process limit: {dev.out('device_config get activity_manager max_phantom_processes') or 'n/a'}")
    print("Done. Send a screenshot of this to Claude if something shows '--'.")


def cmd_windows(a: argparse.Namespace) -> None:
    """Read-only window diagnosis: is freeform supported, which task-resize command works (tested on a running clone)."""
    dev = _root_device()
    def show(title: str, cmd: str) -> None:
        print(f"--- {title}")
        print(dev.out(cmd, 30)[:1500])
    print(f"Rejoin Hub windows check {VERSION} (Android {dev.android()}, SDK {dev.sdk})")
    show("settings", "for k in enable_freeform_support force_resizable_activities enable_non_resizable_multi_window "
         "force_desktop_mode_on_external_displays; do echo \"$k=$(settings get global $k)\"; done")
    show("freeform feature", "pm list features | grep -iE 'freeform|picture_in_picture|multi' || echo 'no freeform feature listed'")
    show("config_freeformWindowManagement (dumpsys)", "dumpsys activity settings 2>/dev/null | head -5; "
         "dumpsys window | grep -m3 -iE 'freeform|mSupportsFreeform|supportsFreeform' || echo none")
    tasks = dev.task_ids()
    roblox = {p: t for p, t in tasks.items() if "roblox" in p}
    print(f"--- roblox tasks: {roblox or 'none running (open one clone first)'}")
    show("task modes", "dumpsys activity activities | grep -E 'Task[{]|TaskRecord[{]|mWindowingMode|windowingMode=' "
         "| grep -iE -B1 -A1 'roblox' | head -30")
    for pkg, tid in roblox.items():
        for cmd in (f"am task resize {tid} 0 0 640 360", f"cmd activity task resize {tid} 0 0 640 360",
                    f"am task resize {tid} 0,0,640,360"):
            rc, out = dev.run(cmd)
            print(f"--- try: {cmd}")
            print(f"rc={rc} {out.strip()[:400]}")
        break
    show("stack list", "am stack list 2>&1 | head -25")
    print("Done. Send a screenshot of everything above to Claude.")


def main() -> None:
    p = argparse.ArgumentParser(prog="rh", description="Rejoin Hub phone agent")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pair", help="connect this phone using the code from the dashboard")
    s.add_argument("code")
    s.add_argument("--name", help="phone name shown in the dashboard")
    s.set_defaults(fn=cmd_pair)
    sub.add_parser("run", help="run the agent in the foreground").set_defaults(fn=cmd_run)
    sub.add_parser("stop", help="stop the background agent").set_defaults(fn=cmd_stop)
    sub.add_parser("status", help="show pairing and run state").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="read-only device check").set_defaults(fn=cmd_doctor)
    sub.add_parser("windows", help="diagnose window tiling (tries resizing a running clone)").set_defaults(fn=cmd_windows)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
