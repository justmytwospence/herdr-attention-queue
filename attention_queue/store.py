"""On-disk state: per-session namespace, advisory lock, atomic JSON.

herdr gives a plugin one state directory per host, shared by every herdr
session on it, so state is namespaced by the session that owns the socket.
Hooks run concurrently; all state changes happen under an fcntl.flock that the
kernel releases if a hook dies.
"""

import contextlib
import errno
import fcntl
import json
import os
import re
import tempfile
import time

PLUGIN_ID = "attention-queue"
STATE_VERSION = 1
GC_MAX_AGE_NS = 30 * 24 * 3600 * 10**9
GC_MAX_SESSIONS = 500

_SESSION_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class LockTimeout(Exception):
    pass


def session_name(sock_path: str) -> str:
    """`homelab` for .../sessions/homelab/herdr.sock, else `default`."""
    directory = os.path.dirname(sock_path)
    if os.path.basename(os.path.dirname(directory)) != "sessions":
        return "default"
    name = os.path.basename(directory)
    if name in (".", "..") or not _SESSION_RE.fullmatch(name):
        return "default"
    return name


def state_root() -> str:
    root = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if root:
        return root
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "herdr", "plugins", PLUGIN_ID)


def session_dir(sock_path: str, create: bool = True) -> str:
    path = os.path.join(state_root(), "sessions", session_name(sock_path))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "report_seq": 0,
        "boot": {"started_ns": 0, "restore_until_ns": 0},
        "expect": [],
        "live": {},
        "sessions": {},
    }


def try_lock(path: str):
    """Non-blocking exclusive flock; returns the fd or None if held elsewhere."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    return fd


def gc_sessions(state: dict, now_ns: int) -> None:
    sessions = state.get("sessions") or {}
    live_keys = {r.get("session") for r in (state.get("live") or {}).values()}
    for key in [
        k
        for k, v in sessions.items()
        if k not in live_keys and now_ns - int(v.get("seen_ns") or 0) > GC_MAX_AGE_NS
    ]:
        del sessions[key]
    excess = len(sessions) - GC_MAX_SESSIONS
    if excess > 0:
        stale = sorted(
            (k for k in sessions if k not in live_keys),
            key=lambda k: int(sessions[k].get("seen_ns") or 0),
        )
        for key in stale[:excess]:
            del sessions[key]


class Store:
    def __init__(self, directory: str):
        self.dir = directory
        self.state_path = os.path.join(directory, "state.json")
        self.lock_path = os.path.join(directory, "lock")

    @contextlib.contextmanager
    def locked(self, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while True:
            fd = try_lock(self.lock_path)
            if fd is not None:
                break
            if time.monotonic() >= deadline:
                raise LockTimeout(self.lock_path)
            time.sleep(0.02)
        try:
            yield
        finally:
            os.close(fd)

    def load(self) -> dict:
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                raise ValueError("unexpected state version")
        except FileNotFoundError:
            return empty_state()
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                os.replace(self.state_path, "%s.corrupt-%d" % (self.state_path, time.time_ns()))
            return empty_state()
        state = empty_state()
        state.update(data)
        return state

    @staticmethod
    def dumps(state: dict) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def save(self, state: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=self.dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(self.dumps(state))
            os.replace(tmp, self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def save_if_changed(self, state: dict, before: str) -> bool:
        if self.dumps(state) == before:
            return False
        self.save(state)
        return True
