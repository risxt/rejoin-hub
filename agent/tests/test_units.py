import base64
import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rejoinhub import grid, links  # noqa: E402
from rejoinhub.agent import PairingError, decode_pairing, slug  # noqa: E402
from rejoinhub.device import clone_letter  # noqa: E402
from rejoinhub.engine import parse_command  # noqa: E402
from rejoinhub.keybypass import pick_link  # noqa: E402
from rejoinhub.notify import mask  # noqa: E402
from rejoinhub.shell import RootShell  # noqa: E402


class LinkTests(unittest.TestCase):
    def test_place(self):
        self.assertEqual(links.build_link({"kind": "place", "value": "8737899170"}),
                         "roblox://experiences/start?placeId=8737899170")

    def test_job_needs_place(self):
        j = "1a2b3c4d-1111-2222-3333-444455556666"
        self.assertEqual(links.build_link({"kind": "job", "value": j}, "123456"),
                         f"roblox://experiences/start?placeId=123456&gameInstanceId={j}")
        self.assertEqual(links.build_link({"kind": "job", "value": j, "place": "999"}),
                         f"roblox://experiences/start?placeId=999&gameInstanceId={j}")
        with self.assertRaises(links.TargetError):
            links.build_link({"kind": "job", "value": j})

    def test_private_server_links(self):
        self.assertEqual(links.build_link({"kind": "ps", "value": "https://www.roblox.com/games/123456/Name?privateServerLinkCode=AbC123"}),
                         "roblox://experiences/start?placeId=123456&linkCode=AbC123")
        self.assertEqual(links.build_link({"kind": "ps", "value": "roblox.com/share?code=abcdef&type=Server"}),
                         "https://www.roblox.com/share?code=abcdef&type=Server")
        with self.assertRaises(links.TargetError):
            links.build_link({"kind": "ps", "value": "https://evil.example/share?code=x"})

    def test_empty(self):
        self.assertIsNone(links.build_link(None))
        self.assertIsNone(links.build_link({"kind": "place", "value": ""}))


class GridTests(unittest.TestCase):
    def test_single_never_full_screen(self):
        (r,) = grid.layout(1, 720, 1280)
        self.assertLess((r[2] - r[0]) * (r[3] - r[1]), 1280 * 720 * 0.6)

    def test_fill_left_to_right(self):
        rs = grid.layout(3, 1920, 1080)
        self.assertEqual(len(rs), 3)
        self.assertLess(rs[0][0], rs[1][0])          # 1st and 2nd side by side
        self.assertEqual(rs[0][1], rs[1][1])
        self.assertGreater(rs[2][1], rs[0][1])       # 3rd on the next row
        for l, t, r, b in rs:
            self.assertTrue(0 <= l < r <= 1920 and 0 <= t < b <= 1080)

    def test_many(self):
        rs = grid.layout(10, 720, 1280)
        self.assertEqual(len(rs), 10)
        self.assertEqual(len(set(rs)), 10)


class MiscTests(unittest.TestCase):
    def test_letters(self):
        self.assertEqual(clone_letter("com.roblox.clienp"), "p")
        self.assertEqual(clone_letter("com.roblox.client"), "t")
        self.assertEqual(clone_letter("com.roblox.client.vip2"), "client_vip2")

    def test_pairing(self):
        key = "A" * 40
        raw = {"d": "https://example-app-default-rtdb.asia-southeast1.firebasedatabase.app", "f": key}
        code = "RH2." + base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
        self.assertEqual(decode_pairing(code)["fleetKey"], key)
        with self.assertRaises(PairingError):
            decode_pairing("RH2.garbage!!")
        with self.assertRaises(PairingError):
            decode_pairing("RH1." + code[4:])
        bad = dict(raw, d="https://evil.example.com")
        with self.assertRaises(PairingError):
            decode_pairing("RH2." + base64.urlsafe_b64encode(json.dumps(bad).encode()).decode())
        short = dict(raw, f="abc")
        with self.assertRaises(PairingError):
            decode_pairing("RH2." + base64.urlsafe_b64encode(json.dumps(short).encode()).decode())

    def test_slug_and_mask(self):
        self.assertEqual(slug("RF-01 VIP HK"), "rf-01-vip-hk")
        self.assertEqual(mask("alpha_one"), "alp****ne")
        self.assertEqual(mask("abc"), "a***")

    def test_commands(self):
        self.assertIsNone(parse_command({"type": "rm -rf"}))
        self.assertIsNone(parse_command({"type": "stop", "letter": "p; reboot"}))
        self.assertEqual(parse_command({"type": "stop", "letter": "p"})["letter"], "p")

    def test_key_link(self):
        txt = "noise https://auth.platorelay.com/a?d=QUJDREVGR0g= more"
        self.assertEqual(pick_link(txt), "https://auth.platorelay.com/a?d=QUJDREVGR0g=")
        self.assertIsNone(pick_link("https://auth.platorelay.com/x"))


@unittest.skipUnless(shutil.which("bash"), "bash needed")
class ShellTests(unittest.TestCase):
    def setUp(self):
        self.sh = RootShell(shutil.which("bash"), require_root=False)

    def tearDown(self):
        self.sh.close()

    def test_exit_code_and_output(self):
        self.assertEqual(self.sh.run("echo hi; exit_code() { return 3; }; exit_code"), (3, "hi\n"))

    def test_no_trailing_newline(self):
        self.assertEqual(self.sh.run("printf abc"), (0, "abc"))

    def test_multiline_exact(self):
        rc, out = self.sh.run("printf 'a\\nb\\n'")
        self.assertEqual(out, "a\nb\n")

    def test_timeout_recovers(self):
        rc, _ = self.sh.run("sleep 5", timeout=1)
        self.assertEqual(rc, 124)
        self.assertEqual(self.sh.run("echo back"), (0, "back\n"))

    def test_quotes(self):
        from rejoinhub.shell import q
        evil = "a'b\"c $(reboot) `x`"
        self.assertEqual(self.sh.run(f"printf %s {q(evil)}"), (0, evil))


if __name__ == "__main__":
    unittest.main()
