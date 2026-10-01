"""Install the plugin's pi bridge into pi's extensions directory.

pi loads every file in `<agent dir>/extensions` (`$PI_CODING_AGENT_DIR`, else
~/.pi/agent), which is how herdr installs its own pi integration too. The
plugin keeps one managed file there, `herdr-attention-queue.ts`, a copy of
pi/herdr-attention-bridge.ts that knows where this checkout is (for its Jev
judge). It is written only where pi is set up, rewritten when the bridge
changes, never written over a file the plugin did not write, and removed with
{"pi_bridge": false} in config.json. Running pi sessions pick up a change with
/reload.
"""

import os
import tempfile
from typing import Optional

from . import config

NAME = "herdr-attention-queue.ts"
MARKER = "// managed by herdr-attention-queue"
HEADER = (
    MARKER + "; it rewrites this file when the plugin updates, and removes it\n"
    "// with {\"pi_bridge\": false} in its config.json. Edit the plugin's\n"
    "// pi/herdr-attention-bridge.ts instead.\n"
)
PLACEHOLDER = "__HERDR_ATTENTION_QUEUE_ROOT__"


def agent_dir() -> str:
    return os.environ.get("PI_CODING_AGENT_DIR") or os.path.expanduser("~/.pi/agent")


def target() -> str:
    return os.path.join(agent_dir(), "extensions", NAME)


def rendered(plugin_root: str) -> Optional[str]:
    try:
        with open(os.path.join(plugin_root, "pi", "herdr-attention-bridge.ts")) as f:
            source = f.read()
    except OSError:
        return None
    return HEADER + source.replace(PLACEHOLDER, plugin_root.replace("\\", "\\\\").replace('"', '\\"'))


def _ours(path: str) -> Optional[str]:
    """The installed file's content when the plugin wrote it; "" when absent; None when foreign."""
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    return content if content.startswith(MARKER) else None


def ensure(plugin_root: str) -> str:
    """Install, update or remove the bridge; returns what happened (for logs and tests)."""
    if not os.path.isdir(agent_dir()):
        return "no pi"
    path = target()
    current = _ours(path)
    if current is None:
        return "foreign file"
    if not config.enabled("pi_bridge"):
        if current:
            try:
                os.unlink(path)
            except OSError:
                return "remove failed"
            return "removed"
        return "off"
    want = rendered(plugin_root)
    if want is None:
        return "no source"
    if current == want:
        return "current"
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".herdr-attention-queue-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(want)
        os.replace(tmp, path)
    except OSError:
        return "write failed"
    return "installed" if not current else "updated"
