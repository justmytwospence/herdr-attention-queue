"""Claude plan usage on every agent row: the `usage` token.

Reads the same OAuth usage endpoint Claude Code's /status uses, with the token
Claude Code stores (macOS Keychain, or ~/.claude/.credentials.json), and caches
the reply in the plugin state directory. Hooks only ever read the cache; when it
is stale they start one detached refresh, which fetches and then pushes the new
text to every agent row. A hook never waits on the network.

The text matches what the pi footer and Claude's status line show, e.g.
`󰥔 38% 7h55m 󰃭 15% 6d 󰁨 8% 󰄔 $154.21/$150 off`:
5-hour block, 7-day window, each model's weekly cap, and extra-usage spend.
"""

import calendar
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Optional, Tuple

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TTL_S = 300
LOCK_STALE_S = 120
FETCH_TIMEOUT_S = 5

ICON_BLOCK = "\U000F0954"  # nf-md-timer_sand
ICON_WEEK = "\U000F00ED"  # nf-md-calendar
ICON_MODEL = "\U000F0068"  # nf-md-auto_fix
ICON_SPEND = "\U000F0114"  # nf-md-cash


def claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def cache_path(state_root: str) -> str:
    return os.path.join(state_root, "usage.json")


def read_cache(state_root: str) -> Tuple[Optional[dict], float]:
    """(data, age in seconds); (None, inf) when there is no usable cache."""
    path = cache_path(state_root)
    try:
        age = time.time() - os.path.getmtime(path)
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, float("inf")
    return (data if isinstance(data, dict) else None), age


def access_token() -> Optional[str]:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token
    raw = None
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                raw = proc.stdout
        except (OSError, subprocess.SubprocessError):
            raw = None
    if not raw:
        try:
            with open(os.path.join(claude_dir(), ".credentials.json")) as f:
                raw = f.read()
        except OSError:
            return None
    try:
        token = (json.loads(raw).get("claudeAiOauth") or {}).get("accessToken")
    except (ValueError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


def fetch(token: str) -> dict:
    request = urllib.request.Request(
        os.environ.get("HERDR_ATTENTION_QUEUE_USAGE_URL") or USAGE_URL,
        headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            # Without a claude-code User-Agent the endpoint is heavily rate limited.
            "User-Agent": "claude-code/2.1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as reply:
        data = json.loads(reply.read().decode())
    if not isinstance(data, dict):
        raise ValueError("usage reply is not an object")
    return data


def backoff_path(state_root: str) -> str:
    return os.path.join(state_root, "usage.failed")


def _note_failure(state_root: str) -> None:
    """A failed fetch (no token, rate limited, offline) waits a full TTL before retrying."""
    try:
        with open(backoff_path(state_root), "w"):
            pass
    except OSError:
        pass


def refresh(state_root: str) -> bool:
    """Fetch and cache the usage reply. False when there is no token or it failed."""
    token = access_token()
    if not token:
        _note_failure(state_root)
        return False
    try:
        data = fetch(token)
    except (OSError, ValueError):
        # Includes HTTP 429: the endpoint rate limits, and retrying on every
        # hook would keep it limited.
        _note_failure(state_root)
        return False
    fd, tmp = tempfile.mkstemp(dir=state_root, prefix=".usage-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, cache_path(state_root))
        try:
            os.unlink(backoff_path(state_root))
        except OSError:
            pass
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def claim_refresh(state_root: str) -> bool:
    """One refresh at a time per host; a lock older than LOCK_STALE_S is abandoned."""
    lock = os.path.join(state_root, "usage.lock")
    try:
        if time.time() - os.path.getmtime(lock) > LOCK_STALE_S:
            os.rmdir(lock)
    except OSError:
        pass
    try:
        os.mkdir(lock)
        return True
    except OSError:
        return False


def release_refresh(state_root: str) -> None:
    try:
        os.rmdir(os.path.join(state_root, "usage.lock"))
    except OSError:
        pass


def _pct(value) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) else None


def until(iso, now: float) -> str:
    """`2026-09-16T04:20:00Z` -> `3h20m` remaining; empty when past or unparseable."""
    if not isinstance(iso, str) or not iso:
        return ""
    try:
        target = calendar.timegm(time.strptime(iso.split(".")[0].rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return ""
    secs = int(target - now)
    if secs <= 0:
        return ""
    if secs >= 86400:
        return "%dd" % (secs // 86400)
    if secs >= 3600:
        return "%dh%dm" % (secs // 3600, secs % 3600 // 60)
    return "%dm" % (secs // 60)


def render(data: Optional[dict], now: Optional[float] = None) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    now = time.time() if now is None else now
    parts = []
    for key, icon in (("five_hour", ICON_BLOCK), ("seven_day", ICON_WEEK)):
        window = data.get(key) if isinstance(data.get(key), dict) else {}
        pct = _pct(window.get("utilization"))
        if pct is None:
            continue
        left = until(window.get("resets_at"), now)
        parts.append("%s %d%%%s" % (icon, pct, " " + left if left else ""))
    scoped = [
        limit
        for limit in data.get("limits") or []
        if isinstance(limit, dict) and limit.get("kind") == "weekly_scoped" and _pct(limit.get("percent")) is not None
    ]
    scoped.sort(key=lambda limit: str(((limit.get("scope") or {}).get("model") or {}).get("display_name") or ""))
    for limit in scoped:
        parts.append("%s %d%%" % (ICON_MODEL, _pct(limit.get("percent"))))
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
    spend_pct = _pct(spend.get("percent"))
    if spend_pct is not None:
        used = (spend.get("used") or {}).get("amount_minor")
        limit = (spend.get("limit") or {}).get("amount_minor")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
            text = "$%.2f/$%.0f" % (used / 100, limit / 100)
        else:
            text = "%d%%" % spend_pct
        if (data.get("extra_usage") or {}).get("is_enabled") is False:
            text += " off"
        parts.append("%s %s" % (ICON_SPEND, text))
    return " ".join(parts) or None


def current(state_root: str, start_refresh) -> Optional[str]:
    """The text to show now; starts a detached refresh when the cache is stale."""
    data, age = read_cache(state_root)
    if age > TTL_S and not _backing_off(state_root) and claim_refresh(state_root):
        start_refresh()
    return render(data)


def _backing_off(state_root: str) -> bool:
    try:
        return time.time() - os.path.getmtime(backoff_path(state_root)) < TTL_S
    except OSError:
        return False
