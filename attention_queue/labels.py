"""Claude session names as pane titles.

Claude Code names each session (`~/.claude/jobs/<first 8 of the session id>/
state.json`, key `name`). Showing it as the pane title and agent label tells
Claude rows apart ("Claude: wireguard snowflake routing") where herdr would
otherwise show only "claude" and the terminal title.
"""

import json
import os
from typing import Optional

from .usage import claude_dir


def claude_name(session_key: Optional[str]) -> Optional[str]:
    """Name for a `claude:id:<uuid>` session key, if Claude has named it."""
    if not session_key or not session_key.startswith("claude:id:"):
        return None
    session = session_key[len("claude:id:") :]
    if len(session) < 8 or "/" in session:
        return None
    try:
        with open(os.path.join(claude_dir(), "jobs", session[:8], "state.json")) as f:
            name = json.load(f).get("name")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(name, str):
        return None
    name = " ".join(name.split())[:80]
    return name or None
