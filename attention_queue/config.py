"""User config: HERDR_PLUGIN_CONFIG_DIR/config.json (every key optional).

    {"usage": true, "claude_names": true, "notifier": {...}}

Outside herdr (the notifier LaunchAgent) the directory is the one herdr gives
the plugin: ~/.config/herdr/plugins/config/attention-queue.
"""

import json
import os

PLUGIN_ID = "attention-queue"


def directory() -> str:
    configured = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if configured:
        return configured
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "plugins", "config", PLUGIN_ID)


def load() -> dict:
    directory_ = directory()
    if not directory_:
        return {}
    try:
        with open(os.path.join(directory_, "config.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled(name: str, default: bool = True) -> bool:
    value = load().get(name, default)
    return value if isinstance(value, bool) else default
