"""Transition log: one JSON line per attention change, for the notifier.

Hooks append under the session lock to `sessions/<session>/transitions.jsonl`;
the file rotates to `transitions.jsonl.1` at 256 KB. `follow` prints lines newer
than a timestamp (from the rotated file too) and then keeps following the log
across rotations, so a notifier can read it locally or over ssh.

Line kinds:

  attn   {"v", "kind": "attn", "ts_ms", "pane_id", "terminal_id", "workspace_id",
          "tab_id", "agent", "label", "workspace", "attn", "prev", "restoring"}
          `prev` is null on the first sighting of an agent. `workspace` is
          where the agent is, as on its row (`homelab › navigation` when the
          workspace has several agents), and is looked up only for blocked
          and done.
  focus  {"v", "kind": "focus", "ts_ms", "pane_id"}: the pane got focus on its
          server.
  ping   {"v", "kind": "ping", "ts_ms"}: written by `follow` (not the log) every
          30 s, so both ends of an ssh pipe notice when the other is gone.
"""

import json
import os
import sys
import time
from typing import IO, Callable, Iterable, Optional

VERSION = 1
NAME = "transitions.jsonl"
MAX_BYTES = 256 * 1024
HEARTBEAT_S = 30.0


def path(session_dir: str) -> str:
    return os.path.join(session_dir, NAME)


def append(session_dir: str, lines: Iterable[dict], max_bytes: int = MAX_BYTES) -> None:
    """Append lines, rotating first if the log is over max_bytes. Call under the lock."""
    data = "".join(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n" for line in lines)
    if not data:
        return
    target = path(session_dir)
    try:
        if os.path.getsize(target) > max_bytes:
            os.replace(target, target + ".1")
    except OSError:
        pass
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)


def _newer(line: str, since_ms: int) -> bool:
    try:
        return int(json.loads(line).get("ts_ms") or 0) > since_ms
    except (ValueError, AttributeError, TypeError):
        return False


def follow(
    session_dir: str,
    since_ms: int,
    out: IO[str] = sys.stdout,
    poll_s: float = 0.25,
    stop: Optional[Callable[[], bool]] = None,
    heartbeat_s: float = HEARTBEAT_S,
) -> int:
    """Print log lines newer than since_ms, then follow the log until stop()."""
    target = path(session_dir)
    stop = stop or (lambda: False)
    last_write = [time.monotonic()]

    def heartbeat() -> None:
        if heartbeat_s and time.monotonic() - last_write[0] >= heartbeat_s:
            out.write(json.dumps({"v": VERSION, "kind": "ping", "ts_ms": int(time.time() * 1000)}) + "\n")
            out.flush()
            last_write[0] = time.monotonic()

    def emit(line: str) -> None:
        if not line.endswith("\n"):
            return
        if _newer(line, since_ms):
            out.write(line)
            out.flush()
            last_write[0] = time.monotonic()

    try:
        with open(target + ".1") as old:
            for line in old:
                emit(line)
    except OSError:
        pass

    fh = None
    partial = ""
    while not stop():
        if fh is None:
            try:
                fh = open(target)
            except OSError:
                heartbeat()
                time.sleep(poll_s)
                continue
            partial = ""
        chunk = fh.readline()
        if chunk:
            partial += chunk
            if partial.endswith("\n"):
                emit(partial)
                partial = ""
            continue
        # At the end of this file: switch if the log rotated underneath us.
        try:
            rotated = os.stat(target).st_ino != os.fstat(fh.fileno()).st_ino
        except OSError:
            rotated = True
        if rotated:
            rest = fh.read()
            for line in (partial + rest).splitlines(True):
                emit(line)
            fh.close()
            fh = None
            continue
        heartbeat()
        time.sleep(poll_s)
    if fh is not None:
        fh.close()
    return 0


def attn_line(t, attn: str, prev: Optional[str], restoring: bool, workspace: Optional[str], label: Optional[str], ts_ms: int) -> dict:
    return {
        "v": VERSION,
        "kind": "attn",
        "ts_ms": ts_ms,
        "pane_id": t.pane_id,
        "terminal_id": t.terminal_id,
        "workspace_id": t.workspace_id,
        "tab_id": t.tab_id,
        "agent": t.agent,
        "label": label,
        "workspace": workspace,
        "attn": attn,
        "prev": prev,
        "restoring": restoring,
    }


def focus_line(pane_id: str, ts_ms: int) -> dict:
    return {"v": VERSION, "kind": "focus", "ts_ms": ts_ms, "pane_id": pane_id}
