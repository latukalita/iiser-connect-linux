"""Command line: iiser-connect <command>."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import sys
import time

from . import APP_NAME, __version__
from .engine import Phase, Reason, check, sign_out
from .store import Store

TITLES = {
    Phase.ONLINE: "online", Phase.CHECKING: "checking", Phase.SIGNING_IN: "signing in",
    Phase.SIGNING_OUT: "signing out", Phase.NO_GATEWAY: "no internet (not the IISER network)",
    Phase.NEEDS_LOGIN: "sign-in needed", Phase.BAD_CREDENTIALS: "wrong password - auto sign-in paused",
    Phase.NO_WIFI: "not on Wi-Fi (auto sign-in is Wi-Fi only)",
    Phase.REJECTED: "gateway refused", Phase.ERROR: "can't reach the gateway",
    Phase.NO_CREDENTIALS: "no login saved", Phase.IDLE: "idle",
}


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return time.strftime("today %H:%M", time.localtime(ts)) if time.localtime(ts).tm_yday == time.localtime().tm_yday else f"{s // 3600} h ago"
    return time.strftime("%d %b %H:%M", time.localtime(ts))


def _yn(v, yes: str, no: str) -> str:
    return "not checked yet" if v is None else (yes if v else no)


def daemon_running(store: Store) -> bool:
    try:
        with open(store.state_dir / "daemon.lock", "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def cmd_status(args) -> int:
    store = Store()
    cfg, st = store.load(), store.read_status()
    info = {
        "phase": st.phase, "detail": st.detail, "checked": st.at,
        "wifi": st.wifi, "gateway": st.gateway, "wifi_only": cfg.wifi_only,
        "account": cfg.username, "auto_login": cfg.auto_login, "paused": cfg.pause,
        "last_login": cfg.last_login, "sign_ins": cfg.login_count,
        "daemon_running": daemon_running(store), "version": __version__,
    }
    if args.json:
        print(json.dumps(info, indent=1))
        return 0
    try:
        title = TITLES[Phase(st.phase)]
    except ValueError:
        title = st.phase
    print(f"{APP_NAME} {__version__}")
    print(f"  status      : {title}" + (f"  ({st.detail})" if st.detail else ""))
    print(f"  wi-fi       : {_yn(st.wifi, 'connected', 'not connected')}" + ("  [auto sign-in is Wi-Fi only]" if cfg.wifi_only else ""))
    print(f"  gateway     : {st.gateway if st.gateway else _yn(None if st.gateway is None else False, '', 'not found on this network')}")
    print(f"  account     : {cfg.username or '-'}")
    print(f"  auto sign-in: {'on' if cfg.auto_login else 'off'}" + (f"  [paused: {cfg.pause}]" if cfg.pause else ""))
    print(f"  last sign-in: {_ago(cfg.last_login)}   ({cfg.login_count} total)")
    print(f"  background  : {'running' if info['daemon_running'] else 'NOT running (systemctl --user start iiser-connect)'}")
    return 0


def cmd_login(_args) -> int:
    check(Reason.MANUAL)
    st = Store().read_status()
    print(TITLES.get(Phase(st.phase), st.phase) + (f" - {st.detail}" if st.detail else ""))
    return 0 if st.phase == Phase.ONLINE.value else 1


def cmd_check(_args) -> int:
    check(Reason.APP_OPEN)
    return cmd_status(argparse.Namespace(json=False))


def cmd_logout(_args) -> int:
    sign_out()
    st = Store().read_status()
    print(st.detail or st.phase)
    return 0


def cmd_setup(args) -> int:
    store = Store()
    user = args.user or input("Campus username: ").strip()
    pw = getpass.getpass("Password (not shown): ")
    if not user or not pw:
        print("Both a username and a password are needed.", file=sys.stderr)
        return 2
    store.set_credentials(user, pw)
    print(f"Saved. The password is stored only in {store.cred_path} (mode 600).")
    if not args.no_login:
        return cmd_login(args)
    return 0


def cmd_forget(_args) -> int:
    Store().forget_everything()
    print("Removed your saved login and settings.")
    return 0


def cmd_gui(_args) -> int:
    from .gui.app import main as gui_main
    return gui_main(sys.argv[:1])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="iiser-connect", description="Automatic sign-in for the IISER campus network.")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("daemon", help="run the background watcher (normally started by systemd)")
    sub.add_parser("check", help="check the connection now and sign in if needed (honours pauses)")
    sub.add_parser("login", help="sign in now, even if auto sign-in is paused")
    sub.add_parser("logout", help="sign out of the gateway (pauses auto sign-in)")
    s = sub.add_parser("status", help="show what the watcher last saw")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("setup", help="save your campus username and password")
    s.add_argument("--user")
    s.add_argument("--no-login", action="store_true", help="just save, don't sign in now")
    sub.add_parser("forget", help="delete your saved login and settings")
    sub.add_parser("gui", help="open the window")
    args = p.parse_args(argv)

    if args.cmd == "daemon":
        from .daemon import run
        return run()
    handlers = {"check": cmd_check, "login": cmd_login, "logout": cmd_logout, "status": cmd_status,
                "setup": cmd_setup, "forget": cmd_forget, "gui": cmd_gui, None: cmd_gui}
    return handlers[args.cmd](args)
