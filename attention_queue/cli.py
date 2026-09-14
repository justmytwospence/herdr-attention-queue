"""Argument dispatch for the plugin entrypoint."""

import os
import signal
import sys
import traceback

from . import herdr, hooks
from . import store as store_mod

USAGE = "usage: attention.py startup | event | reseed | action <%s>" % "|".join(hooks.ACTIONS)


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
        if mode == "reseed" and len(args) == 1:
            _watchdog(int(sum(hooks.reseed_schedule())) + 60)
            return hooks.reseed()
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
