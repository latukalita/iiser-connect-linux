"""Event sources for the watcher: small helper processes whose output lines mean "something changed".

  network   `nmcli monitor`                              any line: NetworkManager saw a change
  screen    `gdbus monitor --session  org.gnome.ScreenSaver`   ActiveChanged(false): screen on / unlocked
  resume    `gdbus monitor --system   org.freedesktop.login1`  PrepareForSleep(false): back from suspend

Each one is optional. If a tool or D-Bus name isn't there the feed just stays quiet and the 2-second
watch still covers it. A feed that dies (NetworkManager restart...) is brought back automatically.
"""
from __future__ import annotations

import os
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    NETWORK = "network"
    SCREEN_ON = "screen_on"


@dataclass
class Feed:
    name: str
    argv: list[str]
    kind: Kind
    match: str | None = None            # substring a line must contain; None = every line counts
    retry_every: float = 10.0
    proc: subprocess.Popen | None = field(default=None, repr=False)
    _buf: bytes = field(default=b"", repr=False)
    _down_since: float = field(default=0.0, repr=False)

    def start(self) -> bool:
        exe = shutil.which(self.argv[0])
        if not exe:
            self._down_since = time.time() + 3600          # not installed: don't keep looking every few seconds
            return False
        try:
            self.proc = subprocess.Popen([exe, *self.argv[1:]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL)
        except OSError:
            self.proc = None
            self._down_since = time.time()
            return False
        self._buf = b""
        return True

    def fileno(self) -> int:
        return self.proc.stdout.fileno()

    def read(self) -> bool:
        """Consume what's available. True if at least one line counted as an event."""
        try:
            chunk = os.read(self.fileno(), 65536)
        except OSError:
            chunk = b""
        if not chunk:                                       # the helper exited
            self.stop()
            self._down_since = time.time()
            return False
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        self._buf = self._buf[-4096:]
        hit = False
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            if self.match is None or self.match in line:
                hit = True
        return hit

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
            self.proc = None

    def revive_if_due(self) -> bool:
        if self.proc is None and time.time() - self._down_since >= self.retry_every:
            self._down_since = time.time()
            return self.start()
        return False


def default_feeds() -> list[Feed]:
    return [
        Feed("network", ["nmcli", "monitor"], Kind.NETWORK),
        Feed("screen", ["gdbus", "monitor", "--session", "--dest", "org.gnome.ScreenSaver"], Kind.SCREEN_ON,
             match="ActiveChanged (false", retry_every=60.0),
        Feed("resume", ["gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1"], Kind.SCREEN_ON,
             match="PrepareForSleep (false", retry_every=60.0),
    ]


def wait(feeds: list[Feed], timeout: float, wake: int | None = None) -> set[Kind]:
    """Block up to ``timeout`` seconds; return the kinds of events that happened.

    ``wake`` is the read end of a pipe the caller fills (e.g. from a signal handler) to end the wait early.
    """
    sel = selectors.DefaultSelector()
    live = [f for f in feeds if f.proc is not None]
    for f in live:
        sel.register(f.fileno(), selectors.EVENT_READ, f)
    if wake is not None:
        sel.register(wake, selectors.EVENT_READ, None)
    seen: set[Kind] = set()
    try:
        if live or wake is not None:
            for key, _ in sel.select(timeout):
                if key.data is None:
                    try:
                        os.read(wake, 1024)
                    except OSError:
                        pass
                elif key.data.read():
                    seen.add(key.data.kind)
        else:
            time.sleep(timeout)
    finally:
        sel.close()
    for f in feeds:
        f.revive_if_due()
    return seen
