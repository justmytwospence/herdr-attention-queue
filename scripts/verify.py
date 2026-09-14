#!/usr/bin/env python3
"""Read-only check of herdr-attention-queue against a live herdr server.

Prints every agent with herdr's status, the plugin's tokens, and the stored
record, then checks the invariants. Exits 1 if any invariant fails.

Socket: $HERDR_SOCKET_PATH, else the socket of $HERDR_SESSION, else the
default session. Example for a named session on another host:

    ssh nuc 'HERDR_SESSION=homelab python3 ~/dotfiles/plugins/herdr-attention-queue/scripts/verify.py'
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
sys.path.insert(0, ROOT)

from attention_queue import herdr, model, store  # noqa: E402


def resolve_socket() -> None:
    if os.environ.get("HERDR_SOCKET_PATH"):
        return
    config = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr")
    session = os.environ.get("HERDR_SESSION")
    if session and session != "default":
        path = os.path.join(config, "sessions", session, "herdr.sock")
    else:
        path = os.path.join(config, "herdr.sock")
    os.environ["HERDR_SOCKET_PATH"] = path


def main() -> int:
    resolve_socket()
    try:
        agents = herdr.list_agents()
    except herdr.Unavailable as e:
        print("herdr unavailable: %s" % e)
        return 1
    state_path = os.path.join(store.session_dir(herdr.socket_path(), create=False), "state.json")
    try:
        with open(state_path) as f:
            live = json.load(f).get("live") or {}
    except (OSError, ValueError):
        live = {}

    problems = []
    fmt = "%-10s %-8s %-8s %-4s %-13s %-6s %s"
    print(fmt % ("pane", "herdr", "attn", "rank", "attn_ts", "sticky", "session"))
    for t in agents:
        tokens = t.tokens
        rec = live.get(t.terminal_id) or {}
        attn = tokens.get("attn")
        print(
            fmt
            % (
                t.pane_id,
                t.status,
                attn or "-",
                tokens.get("attn_rank", "-"),
                tokens.get("attn_ts", "-"),
                rec.get("sticky", "-"),
                t.session or "-",
            )
        )
        if not all(k in tokens for k in model.TOKEN_NAMES):
            problems.append("%s: missing tokens %s" % (t.pane_id, sorted(tokens)))
            continue
        if model.RANK.get(attn) != tokens.get("attn_rank"):
            problems.append("%s: rank %s does not match %s" % (t.pane_id, tokens.get("attn_rank"), attn))
        if len(tokens.get("attn_ts", "")) != 13 or not tokens["attn_ts"].isdigit():
            problems.append("%s: malformed attn_ts %r" % (t.pane_id, tokens.get("attn_ts")))
        if attn == "done" and model.raw_status(t.status) in model.BUSY:
            problems.append("%s: done while herdr says %s" % (t.pane_id, t.status))

    print()
    print("%d agents, state %s" % (len(agents), state_path))
    for problem in problems:
        print("FAIL " + problem)
    if not problems:
        print("ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
