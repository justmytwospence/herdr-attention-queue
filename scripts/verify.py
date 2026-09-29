#!/usr/bin/env python3
"""Read-only check of herdr-attention-queue against a live herdr server.

Prints every agent with herdr's status, the plugin's tokens, and the stored
record, then checks the invariants. Exits 1 if any invariant fails.

Socket: $HERDR_SOCKET_PATH, else the socket of $HERDR_SESSION, else the
default session. Example for a named session on another host:

    ssh nuc 'HERDR_SESSION=homelab python3 ~/dotfiles/plugins/herdr-attention-queue/scripts/verify.py'

With --all-machines it checks this server, then runs itself over ssh on every
saved herdr machine (`herdr machine list --json`), with the same path relative
to the home directory, or --remote-script PATH.
"""

import json
import os
import shlex
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
sys.path.insert(0, ROOT)

import attention_queue  # noqa: E402
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


def check() -> int:
    resolve_socket()
    try:
        agents = herdr.list_agents()
        version = herdr.call("ping", {}).get("version")
    except (herdr.Unavailable, herdr.HerdrError) as e:
        print("herdr unavailable: %s" % e)
        return 1
    completions = herdr.reports_completions(herdr.version_tuple(version))
    print(
        "herdr %s, plugin %s, completion_seq %s"
        % (version, attention_queue.__version__, "yes" if completions else "no")
    )
    state_path = os.path.join(store.session_dir(herdr.socket_path(), create=False), "state.json")
    try:
        with open(state_path) as f:
            live = json.load(f).get("live") or {}
    except (OSError, ValueError):
        live = {}

    problems = []
    fmt = "%-10s %-8s %-8s %-4s %-4s %-13s %-6s %s"
    print(fmt % ("pane", "herdr", "attn", "rank", "bg", "attn_ts", "sticky", "session"))
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
                tokens.get(model.BG_TOKEN, "-"),
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
        if tokens.get("attn_icon") != model.ICON.get(attn):
            problems.append("%s: icon %r does not match %s" % (t.pane_id, tokens.get("attn_icon"), attn))
        if attn == "done" and model.raw_status(t.status) in model.BUSY:
            problems.append("%s: done while herdr says %s" % (t.pane_id, t.status))
        if attn == "idle" and model.bg_count(tokens) > 0:
            problems.append("%s: idle with background work pending" % t.pane_id)

    print()
    print("%d agents, state %s" % (len(agents), state_path))
    for problem in problems:
        print("FAIL " + problem)
    if not problems:
        print("ok")
    return 1 if problems else 0


def remote_script() -> str:
    """This script's path relative to home, for the remote shell to expand."""
    here = os.path.abspath(__file__)
    home = os.path.expanduser("~")
    return "~/" + os.path.relpath(here, home) if here.startswith(home + os.sep) else here


def all_machines(script=None) -> int:
    print("== Local")
    failed = check() != 0
    try:
        out = subprocess.run(
            ["herdr", "machine", "list", "--json"], capture_output=True, text=True, timeout=15
        ).stdout
        machines = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print("herdr machine list failed: %s" % e)
        return 1
    for m in machines:
        if not m.get("enabled", True):
            continue
        print("\n== %s (%s, session %s)" % (m.get("label"), m.get("target"), m.get("session")))
        command = "HERDR_SESSION=%s python3 -B %s" % (shlex.quote(str(m.get("session") or "default")), script or remote_script())
        try:
            proc = subprocess.run(
                ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10", str(m["target"]), command],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as e:
            print("ssh failed: %s" % e)
            failed = True
            continue
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            failed = True
            if not proc.stdout:
                print((proc.stderr or "ssh exited %d" % proc.returncode).strip().splitlines()[-1])
    return 1 if failed else 0


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--all-machines"]:
        return all_machines()
    if len(args) == 3 and args[0] == "--all-machines" and args[1] == "--remote-script":
        return all_machines(args[2])
    if args:
        print("usage: verify.py [--all-machines [--remote-script PATH]]", file=sys.stderr)
        return 2
    return check()


if __name__ == "__main__":
    sys.exit(main())
