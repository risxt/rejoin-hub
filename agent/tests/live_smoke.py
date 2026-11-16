"""Live smoke test: the real agent code against the real Firebase project, with a simulated phone.

Usage: RH_PAIR=<RH1.code> python -m tests.live_smoke [seconds]
It registers a device called "pc-test", runs three fake clones for a while (so the dashboard shows them), then removes
everything it wrote under devices/accounts/commands. No real phone is touched.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("RH_HOME", tempfile.mkdtemp(prefix="rh-smoke-"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rejoinhub import agent as ag  # noqa: E402
from rejoinhub import engine as eng  # noqa: E402
from tests.fakes import Clock, Clone, FakeDevice  # noqa: E402


class RealClock(Clock):
    def __call__(self) -> float:
        return time.time()


def main() -> None:
    code = os.environ.get("RH_PAIR", "").strip()
    if not code:
        sys.exit("set RH_PAIR to the pairing code")
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    clock = RealClock()
    dev = FakeDevice(clock, [Clone("com.roblox.clienp", "smoke_alpha", "9001", join_after=12),
                             Clone("com.roblox.clienq", "smoke_bravo", "9002", join_after=18),
                             Clone("com.roblox.clienr", "smoke_charlie", "9003", join_after=24)])
    cfg = ag.pair(code, "PC test", dev)
    print("paired as", cfg["deviceId"])
    a = ag.Agent(cfg, dev)
    stop = a.stop

    def world() -> None:  # the fake phone keeps writing detector files in real time
        while not stop.is_set():
            dev._write_heartbeats()
            time.sleep(1)

    threading.Thread(target=world, daemon=True).start()
    t = threading.Thread(target=a.run, daemon=True)
    t.start()
    time.sleep(seconds)
    print("statuses:", {i.letter: i.status for i in a.engine.instances.values()})
    stop.set()
    a.engine.commands.put({"type": "reload-config"})
    t.join(15)
    time.sleep(3)  # let the writer queue drain
    if os.environ.get("RH_KEEP") != "1":
        for path in (f"accounts/{cfg['deviceId']}", f"commands/{cfg['deviceId']}", f"devices/{cfg['deviceId']}"):
            try:
                a.fb.delete(path)
            except Exception as e:
                print("cleanup", path, e)
        print("cleaned up")


if __name__ == "__main__":
    main()
