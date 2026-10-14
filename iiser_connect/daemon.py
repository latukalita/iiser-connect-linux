"""Background watcher.

On the campus Wi-Fi it looks at the connection every 2 seconds: one tiny HTTP request (Google's
generate_204, ~1 KB). The moment that stops answering 204 it asks the engine to sign in - the
engine decides whether that's allowed (see engine.py for the safety rules).

It also reacts at once to a network change, and to the screen turning on / unlocking / the PC
resuming from suspend. Away from the campus network (home, hotspot, no Wi-Fi) it barely does
anything: there is nothing to watch, and a network event wakes it when that changes.
"""
from __future__ import annotations

import fcntl
import os
import signal
import sys
import time

from . import events
from .engine import Outcome, Phase, Reason, check
from .store import Config, Status, Store

POLL_EVERY = 2.0          # on the campus network: sign in again within ~2 s of losing the session
WAITING_EVERY = 10.0      # paused / refused: nothing can happen automatically, just keep the display fresh
NO_GATEWAY_EVERY = 20.0   # no internet and no gateway: probably not campus - look again now and then
ELSEWHERE_EVERY = 120.0   # online, but the gateway isn't here (home Wi-Fi): events do the work
NO_WIFI_EVERY = 30.0      # "Only on Wi-Fi" and no Wi-Fi: events do the work
DEBOUNCE = 2.0            # let a burst of NetworkManager events settle
SCREEN_DEBOUNCE = 0.5     # the screen is on: check right away
EVENT_RETRIES = 3         # a freshly joined / resumed network may need a few seconds before the gateway answers

PAUSED = {Phase.BAD_CREDENTIALS.value, Phase.NEEDS_LOGIN.value, Phase.REJECTED.value, Phase.NO_CREDENTIALS.value}


def schedule(st: Status, cfg: Config) -> tuple[float, Reason]:
    """When to look next, and why. Only the campus network gets the fast 2-second watch."""
    if st.phase == Phase.NO_WIFI.value:
        return NO_WIFI_EVERY, Reason.EVENT
    if st.phase == Phase.NO_GATEWAY.value or st.gateway == "":
        return (NO_GATEWAY_EVERY if st.phase == Phase.NO_GATEWAY.value else ELSEWHERE_EVERY), Reason.EVENT
    if st.phase in PAUSED:
        return WAITING_EVERY, Reason.PERIODIC
    return POLL_EVERY, Reason.PERIODIC


def run() -> int:
    store = Store()
    store.state_dir.mkdir(parents=True, exist_ok=True)
    lock = open(store.state_dir / "daemon.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("iiser-connect daemon is already running", file=sys.stderr)
        return 1

    # SIGTERM/SIGINT end the current wait at once (a plain flag would only be noticed after up to 30 s).
    stop = {"now": False}
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.update(now=True))

    feeds = events.default_feeds()
    for f in feeds:
        f.start()

    pending_at = time.time()          # first check right away
    reason = Reason.EVENT
    retries = EVENT_RETRIES
    print("iiser-connect daemon started", flush=True)

    while not stop["now"]:
        seen = events.wait(feeds, max(0.0, min(pending_at - time.time(), 30.0)), wake_r)
        if stop["now"]:
            break
        if seen:
            if events.Kind.SCREEN_ON in seen:
                pending_at, reason = time.time() + SCREEN_DEBOUNCE, Reason.SCREEN_ON
            else:
                pending_at, reason = time.time() + DEBOUNCE, Reason.EVENT
            retries = EVENT_RETRIES
            continue
        if time.time() < pending_at:
            continue

        try:
            outcome = check(reason, store)
        except Exception as e:                           # never let one bad check kill the watcher
            print(f"check failed: {e!r}", file=sys.stderr, flush=True)
            outcome = Outcome.DONE
        if outcome == Outcome.RETRY and retries > 0:
            retries -= 1
            pending_at = time.time() + 4
        else:
            delay, reason = schedule(store.read_status(), store.load())
            pending_at, retries = time.time() + delay, 0

    for f in feeds:
        f.stop()
    print("iiser-connect daemon stopped", flush=True)
    return 0
