"""The brain. Looks at the connection, decides whether a captive-portal sign-in is needed, does it.

Same rules as the Android app:
  * It only talks to the IISER gateway, and only if that gateway answers on this network.
    On any other network (home, hotspot...) it stays out of the way. When the internet is down it
    finds the gateway from the portal's own redirect, so a different gateway address (hostel...)
    needs no setup - the certificate still has to be valid for an iisertvm.ac.in name.
  * Automatic sign-in only runs while connected to Wi-Fi (switchable). Buttons always work.
  * Wrong password  ->  ONE attempt, then auto-login is paused until the user changes the
    credentials. It can never hammer the account into a lock-out.
  * Automatic attempts are rate limited, and permanent refusals (login limit, expired...) back off.
"""
from __future__ import annotations

import os
import time
from enum import Enum

from . import notify
from .netinfo import wifi_up
from .portal import Accepted, Failure, PortalClient, Rejected
from .store import KEEP, Store


class Phase(str, Enum):
    IDLE = "idle"
    CHECKING = "checking"
    SIGNING_IN = "signing_in"
    SIGNING_OUT = "signing_out"
    ONLINE = "online"
    NO_GATEWAY = "no_gateway"          # no internet and this isn't the IISER network
    NO_WIFI = "no_wifi"                # "Only on Wi-Fi" is on and this PC isn't on Wi-Fi
    NEEDS_LOGIN = "needs_login"
    BAD_CREDENTIALS = "bad_credentials"
    REJECTED = "rejected"
    ERROR = "error"
    NO_CREDENTIALS = "no_credentials"


class Reason(str, Enum):
    EVENT = "event"            # the network changed
    SCREEN_ON = "screen_on"    # the screen woke up / was unlocked / the PC resumed
    PERIODIC = "periodic"      # the 2-second watch on the campus Wi-Fi
    APP_OPEN = "app_open"
    MANUAL = "manual"


# What each automatic trigger is called in the activity log.
WHY = {
    Reason.EVENT: " when the network changed",
    Reason.SCREEN_ON: " when the screen woke up",
    Reason.PERIODIC: " after the gateway session ended",
}
RETRYABLE = (Reason.EVENT, Reason.SCREEN_ON)       # a freshly joined / resumed network may need a few seconds


class Outcome(str, Enum):
    DONE = "done"
    RETRY = "retry"


MIN_AUTO_GAP = 15          # seconds between automatic login attempts
PERMANENT_BACKOFF = 10 * 60


def client_for(store: Store) -> PortalClient:
    cfg = store.load()
    return PortalClient(cfg.host, cfg.ip, base_override=os.environ.get("IISER_CONNECT_TEST_BASE"))


def check(reason: Reason, store: Store | None = None, client: PortalClient | None = None) -> Outcome:
    store = store or Store()
    with store.lock():
        return _check(reason, store, client)


