import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rejoinhub import engine as eng  # noqa: E402
from rejoinhub import keybypass  # noqa: E402
from tests.fakes import Clock, Clone, FakeDevice, MemReporter  # noqa: E402

eng.game_name = lambda place: "Test Game" if place else None


def make(clones=None, **kw):
    clock = Clock()
    clones = clones or [Clone("com.roblox.clienp", "alpha_one", "101"), Clone("com.roblox.clienq", "bravo_two", "202"),
                        Clone("com.roblox.clienr", "charlie_3", "303")]
    dev = FakeDevice(clock, clones, **kw)
    rep = MemReporter()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tmp.close()
    os.unlink(tmp.name)
    e = eng.Engine(dev, rep, tmp.name, clock=clock, sleep=clock.sleep)
    e.apply_config({"checkS": 10, "stuckS": 45, "restartWaitS": 5, "launchGapS": 8})
    return e, dev, rep, clock


def run_for(e, dev, seconds, step=10):
    end = dev.clock.t + seconds
    while dev.clock.t < end:
        dev.advance(step, step=1.0)
        e.tick()
        while not e.commands.empty():
            e.handle_command(e.commands.get_nowait())


def statuses(rep):
    return {k: v["status"] for k, v in rep.accounts.items()}


class EngineTests(unittest.TestCase):
    def test_fresh_start_all_join_staggered(self):
        e, dev, rep, clock = make()
        e.setup()
        run_for(e, dev, 120)
        self.assertEqual(statuses(rep), {"p": "ingame", "q": "ingame", "r": "ingame"})
        launches = [c for c in dev.calls if c.startswith("launch")]
        self.assertEqual(len(launches), 3)
        # each clone bound to its own Roblox user through the username in shared_prefs
        self.assertEqual(e.bindings, {"101": "com.roblox.clienp", "202": "com.roblox.clienq", "303": "com.roblox.clienr"})

    def test_crash_relaunches_after_wait(self):
        e, dev, rep, clock = make()
        e.setup()
        run_for(e, dev, 120)
        dev.crash("com.roblox.clienq")
        run_for(e, dev, 10)
        self.assertEqual(statuses(rep)["q"], "crashed")
        run_for(e, dev, 60)
        self.assertEqual(statuses(rep)["q"], "ingame")
        self.assertEqual(rep.accounts["q"]["rejoins"], 2)
        self.assertIn("Roblox closed or crashed, restarting", rep.texts("q"))

    def test_disconnect_error_rejoins(self):
        e, dev, rep, clock = make()
        e.setup()
        run_for(e, dev, 120)
        dev.clones["com.roblox.clienp"].error = {"kind": "disconnect", "code": 277, "text": "Lost connection", "ts": int(clock.t)}
        run_for(e, dev, 10)
        self.assertIn("Roblox error: disconnect (code 277)", rep.texts("p"))
        run_for(e, dev, 60)
        self.assertEqual(statuses(rep)["p"], "ingame")
        self.assertEqual(rep.accounts["p"]["rejoins"], 2)

    def test_teleport_failure_recovers_when_job_changes(self):
        e, dev, rep, clock = make()
        e.setup()
        run_for(e, dev, 120)
        c = dev.clones["com.roblox.clienp"]
        c.error = {"kind": "teleport_failed", "code": 769, "text": "x", "ts": int(clock.t)}
        run_for(e, dev, 10)
        c.error, c.job = None, "99999999-2222-3333-4444-555555555555"
        dev.files.pop(f"{dev.WS}/RejoinHub/hb_101.json")
        run_for(e, dev, 30)
        self.assertIn("Recovered by itself after teleport_failed", rep.texts("p"))
        self.assertEqual(rep.accounts["p"]["rejoins"], 1)

    def test_frozen_detector_goes_lobby_then_rejoins(self):
        e, dev, rep, clock = make()
        e.setup()
        run_for(e, dev, 120)
        dev.clones["com.roblox.clienr"].frozen = True
        run_for(e, dev, 40)
        self.assertIn(statuses(rep)["r"], ("lobby", "rejoining"))
        run_for(e, dev, 90)
        self.assertEqual(statuses(rep)["r"], "ingame")
        self.assertTrue(any(t.startswith("Rejoining: stuck for") for t in rep.texts("r")))

    def test_never_joins_triggers_key_refresh_then_rejoin_all(self):
        e, dev, rep, clock = make(license="")
        dev.clones["com.roblox.clienp"].join_after = None
        e.izen_key = "k"
        e.setup()
        calls = []

        def fake_refresh(d, pkgs, key):
            calls.append(key)
            dev.clones["com.roblox.clienp"].join_after = 20
            return keybypass.KeyResult(True, "new Delta key written")

        with mock.patch.object(keybypass, "refresh", fake_refresh):
            run_for(e, dev, 200)
            for t in list(__import__("threading").enumerate()):
                if t.name == "key-refresh":
                    t.join(5)
            run_for(e, dev, 120)
        self.assertEqual(calls, ["k"])
        self.assertTrue(any(t.startswith("New Delta key written") for t in rep.texts()))
        self.assertEqual(statuses(rep)["p"], "ingame")

    def test_key_refresh_failure_falls_back_to_relaunch(self):
        e, dev, rep, clock = make(license="")
        dev.clones["com.roblox.clienp"].join_after = None
        e.setup()
        with mock.patch.object(keybypass, "refresh", lambda d, p, k: keybypass.KeyResult(False, "no Delta key link found")):
            run_for(e, dev, 200)
            for t in list(__import__("threading").enumerate()):
                if t.name == "key-refresh":
                    t.join(5)
            run_for(e, dev, 200)
        self.assertIn("Key refresh failed: no Delta key link found", rep.texts())
        self.assertTrue(any("did not reach the game" in t for t in rep.texts("p")))

    def test_banned_is_not_rejoined(self):
        e, dev, rep, clock = make()
        e.setup()
        run_for(e, dev, 120)
        dev.clones["com.roblox.clienq"].error = {"kind": "banned", "code": 0, "text": "You are banned", "ts": int(clock.t)}
        run_for(e, dev, 60)
        self.assertEqual(statuses(rep)["q"], "offline")
        self.assertFalse(dev.clones["com.roblox.clienq"].running)

    def test_stop_start_target_commands(self):
        e, dev, rep, clock = make()
        e.setup()
        run_for(e, dev, 120)
        e.submit({"type": "stop", "letter": "p"})
        run_for(e, dev, 60)
        self.assertEqual(statuses(rep)["p"], "offline")
        self.assertFalse(dev.clones["com.roblox.clienp"].running)
        e.submit({"type": "start", "letter": "p"})
        run_for(e, dev, 60)
        self.assertEqual(statuses(rep)["p"], "ingame")
        e.submit({"type": "target", "letter": "q", "target": {"kind": "place", "value": "1234567"}})
        run_for(e, dev, 60)
        self.assertEqual(dev.clones["com.roblox.clienq"].links[-1], "roblox://experiences/start?placeId=1234567")
        self.assertEqual(rep.accounts["q"]["target"], {"kind": "place", "value": "1234567"})

    def test_bad_target_rejected(self):
        e, dev, rep, clock = make()
        e.setup()
        run_for(e, dev, 60)
        e.submit({"type": "target", "letter": "q", "target": {"kind": "place", "value": "abc"}})
        run_for(e, dev, 10)
        self.assertTrue(any(t.startswith("Target rejected") for t in rep.texts("q")))

    def test_unknown_user_bound_by_launch_timing(self):
        clones = [Clone("com.roblox.clienp", "", "101")]
        e, dev, rep, clock = make(clones)
        e.setup()
        run_for(e, dev, 60)
        self.assertEqual(e.bindings.get("101"), "com.roblox.clienp")
        self.assertEqual(statuses(rep)["p"], "ingame")

    def test_no_detector_uses_logcat_without_flapping(self):
        e, dev, rep, clock = make(delta=False)
        for c in dev.clones.values():
            c.join_after = None
            dev.logcat_ok.add(c.pkg)
        e.setup()
        run_for(e, dev, 200)
        self.assertEqual(statuses(rep), {"p": "ingame", "q": "ingame", "r": "ingame"})
        launches = [c for c in dev.calls if c.startswith("launch")]
        self.assertEqual(len(launches), 3)

    def test_adopts_running_session(self):
        e, dev, rep, clock = make()
        for c in dev.clones.values():
            c.running, c.launched_at, c.join_after = True, clock.t - 100, 0
        dev.advance(1)
        e.setup()
        run_for(e, dev, 30)
        self.assertEqual(statuses(rep), {"p": "ingame", "q": "ingame", "r": "ingame"})
        self.assertFalse([c for c in dev.calls if c.startswith("launch")])

    def test_backoff_after_repeated_failures(self):
        clones = [Clone("com.roblox.clienp", "alpha_one", "101", join_after=None)]
        e, dev, rep, clock = make(clones, delta=False)  # no Delta: no key refresh possible
        e.setup()
        run_for(e, dev, 900)
        launches = [c for c in dev.calls if c.startswith("launch")]
        self.assertLess(len(launches), 9)  # 150 s join window + growing waits
        self.assertTrue(any("failed joins in a row" in t for t in rep.texts("p")))

    def test_state_survives_restart(self):
        e, dev, rep, clock = make()
        e.setup()
        e.submit({"type": "stop", "letter": "r"})
        run_for(e, dev, 30)
        e2 = eng.Engine(dev, MemReporter(), e.state_path, clock=clock)
        self.assertFalse(e2.instances["com.roblox.clienr"].enabled)
        self.assertEqual(e2.bindings, e.bindings)


if __name__ == "__main__":
    unittest.main()
