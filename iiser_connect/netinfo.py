"""Is this PC on Wi-Fi right now?  Read straight from sysfs: no subprocess, no D-Bus, microseconds.

``IISER_CONNECT_SYSNET=/some/dir`` points at a fake ``/sys/class/net`` (used by the tests).
"""
from __future__ import annotations

import os
from pathlib import Path


def _root() -> Path:
    return Path(os.environ.get("IISER_CONNECT_SYSNET", "/sys/class/net"))


def wifi_up() -> bool:
    """True when at least one wireless interface is associated with an access point."""
    root = _root()
    try:
        names = os.listdir(root)
    except OSError:
        return False
    for name in names:
        d = root / name
        if (d / "wireless").is_dir() or (d / "phy80211").exists():
            try:
                if (d / "operstate").read_text().strip() == "up":
                    return True
            except OSError:
                continue
    return False
