"""Minimal herdr socket API client (stdlib only).

One request per connection, one JSON line each way. Plugin processes get
HERDR_SOCKET_PATH for the server that spawned them, so a plugin running under a
named session (e.g. `homelab`) talks to that session's server.
"""

import json
import os
import socket
import time
from typing import Dict, List, Optional

from .model import VIEW_LABEL, VIEW_SORT, Truth, session_key

DEFAULT_PLUGIN_ID = "attention-queue"
MAX_REPLY_BYTES = 8 * 1024 * 1024


class Unavailable(Exception):
    """The herdr socket is missing, refused, timed out, or answered garbage."""


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def plugin_id() -> str:
    return os.environ.get("HERDR_PLUGIN_ID") or DEFAULT_PLUGIN_ID


def source() -> str:
    return "plugin:" + plugin_id()


def socket_path() -> str:
    return os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")


def call(method: str, params: dict, timeout: float = 2.0) -> dict:
    request = {
        "id": "%s:%s:%d" % (plugin_id(), method, time.time_ns()),
        "method": method,
        "params": params,
    }
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path())
        sock.sendall((json.dumps(request) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_REPLY_BYTES:
                raise Unavailable("%s: reply too large" % method)
    except OSError as e:
        raise Unavailable("%s: %s" % (method, e))
    finally:
        sock.close()
    line = buf.split(b"\n", 1)[0]
    if not line:
        raise Unavailable("%s: empty reply" % method)
    try:
        reply = json.loads(line.decode())
    except ValueError as e:
        raise Unavailable("%s: unreadable reply: %s" % (method, e))
    error = reply.get("error")
    if error:
        raise HerdrError(str(error.get("code", "error")), str(error.get("message", "")))
    return reply.get("result") or {}


def list_agents() -> List[Truth]:
    agents = []
    for a in call("agent.list", {}).get("agents") or []:
        pane_id = a.get("pane_id")
        if not isinstance(pane_id, str):
            continue
        tokens = a.get("tokens") or {}
        agents.append(
            Truth(
                terminal_id=a.get("terminal_id") or "pane:" + pane_id,
                pane_id=pane_id,
                agent=a.get("agent"),
                session=session_key(a.get("agent_session")),
                status=a.get("agent_status") or "unknown",
                seq=int(a.get("state_change_seq") or 0),
                tokens={k: v for k, v in tokens.items() if isinstance(v, str)},
                completion_seq=_int_or_none(a.get("completion_seq")),
                detection_skipped=bool(a.get("screen_detection_skipped")),
                workspace_id=a.get("workspace_id"),
                tab_id=a.get("tab_id"),
            )
        )
    return agents


def _int_or_none(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def version_tuple(text) -> tuple:
    parts = []
    for piece in str(text or "").split("-")[0].split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def server_version() -> tuple:
    """(major, minor, patch) of the server, () if it does not say."""
    return version_tuple(call("ping", {}).get("version"))


# herdr 0.9.2 added completion_seq to agent.list. It is omitted when None, so
# an older server and "no completion" look alike; the version tells them apart.
COMPLETION_SEQ_VERSION = (0, 9, 2)


def reports_completions(version: tuple) -> bool:
    return version >= COMPLETION_SEQ_VERSION


def matched_rule(pane_id: str) -> Optional[str]:
    """Id of the detection rule that set this agent's state, if screen-detected."""
    explain = call("agent.explain", {"target": pane_id}).get("explain") or {}
    rule = explain.get("matched_rule") if isinstance(explain, dict) else None
    rule_id = rule.get("id") if isinstance(rule, dict) else None
    return rule_id if isinstance(rule_id, str) else None


def report_tokens(
    pane_id: str,
    tokens: Dict[str, Optional[str]],
    seq: int,
    label: Optional[str] = None,
    clear_label: bool = False,
) -> None:
    """Patch this plugin's tokens on a pane. A None value clears that token.

    `label` names a Claude session. Presentation fields are scoped to the
    reporting source, so every report for a named pane carries them again.
    """
    params = {"pane_id": pane_id, "source": source(), "tokens": tokens, "seq": seq}
    if label:
        params.update({"agent": "claude", "title": label, "display_agent": "Claude: " + label})
    elif clear_label:
        params.update({"clear_title": True, "clear_display_agent": True})
    call("pane.report_metadata", params)


def set_view() -> dict:
    return call("agent.view.set", {"source": source(), "label": VIEW_LABEL, "sort": VIEW_SORT})


def clear_view() -> dict:
    return call("agent.view.clear", {"source": source()})
