"""Commands for agent harnesses: report state into the pane's `activity` token.

    attention.py activity blocked|working|idle|clear
    attention.py ask-check [--report] [--else working|idle|clear]

Both run inside an agent's pane (harness hooks, the pi bridge), so herdr's pane
environment says where to report. Outside herdr they do nothing. They read the
hook's JSON on stdin, never fail the hook, and return quickly: the plugin is
asked to show the change in a detached process.
"""

import json
import os
import subprocess
import sys
import time
from typing import Optional

from . import ask, herdr, model

SOURCE_SUFFIX = ".activity"
# A report outlives no agent for long: SessionStart hooks clear it, and herdr
# drops an exited agent's pane anyway.
TTL_MS = 24 * 3600 * 1000
STATES = model.ACTIVITIES + ("clear",)


def in_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_PANE_ID"))


def read_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def refresh() -> None:
    """Ask the plugin to reconcile now: a token change emits no plugin event."""
    binary = os.environ.get("HERDR_BIN_PATH")
    if not binary:
        return
    try:
        subprocess.Popen(
            [binary, "plugin", "action", "invoke", herdr.plugin_id() + ".refresh"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError:
        pass


def report(state: str) -> bool:
    """Set (or clear) this pane's `activity` token. False when it could not."""
    if state not in STATES or not in_herdr():
        return False
    params = {
        "pane_id": os.environ["HERDR_PANE_ID"],
        "source": herdr.source() + SOURCE_SUFFIX,
        "seq": time.time_ns(),
        "tokens": {model.ACTIVITY_TOKEN: None if state == "clear" else state},
    }
    if state != "clear":
        params["ttl_ms"] = TTL_MS
    try:
        herdr.call("pane.report_metadata", params)
    except (herdr.Unavailable, herdr.HerdrError):
        return False
    refresh()
    return True


def activity(state: str) -> int:
    read_stdin()  # drain the hook payload; harnesses may block on a full pipe
    if state not in STATES:
        print("usage: attention.py activity %s" % "|".join(STATES), file=sys.stderr)
        return 0
    report(state)
    return 0


def parse_payload(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def ask_check(args) -> int:
    """Judge the finished turn on stdin; print the verdict as JSON.

    --report sets `activity=blocked` when the turn asks the user something,
    otherwise the --else state (if given). One hook doing both keeps a single
    writer per event, so parallel hooks cannot race each other.
    """
    report_it, otherwise = False, None
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag == "--report":
            report_it = True
        elif flag == "--else" and rest and rest[0] in STATES:
            otherwise = rest.pop(0)
        else:
            print("usage: attention.py ask-check [--report] [--else %s]" % "|".join(STATES), file=sys.stderr)
            return 0
    verdict = ask.check(parse_payload(read_stdin()))
    print(json.dumps(verdict))
    sys.stdout.flush()
    if report_it:
        state: Optional[str] = "blocked" if verdict["asks"] else otherwise
        if state:
            report(state)
    return 0
