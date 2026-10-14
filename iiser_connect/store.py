"""Where everything lives, and tiny atomic JSON helpers.

  ~/.config/iiser-connect/config.json        settings (no secrets)
  ~/.config/iiser-connect/credentials        the password, mode 0600, directory 0700
  ~/.local/state/iiser-connect/status.json   what the daemon last saw (the GUI watches this)
  ~/.local/state/iiser-connect/activity.json rolling log of the last 40 events
  ~/.local/state/iiser-connect/lock          flock so the daemon, CLI and GUI never overlap

``IISER_CONNECT_DIR=/some/dir`` relocates both trees (used by the tests).
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_HOST = "gateway.iisertvm.ac.in"
DEFAULT_IP = "172.16.31.101"
MAX_LOG = 40


def _dirs() -> tuple[Path, Path]:
    override = os.environ.get("IISER_CONNECT_DIR")
    if override:
        base = Path(override)
        return base / "config", base / "state"
    home = Path.home()
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "iiser-connect"
    state = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state")) / "iiser-connect"
    return cfg, state


def _write_json(path: Path, data, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


@dataclass
class Config:
    username: str = ""
    auto_login: bool = True
    notify: bool = True
    wifi_only: bool = True            # automatic sign-in only while connected to Wi-Fi
    host: str = DEFAULT_HOST
    ip: str = DEFAULT_IP
    pause: str | None = None          # None | "logged_out" | "bad_credentials"
    last_login: float = 0.0
    login_count: int = 0
    # shared rate-limit memory so the daemon, CLI and GUI agree
    last_auto_attempt: float = 0.0
    last_permanent_reject: float = 0.0


@dataclass
class Status:
    phase: str = "idle"
    detail: str = ""
    at: float = field(default_factory=time.time)
    gateway: str | None = None        # gateway host if it answers on this network, "" if not, None = not checked yet
    wifi: bool | None = None          # connected to Wi-Fi? None = not checked yet


KEEP = object()                       # "leave this status field as it was"
HEARTBEAT = 30.0                      # rewrite an unchanged status at most this often


class Store:
    def __init__(self) -> None:
        self.cfg_dir, self.state_dir = _dirs()
        self.config_path = self.cfg_dir / "config.json"
        self.cred_path = self.cfg_dir / "credentials"
        self.status_path = self.state_dir / "status.json"
        self.log_path = self.state_dir / "activity.json"
        self.lock_path = self.state_dir / "lock"

    # -- config ---------------------------------------------------------------------------
    def load(self) -> Config:
        raw = _read_json(self.config_path, {})
        known = {f.name for f in fields(Config)}
        return Config(**{k: v for k, v in raw.items() if k in known})

    def save(self, cfg: Config) -> None:
        self.cfg_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_json(self.config_path, asdict(cfg), 0o600)

    def update(self, **changes) -> Config:
        cfg = self.load()
        for k, v in changes.items():
            setattr(cfg, k, v)
        self.save(cfg)
        return cfg

    # -- credentials ----------------------------------------------------------------------
    def password(self) -> str:
        return _read_json(self.cred_path, {}).get("password", "")

    def has_credentials(self) -> bool:
        return bool(self.load().username and self.password())

    def set_credentials(self, username: str, password: str) -> None:
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cfg_dir, 0o700)
        _write_json(self.cred_path, {"password": password}, 0o600)
        cfg = self.load()
        cfg.username = username.strip()
        cfg.pause = None            # new credentials: let auto-login try again
        cfg.last_permanent_reject = 0.0
        self.save(cfg)

    def forget_everything(self) -> None:
        for p in (self.config_path, self.cred_path, self.status_path, self.log_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    # -- status + activity ----------------------------------------------------------------
    def read_status(self) -> Status:
        raw = _read_json(self.status_path, {})
        return Status(raw.get("phase", "idle"), raw.get("detail", ""), raw.get("at", 0.0),
                      raw.get("gateway"), raw.get("wifi"))

    def write_status(self, phase: str, detail: str = "", *, gateway=KEEP, wifi=KEEP) -> None:
        """Record what we saw. The watcher checks every 2 s, so an unchanged status is NOT rewritten
        (no disk churn, and the window isn't woken up for nothing) except as a slow heartbeat."""
        prev = _read_json(self.status_path, {})
        new = {
            "phase": str(getattr(phase, "value", phase)),
            "detail": detail,
            "gateway": prev.get("gateway") if gateway is KEEP else gateway,
            "wifi": prev.get("wifi") if wifi is KEEP else wifi,
        }
        now = time.time()
        if all(prev.get(k) == v for k, v in new.items()) and now - prev.get("at", 0.0) < HEARTBEAT:
            return
        _write_json(self.status_path, {**new, "at": now})

    def note_network(self, *, wifi, gateway) -> None:
        """Update only the Wi-Fi / gateway facts, leaving phase and message alone."""
        prev = _read_json(self.status_path, {})
        if prev.get("wifi") == wifi and prev.get("gateway") == gateway:
            return
        _write_json(self.status_path, {**prev, "wifi": wifi, "gateway": gateway,
                                       "phase": prev.get("phase", "idle"), "at": time.time()})

    def activity(self) -> list[dict]:
        return _read_json(self.log_path, [])

    def log(self, level: str, text: str) -> None:
        """level: info | success | warning | error"""
        entries = [{"t": time.time(), "l": level, "m": text}] + self.activity()
        _write_json(self.log_path, entries[:MAX_LOG])

    def clear_log(self) -> None:
        _write_json(self.log_path, [])

    # -- cross-process lock ---------------------------------------------------------------
    @contextmanager
    def lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as f:        # "a": never truncate (the window watches this folder)
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