def _check(reason: Reason, store: Store, client: PortalClient | None) -> Outcome:
    cfg = store.load()
    manual = reason == Reason.MANUAL

    if not (cfg.username and store.password()):
        store.write_status(Phase.NO_CREDENTIALS)
        return Outcome.DONE

    wifi = wifi_up()
    if cfg.wifi_only and not wifi and not manual:       # buttons always work; only the automatic part is Wi-Fi-only
        store.write_status(Phase.NO_WIFI, "Auto sign-in only runs on Wi-Fi", wifi=False, gateway="")
        return Outcome.DONE

    if manual or reason == Reason.APP_OPEN:
        store.write_status(Phase.CHECKING, wifi=wifi)

    client = client or client_for(store)

    probe = client.probe()
    if probe.online:
        # The 2-second watch skips the gateway look-up; every other trigger refreshes it, because
        # it also tells the watcher whether this network is the campus one at all.
        prev = store.read_status()
        gateway = KEEP
        if reason != Reason.PERIODIC or prev.gateway is None:
            gateway = client.host if client.portal_reachable() else ""
        if manual:
            store.log("info", "Already online - nothing to do")
        detail = prev.detail if prev.phase == Phase.ONLINE.value and prev.detail else "Internet is reachable"
        store.write_status(Phase.ONLINE, detail, wifi=wifi, gateway=gateway)
        return Outcome.DONE

    client.adopt(probe.redirect)                      # the portal's redirect says where the gateway is here
    if not client.portal_reachable():
        store.write_status(Phase.NO_GATEWAY, "No internet, and the IISER gateway isn't reachable from this network",
                           wifi=wifi, gateway="")
        if manual:
            store.log("warning", "Gateway not reachable on this network")
        return Outcome.RETRY if reason in RETRYABLE else Outcome.DONE
    store.note_network(wifi=wifi, gateway=client.host)

    # The gateway is here and there's no internet: a sign-in is needed.
    if manual:
        store.update(pause=None)
        cfg = store.load()
    else:
        if not cfg.auto_login or cfg.pause:
            if cfg.pause == "bad_credentials":
                store.write_status(Phase.BAD_CREDENTIALS, "Auto sign-in is paused until you update your password")
            else:
                store.write_status(Phase.NEEDS_LOGIN, "Auto sign-in is paused")
            return Outcome.DONE
        now = time.time()
        if now - cfg.last_auto_attempt < MIN_AUTO_GAP or now - cfg.last_permanent_reject < PERMANENT_BACKOFF:
            return Outcome.DONE
        store.update(last_auto_attempt=now)

    store.write_status(Phase.SIGNING_IN, "Talking to the IISER gateway")
    result = client.login(cfg.username, store.password())

    if isinstance(result, Accepted):
        online = False
        for i in range(3):
            if i:
                time.sleep(1)
            online = client.has_internet()
            if online:
                break
        cfg = store.load()
        store.update(last_login=time.time(), login_count=cfg.login_count + 1)
        store.log("success", f"Signed in as {cfg.username}" if manual else
                  f"Signed in automatically as {cfg.username}{WHY.get(reason, '')}")
        store.write_status(Phase.ONLINE, f"Signed in as {cfg.username}" if online else "Signed in - waiting for the network")
        if cfg.notify:
            notify.send("You're online", f"Signed in to the campus network as {cfg.username}")
        return Outcome.DONE

    if isinstance(result, Rejected):
        store.log("error", f"Gateway: {result.message}")
        if result.bad_credentials:
            store.update(pause="bad_credentials")
            store.write_status(Phase.BAD_CREDENTIALS, result.message)
            if cfg.notify:
                notify.send("Sign-in rejected", "Check your username and password in IISER Connect. Auto sign-in is paused.", urgent=True)
        else:
            if result.permanent:
                store.update(last_permanent_reject=time.time())
            store.write_status(Phase.REJECTED, result.message)
            if cfg.notify and not manual:
                notify.send("The gateway said no", result.message, urgent=True)
        return Outcome.DONE

    assert isinstance(result, Failure)
    store.log("warning", result.error)
    store.write_status(Phase.ERROR, result.error)
    return Outcome.DONE if manual else Outcome.RETRY


def sign_out(store: Store | None = None, client: PortalClient | None = None) -> None:
    store = store or Store()
    with store.lock():
        cfg = store.load()
        client = client or client_for(store)
        store.write_status(Phase.SIGNING_OUT, "Telling the gateway you're done")
        result = client.logout(cfg.username)
        if isinstance(result, Accepted):
            store.update(pause="logged_out")
            store.log("info", "Signed out. Auto sign-in paused until you sign in again")
            store.write_status(Phase.NEEDS_LOGIN, "You're signed out")
        else:
            msg = result.message if isinstance(result, Rejected) else result.error
            store.log("warning", f"Sign-out failed: {msg}")
            store.write_status(Phase.ERROR, msg)
