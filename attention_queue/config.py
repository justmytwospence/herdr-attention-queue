"""User config: HERDR_PLUGIN_CONFIG_DIR/config.json (every key optional).

    {"usage": true, "claude_names": true}
"""

import json
import os


def load() -> dict:
    directory = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not directory:
        return {}
    try:
        with open(os.path.join(directory, "config.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled(name: str, default: bool = True) -> bool:
    value = load().get(name, default)
    return value if isinstance(value, bool) else default
