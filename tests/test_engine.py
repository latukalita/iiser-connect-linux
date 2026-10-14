import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRICKY_PASSWORD = "p&ss+w%rd#1 é=;"      # characters the old shell script mangled


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["IISER_CONNECT_DIR"] = self.tmp.name
        os.environ["IISER_CONNECT_NO_NOTIFY"] = "1"
        self.port = free_port()
        os.environ["IISER_CONNECT_TEST_BASE"] = f"http://127.0.0.1:{self.port}"
        self.sysnet = Path(self.tmp.name) / "sysnet"
        os.environ["IISER_CONNECT_SYSNET"] = str(self.sysnet)
        self.set_wifi(True)
        self.portal = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "mock_portal.py"), "--port", str(self.port),
             "--user", "alice", "--password", TRICKY_PASSWORD],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.state()
                break
            except OSError:
                time.sleep(0.1)
        from iiser_connect import engine
        from iiser_connect.store import Store
        self.engine, self.store = engine, Store()
        engine.MIN_AUTO_GAP = 0           # rate limit is tested separately

    def set_wifi(self, up: bool):
        """A fake /sys/class/net with one wireless interface (and a wired one that's always up)."""
        (self.sysnet / "wlan0" / "wireless").mkdir(parents=True, exist_ok=True)
        (self.sysnet / "wlan0" / "operstate").write_text("up\n" if up else "down\n")
        (self.sysnet / "eth0").mkdir(exist_ok=True)
        (self.sysnet / "eth0" / "operstate").write_text("up\n")

    def tearDown(self):
        self.portal.kill()
        self.portal.wait()
        self.tmp.cleanup()

    def state(self):
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/admin/state", timeout=2))

    def admin(self, path):
        urllib.request.urlopen(f"http://127.0.0.1:{self.port}/admin/{path}", timeout=2).read()

    def check(self, reason=None):
        return self.engine.check(reason or self.engine.Reason.EVENT, self.store)

    def phase(self):
        return self.store.read_status().phase

    # -----------------------------------------------------------------------------------
    def test_signs_in_with_tricky_password_and_url_encodes_it(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        self.assertEqual(self.phase(), "online")
        self.assertEqual(self.state()["logins_ok"], 1)
        self.assertEqual(self.store.load().login_count, 1)

    def test_online_means_no_login_at_all(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        for _ in range(5):
            self.check(self.engine.Reason.PERIODIC)
        self.assertEqual(self.state()["login_attempts"], 1)       # the old script did 1 per minute, forever

    def test_wrong_password_is_tried_exactly_once_then_paused(self):
        self.store.set_credentials("alice", "wrong")
        for _ in range(6):
            self.check()
        self.assertEqual(self.state()["login_attempts"], 1)
        self.assertEqual(self.phase(), "bad_credentials")
        self.assertEqual(self.store.load().pause, "bad_credentials")

    def test_fixing_the_password_resumes(self):
        self.store.set_credentials("alice", "wrong")
        self.check()
        self.store.set_credentials("alice", TRICKY_PASSWORD)        # user fixes it
        self.check()
        self.assertEqual(self.phase(), "online")
        self.assertEqual(self.state()["login_attempts"], 2)

    def test_session_expiry_triggers_one_new_login(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        self.admin("expire")
        self.check(self.engine.Reason.PERIODIC)
        self.assertEqual(self.state()["logins_ok"], 2)
        self.assertEqual(self.phase(), "online")

    def test_login_limit_rejection_backs_off(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.admin("limit/1")
        for _ in range(4):
            self.check()
        self.assertEqual(self.state()["login_attempts"], 1)
        self.assertEqual(self.phase(), "rejected")
        self.assertIsNone(self.store.load().pause)                  # not a password problem

    def test_not_the_iiser_network_is_left_alone(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.portal.kill()
        self.portal.wait()
        out = self.check(self.engine.Reason.EVENT)
        self.assertEqual(self.phase(), "no_gateway")
        self.assertEqual(out, self.engine.Outcome.RETRY)

    def test_logout_pauses_and_manual_login_resumes(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        self.engine.sign_out(self.store)
        self.assertFalse(self.state()["logged_in"])
        self.check(self.engine.Reason.PERIODIC)                     # paused: must NOT log back in
        self.assertFalse(self.state()["logged_in"])
        self.assertEqual(self.phase(), "needs_login")
        self.check(self.engine.Reason.MANUAL)
        self.assertTrue(self.state()["logged_in"])

    def test_auto_login_off_never_logs_in(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.store.update(auto_login=False)
        self.check()
        self.assertEqual(self.state()["login_attempts"], 0)

    def test_no_credentials(self):
        self.check()
        self.assertEqual(self.phase(), "no_credentials")
        self.assertEqual(self.state()["login_attempts"], 0)

    # ---- Wi-Fi only ---------------------------------------------------------------------
    def test_wifi_only_does_nothing_without_wifi_even_when_offline(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.set_wifi(False)                                         # cable only
        for reason in (self.engine.Reason.EVENT, self.engine.Reason.SCREEN_ON, self.engine.Reason.PERIODIC):
            self.check(reason)
        self.assertEqual(self.state()["login_attempts"], 0)
        self.assertEqual(self.phase(), "no_wifi")
        self.assertIs(self.store.read_status().wifi, False)

    def test_buttons_still_work_without_wifi(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.set_wifi(False)
        self.check(self.engine.Reason.MANUAL)
        self.assertTrue(self.state()["logged_in"])

    def test_wifi_only_can_be_switched_off_for_cable(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.store.update(wifi_only=False)
        self.set_wifi(False)
        self.check()
        self.assertTrue(self.state()["logged_in"])

    def test_wifi_comes_back_and_it_signs_in(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.set_wifi(False)
        self.check()
        self.assertEqual(self.state()["login_attempts"], 0)
        self.set_wifi(True)
        self.check()
        self.assertTrue(self.state()["logged_in"])

    # ---- screen on ----------------------------------------------------------------------
    def test_screen_on_signs_in_when_offline_and_says_why(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check(self.engine.Reason.SCREEN_ON)
        self.assertTrue(self.state()["logged_in"])
        self.assertIn("screen woke up", self.store.activity()[0]["m"])

    def test_screen_on_when_already_online_does_nothing(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        self.check(self.engine.Reason.SCREEN_ON)
        self.assertEqual(self.state()["login_attempts"], 1)

    def test_screen_on_respects_a_sign_out(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        self.engine.sign_out(self.store)
        self.check(self.engine.Reason.SCREEN_ON)
        self.assertFalse(self.state()["logged_in"])                  # you signed out on purpose

    def test_screen_on_retries_while_the_network_wakes_up(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.portal.kill()
        self.portal.wait()
        self.assertEqual(self.check(self.engine.Reason.SCREEN_ON), self.engine.Outcome.RETRY)

    # ---- gateway discovery + status ------------------------------------------------------
    def test_gateway_is_recorded_for_the_window(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        st = self.store.read_status()
        self.assertEqual((st.phase, st.gateway, st.wifi), ("online", "gateway.iisertvm.ac.in", True))

    def test_not_campus_is_recorded_when_online_without_a_gateway(self):
        from iiser_connect.portal import PortalClient
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        client = PortalClient("gateway.iisertvm.ac.in", base_override=f"http://127.0.0.1:{self.port}")
        client.portal_reachable = lambda: False                      # home Wi-Fi: internet yes, gateway no
        self.engine.check(self.engine.Reason.EVENT, self.store, client)
        self.assertEqual(self.store.read_status().gateway, "")
        self.assertEqual(self.phase(), "online")

    def test_the_two_second_watch_does_not_ask_the_gateway_each_time(self):
        from iiser_connect.portal import PortalClient
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        client = PortalClient("gateway.iisertvm.ac.in", base_override=f"http://127.0.0.1:{self.port}")
        asked = []
        client.portal_reachable = lambda: asked.append(1) or True
        for _ in range(5):
            self.engine.check(self.engine.Reason.PERIODIC, self.store, client)
        self.assertEqual(asked, [])

    def test_status_is_not_rewritten_every_two_seconds(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.check()
        before = self.store.status_path.stat().st_mtime_ns
        at = self.store.read_status().at
        for _ in range(5):
            self.check(self.engine.Reason.PERIODIC)
        self.assertEqual(self.store.status_path.stat().st_mtime_ns, before)
        self.assertEqual(self.store.read_status().at, at)

    def test_adopt_only_trusts_iiser_names_and_private_addresses(self):
        from iiser_connect.portal import PortalClient

        def after(url):
            c = PortalClient("gateway.iisertvm.ac.in", "172.16.31.101")
            c.adopt(url)
            return c.host, c.seen_ip

        self.assertEqual(after("http://172.16.40.9:8090/httpclient.html"), ("gateway.iisertvm.ac.in", "172.16.40.9"))
        self.assertEqual(after("https://hostel-gw.iisertvm.ac.in:8090/x"), ("hostel-gw.iisertvm.ac.in", ""))
        for hostile in ("http://evil.example.com:8090/x", "http://iisertvm.ac.in.evil.com/x", "http://evil-iisertvm.ac.in/x",
                        "http://8.8.8.8/x", "http://127.0.0.1/x", "http://169.254.1.1/x", "/httpclient.html", "", None,
                        "http://[::1]/x", "http://user@evil.com/x"):
            self.assertEqual(after(hostile), ("gateway.iisertvm.ac.in", ""), hostile)

    def test_password_is_stored_privately(self):
        self.store.set_credentials("alice", TRICKY_PASSWORD)
        self.assertEqual(stat.S_IMODE(self.store.cred_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.cfg_dir.stat().st_mode), 0o700)
        self.assertNotIn("p&ss", self.store.config_path.read_text())
        self.assertNotIn("p&ss", (self.store.state_dir / "status.json").read_text() if self.store.status_path.exists() else "")

    def test_test_override_refuses_non_loopback(self):
        from iiser_connect.portal import PortalClient
        with self.assertRaises(ValueError):
            PortalClient("gateway.iisertvm.ac.in", base_override="http://evil.example.com:8090")


if __name__ == "__main__":
    unittest.main(verbosity=2)
