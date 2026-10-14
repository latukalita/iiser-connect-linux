"""Desktop notifications via notify-send (silently skipped if unavailable)."""
from __future__ import annotations

import os
import shutil
import subprocess

from . import APP_NAME


def send(title: str, text: str, urgent: bool = False, timeout_ms: int = 6000) -> None:
    exe = shutil.which("notify-send")
    if not exe or os.environ.get("IISER_CONNECT_NO_NOTIFY"):     # the env switch is for the test-suite
        return
    cmd = [exe, "-a", APP_NAME, "-i", "network-wireless", "-u", "normal" if urgent else "low"]
    if timeout_ms and not urgent:
        cmd += ["-t", str(timeout_ms)]
    try:
        subprocess.run(cmd + [title, text], timeout=5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
