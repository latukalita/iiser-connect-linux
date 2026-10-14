"""Runs the real daemon process against the mock portal, with stand-ins for `nmcli` and `gdbus`
(so no test depends on this PC's real network, screen or Wi-Fi)."""
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

SCREEN_ON = "/org/gnome/ScreenSaver: org.gnome.ScreenSaver.ActiveChanged (false,)"
SCREEN_OFF = "/org/gnome/ScreenSaver: org.gnome.ScreenSaver.ActiveChanged (true,)"
RESUME = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)"
GOING_TO_SLEEP = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)"
NM_EVENT = "Connectivity is now 'portal'"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.sysnet = Path(self.tmp.name) / "sysnet"
        self.env = dict(os.environ, IISER_CONNECT_DIR=self.tmp.name, IISER_CONNECT_NO_NOTIFY="1", IISER_CONNECT_SYSNET=str(self.sysnet),
                        IISER_CONNECT_TEST_BASE=f"http://127.0.0.1:{self.port}")
        self.set_wifi(True)
        self.portal = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "mock_portal.py"), "--port", str(self.port),
             "--user", "bob", "--password", "right"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.state()
                break
            except OSError:
                time.sleep(0.1)
        os.environ["IISER_CONNECT_DIR"] = self.tmp.name
        from iiser_connect.store import Store
        self.store = Store()
        self.daemon = None

    def tearDown(self):
        if self.daemon:
            self.daemon.terminate()
            try:
                self.daemon.wait(5)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
        self.portal.kill()
        self.portal.wait()
        self.tmp.cleanup()

    def set_wifi(self, up: bool):
        (self.sysnet / "wlan0" / "wireless").mkdir(parents=True, exist_ok=True)
        (self.sysnet / "wlan0" / "operstate").write_text("up\n" if up else "down\n")

    def state(self):
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/admin/state", timeout=2))

    def admin(self, p):
        urllib.request.urlopen(f"http://127.0.0.1:{self.port}/admin/{p}", timeout=2).read()

    def fake_tools(self, network=None, screen=None, resume=None):
        """Stand-ins for `nmcli monitor` and `gdbus monitor` that print one line after N seconds (or never)."""
        d = Path(self.tmp.name) / "bin"
        d.mkdir(exist_ok=True)

        def tool(name, body):
            f = d / name
            f.write_text("#!/bin/bash\n" + body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)

        def after(delay, line):
            return (f"sleep {delay}\necho {json.dumps(line)}\n" if delay is not None else "") + "exec sleep 600\n"

        tool("nmcli", after(*network) if network else "exec sleep 600\n")
        tool("gdbus", 'if [[ "$*" == *--session* ]]; then\n' + (after(*screen) if screen else "exec sleep 600\n") +
             "else\n" + (after(*resume) if resume else "exec sleep 600\n") + "fi\n")
        return d

    def start(self, tools=None, **intervals):
        """Start the daemon. ``intervals`` override its timing constants; ``quiet=True`` switches polling off."""
        quiet = intervals.pop("quiet", False)
        if quiet:
            intervals = {k: 1000 for k in ("POLL_EVERY", "WAITING_EVERY", "NO_GATEWAY_EVERY", "ELSEWHERE_EVERY",
                                           "NO_WIFI_EVERY")} | intervals
        env = dict(self.env, PATH=f"{tools or self.fake_tools()}:{os.environ['PATH']}")
        patch = "".join(f"d.{k}={v}; " for k, v in intervals.items())
        code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); import iiser_connect.daemon as d, iiser_connect.engine as e; "
                f"{patch}e.MIN_AUTO_GAP=1; sys.exit(d.run())")
        self.daemon = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_for(self, cond, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cond():
                return time.time() - t0
            time.sleep(0.1)
        return None

    def first_sign_in(self):
        self.store.set_credentials("bob", "right")
        self.assertIsNotNone(self.wait_for(lambda: self.state()["logged_in"], 10), "no first sign-in")

    # -----------------------------------------------------------------------------------
    def test_two_second_watch_recovers_from_expiry(self):
        self.start()                                              # real defaults: 2 s
        self.first_sign_in()
        self.admin("expire")
        t = self.wait_for(lambda: self.state()["logins_ok"] >= 2, 15)
        self.assertIsNotNone(t, "daemon didn't recover after the session expired")
        print(f"\n  signed back in {t:.1f}s after the session expired (2 s watch)")
        self.assertLess(t, 6)

    def test_network_event_triggers_immediately_not_by_polling(self):
        self.start(self.fake_tools(network=(4, NM_EVENT)), quiet=True)
        self.first_sign_in()
        self.admin("expire")
        t = self.wait_for(lambda: self.state()["logins_ok"] >= 2, 12)
        self.assertIsNotNone(t, "daemon ignored the NetworkManager event")
        print(f"\n  signed back in {t:.1f}s after expiry, driven purely by the NM event")

    def test_screen_on_signs_in_when_the_internet_is_off(self):
        self.start(self.fake_tools(screen=(4, SCREEN_ON)), quiet=True)
        self.first_sign_in()
        self.admin("expire")                                      # internet drops while the screen is off...
        t = self.wait_for(lambda: self.state()["logins_ok"] >= 2, 12)
        self.assertIsNotNone(t, "daemon ignored the screen turning on")
        print(f"\n  signed in {t:.1f}s after expiry, driven purely by screen-on")
        self.assertIn("screen woke up", self.store.activity()[0]["m"])

    def test_resume_from_suspend_signs_in(self):
        self.start(self.fake_tools(resume=(4, RESUME)), quiet=True)
        self.first_sign_in()
        self.admin("expire")
        self.assertIsNotNone(self.wait_for(lambda: self.state()["logins_ok"] >= 2, 12), "daemon ignored the resume")

    def test_screen_off_and_going_to_sleep_do_not_trigger(self):
        self.start(self.fake_tools(screen=(3, SCREEN_OFF), resume=(3, GOING_TO_SLEEP)), quiet=True)
        self.first_sign_in()
        self.admin("expire")
        time.sleep(8)
        self.assertEqual(self.state()["logins_ok"], 1)

    def test_screen_on_while_online_does_nothing(self):
        self.start(self.fake_tools(screen=(3, SCREEN_ON)), quiet=True)
        self.first_sign_in()
        time.sleep(6)
        self.assertEqual(self.state()["login_attempts"], 1)

    def test_no_wifi_means_no_automatic_sign_in_until_wifi_returns(self):
        self.set_wifi(False)
        self.store.set_credentials("bob", "right")
        self.start(NO_WIFI_EVERY=1)
        time.sleep(5)
        self.assertEqual(self.state()["login_attempts"], 0)
        self.assertEqual(self.store.read_status().phase, "no_wifi")
        self.set_wifi(True)
        self.assertIsNotNone(self.wait_for(lambda: self.state()["logged_in"], 8), "didn't sign in once Wi-Fi was back")

    def test_wrong_password_one_attempt_only_even_with_constant_polling(self):
        self.store.set_credentials("bob", "WRONG")
        self.start(POLL_EVERY=1, WAITING_EVERY=1)
        time.sleep(10)
        self.assertEqual(self.state()["login_attempts"], 1)
        self.assertEqual(self.store.read_status().phase, "bad_credentials")

    def test_only_one_daemon_at_a_time(self):
        self.start(quiet=True)
        time.sleep(1.5)
        second = subprocess.run([sys.executable, "-m", "iiser_connect", "daemon"], env=self.env, cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 1)
        self.assertIn("already running", second.stderr)


class ScheduleTests(unittest.TestCase):
    """Only the campus network gets the fast watch; everywhere else it stays out of the way."""

    def sched(self, phase, gateway):
        from iiser_connect import daemon
        from iiser_connect.store import Config, Status
        return daemon.schedule(Status(phase=phase, gateway=gateway), Config())

    def test_campus_is_watched_every_two_seconds(self):
        from iiser_connect.daemon import POLL_EVERY
        self.assertEqual(POLL_EVERY, 2.0)
        for phase in ("online", "error", "signing_in"):
            self.assertEqual(self.sched(phase, "gateway.iisertvm.ac.in")[0], 2.0, phase)
        self.assertEqual(self.sched("online", None)[0], 2.0)       # not looked up yet: the engine will do it

    def test_elsewhere_is_barely_touched(self):
        self.assertGreaterEqual(self.sched("online", "")[0], 60)       # home Wi-Fi with internet
        self.assertGreaterEqual(self.sched("no_gateway", "")[0], 10)   # no internet, no gateway
        self.assertGreaterEqual(self.sched("no_wifi", "")[0], 30)      # cable only

    def test_nothing_to_do_automatically_means_a_calm_pace(self):
        for phase in ("bad_credentials", "needs_login", "rejected", "no_credentials"):
            self.assertGreaterEqual(self.sched(phase, "gateway.iisertvm.ac.in")[0], 10, phase)


if __name__ == "__main__":
    unittest.main(verbosity=2)
