"""Argument dispatch for the plugin entrypoint."""

import os
import signal
import sys
import traceback

from . import herdr, hooks
from . import store as store_mod

USAGE = (
    "usage: attention.py startup | event | focus | reseed | ticker | usage-refresh | action <%s>\n"
    "       attention.py follow --session NAME [--since-ms N]\n"
    "       attention.py notifier\n"
    "       attention.py activity blocked|working|idle|clear\n"
    "       attention.py ask-check [--report] [--else STATE]" % "|".join(hooks.ACTIONS)
)


def _follow_args(args):
    """(session, since_ms) from `--session NAME [--since-ms N]`, or None."""
    session, since = None, 0
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if not rest:
            return None
        value = rest.pop(0)
        if flag == "--session":
            session = value
        elif flag == "--since-ms" and value.isdigit():
            since = int(value)
        else:
            return None
    return (session, since) if session else None


def _watchdog(seconds: int) -> None:
    """herdr never times out a hook; never hang and hold one of its 32 slots."""

    def expire(signum, frame):
        sys.stderr.write("attention-queue: watchdog expired after %ds\n" % seconds)
        os._exit(3)

    signal.signal(signal.SIGALRM, expire)
    signal.alarm(int(seconds))


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    mode = args[0] if args else None
    try:
        if mode == "event" and len(args) == 1:
            _watchdog(15)
            return hooks.on_event()
        if mode == "startup" and len(args) == 1:
            _watchdog(20)
            return hooks.on_startup()
        if mode == "usage-refresh" and len(args) == 1:
            _watchdog(30)
            return hooks.usage_refresh()
        if mode == "reseed" and len(args) == 1:
            _watchdog(int(sum(hooks.reseed_schedule())) + 60)
            return hooks.reseed()
        if mode == "focus" and len(args) == 1:
            _watchdog(15)
            return hooks.on_focus()
        if mode == "follow" and _follow_args(args[1:]):
            return hooks.follow(*_follow_args(args[1:]))
        if mode == "notifier" and len(args) == 1:
            if sys.platform != "darwin":
                print("attention-queue: the notifier runs on macOS only", file=sys.stderr)
                return 2
            from . import notifier

            return notifier.run_daemon()
        if mode == "ticker" and len(args) == 1:
            _watchdog(hooks.TICKER_MAX_S + 120)
            return hooks.ticker()
        if mode == "activity" and len(args) == 2:
            from . import harness

            _watchdog(10)
            return harness.activity(args[1])
        if mode == "ask-check":
            from . import harness

            _watchdog(15)
            return harness.ask_check(args[1:])
        if mode == "action" and len(args) == 2 and args[1] in hooks.ACTIONS:
            _watchdog(20)
            return hooks.action(args[1])
    except (store_mod.LockTimeout, herdr.Unavailable) as e:
        # Another hook holds the lock for too long, or herdr is gone. The next
        # event reconciles from truth, so this is not a failure.
        if mode == "action":
            print("attention-queue: %s" % e)
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    print(USAGE, file=sys.stderr)
    return 2
