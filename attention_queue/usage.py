"""Claude plan usage on every agent row: the `usage` token.

Reads the same OAuth usage endpoint Claude Code's /status uses, with the token
Claude Code stores (macOS Keychain, or ~/.claude/.credentials.json), or the one
printed by $ATTENTION_QUEUE_CLAUDE_TOKEN_CMD when set (a herdr-machine0 hub,
which holds no Claude Code login, sets it to `spoke token anthropic`), and caches
the reply in the plugin state directory. Hooks only ever read the cache; when it
is stale they start one detached refresh, which fetches and then pushes the new
text to every agent row. A hook never waits on the network.

The text is the one window most likely to stop work (the 5-hour block, the
7-day window, a model's weekly cap, or extra-usage spend), judged by its pace
toward the cap before it resets, e.g. `󰃭 30% 4d`. The leading icon names the
window; a trailing badge marks warn (nearing the cap) or critical (about to
hit it).
"""

import calendar
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import NamedTuple, Optional, Tuple

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TTL_S = 300
LOCK_STALE_S = 120
FETCH_TIMEOUT_S = 5



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


def command_token() -> Optional[str]:
    """The token printed by $ATTENTION_QUEUE_CLAUDE_TOKEN_CMD, if that is set."""
    command = os.environ.get("ATTENTION_QUEUE_CLAUDE_TOKEN_CMD")
    if not command:
        return None
    try:
        proc = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    token = proc.stdout.strip() if proc.returncode == 0 else ""
    return token or None


def access_token() -> Optional[str]:
    if os.environ.get("ATTENTION_QUEUE_CLAUDE_TOKEN_CMD"):
        return command_token()
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
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_time(iso) -> Optional[float]:
    """`2026-09-16T04:20:00.123+00:00` -> epoch seconds (UTC); None when unparseable."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def until(iso, now: float) -> str:
    """`2026-09-16T04:20:00Z` -> `3h20m` remaining; empty when past or unparseable."""
    target = parse_time(iso)
    if target is None:
        return ""
    secs = int(target - now)
    if secs <= 0:
        return ""
    if secs >= 86400:
        return "%dd" % (secs // 86400)
    if secs >= 3600:
        return "%dh%dm" % (secs // 3600, secs % 3600 // 60)
    return "%dm" % (secs // 60)


SESSION_S = 5 * 3600
WEEK_S = 7 * 86400
# A window's pace is trusted once this much of it has passed; before that the
# projection is noise (5% in the first minute "projects" to 1500%).
MIN_HISTORY = 0.2
# Below this much use a fast pace is not yet a problem worth colouring.
PACE_FLOOR_PCT = 30
WARN_PCT = 75
CRITICAL_PCT = 90
# On pace to hit the cap within this long, before the reset: about to be a problem.
CRITICAL_HORIZON_S = 3600
LEVELS = ("ok", "warn", "critical")
# Which window: the token starts with its icon (Nerd Font).
ICON = {
    "session": "\U000F0954",  # nf-md-clock: the 5-hour block
    "week": "\U000F00ED",  # nf-md-calendar: the 7-day window
    "model": "\U000F0068",  # nf-md-auto_fix: a model's weekly cap
    "spend": "\U000F0114",  # nf-md-cash: extra-usage spend
}
# A problem level ends the token with a badge, so display rules can colour it
# with `contains` (they cannot read a number out of the middle of a token).
BADGE = {
    "warn": "\U000F0026",  # nf-md-alert
    "critical": "\U000F0029",  # nf-md-alert_octagon
}


class Window(NamedTuple):
    kind: str  # an ICON key
    pct: int
    resets_at: Optional[str] = None
    length_s: Optional[int] = None
    severity: Optional[str] = None
    detail: Optional[str] = None  # replaces "pct% countdown" (extra-usage spend)


class Assessed(NamedTuple):
    window: Window
    level: str
    score: float


def _severity_level(severity) -> Optional[str]:
    """The API's own severity for a limit, mapped onto ours; None when normal."""
    text = str(severity or "").strip().lower()
    if not text or text in ("normal", "ok", "none", "low"):
        return None
    if any(word in text for word in ("crit", "exceed", "reach", "block", "lock", "max")):
        return "critical"
    return "warn"


def windows(data: dict):
    """Every limit in the usage reply, as Windows."""
    found = []
    seen = set()
    for limit in data.get("limits") or []:
        if not isinstance(limit, dict):
            continue
        pct = _pct(limit.get("percent"))
        if pct is None:
            continue
        kind, group = limit.get("kind"), limit.get("group")
        if kind == "session" or (kind is None and group == "session"):
            window, length = "session", SESSION_S
        elif kind == "weekly_all":
            window, length = "week", WEEK_S
        elif kind == "weekly_scoped":
            window, length = "model", WEEK_S
        else:
            continue
        seen.add(window)
        found.append(Window(window, pct, limit.get("resets_at"), length, limit.get("severity")))
    # Older replies have no `limits`; the top-level windows say the same.
    for key, kind, length in (("five_hour", "session", SESSION_S), ("seven_day", "week", WEEK_S)):
        window = data.get(key) if isinstance(data.get(key), dict) else {}
        pct = _pct(window.get("utilization"))
        if pct is not None and kind not in seen:
            found.append(Window(kind, pct, window.get("resets_at"), length))
    spend = data.get("spend") if isinstance(data.get("spend"), dict) else {}
    extra = data.get("extra_usage") if isinstance(data.get("extra_usage"), dict) else {}
    spend_pct = _pct(spend.get("percent"))
    enabled = extra.get("is_enabled") is not False and spend.get("enabled") is not False
    if spend_pct is not None and enabled:
        used = (spend.get("used") or {}).get("amount_minor")
        cap = (spend.get("limit") or {}).get("amount_minor")
        if isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap > 0:
            detail = "$%.2f/$%.0f" % (used / 100, cap / 100)
        else:
            detail = "%d%%" % spend_pct
        found.append(Window("spend", spend_pct, severity=spend.get("severity"), detail=detail))
    return found


def assess(window: Window, now: float) -> Assessed:
    """How close a window is to stopping work, at its current pace.

    critical: 90% used, or on pace to hit the cap within the hour, before the
    reset. warn: 75% used, or on pace to hit the cap before the reset. Pace
    counts once a fifth of the window has passed and 30% is used. The score,
    for ranking windows, is the use projected at the reset.
    """
    pct = window.pct
    level = "ok"
    score = float(pct)
    reset = parse_time(window.resets_at)
    left = reset - now if reset is not None else None
    if left is not None and window.length_s and 0 < left <= window.length_s:
        elapsed = window.length_s - left
        if elapsed >= MIN_HISTORY * window.length_s and pct > 0:
            rate = pct / elapsed
            score = max(score, pct + rate * left)
            if PACE_FLOOR_PCT <= pct < 100:
                to_cap = (100 - pct) / rate
                if to_cap < left:
                    level = "critical" if to_cap < CRITICAL_HORIZON_S else "warn"
    if pct >= CRITICAL_PCT:
        level = "critical"
    elif pct >= WARN_PCT and level == "ok":
        level = "warn"
    reported = _severity_level(window.severity)
    if reported and LEVELS.index(reported) > LEVELS.index(level):
        level = reported
    return Assessed(window, level, score)


def bottleneck(data, now: Optional[float] = None) -> Optional[Assessed]:
    """The window most likely to stop work: highest level, then highest projection.

    Extra-usage spend only counts once it is a problem itself: it is spent only
    after a plan window runs out, and that window already shows.
    """
    if not isinstance(data, dict):
        return None
    now = time.time() if now is None else now
    ranked = [assess(w, now) for w in windows(data)]
    ranked = [a for a in ranked if a.window.detail is None or a.level != "ok"]
    if not ranked:
        return None
    return max(ranked, key=lambda a: (LEVELS.index(a.level), a.score))


def render(data: Optional[dict], now: Optional[float] = None) -> Optional[str]:
    """`<window icon> <pct>% <reset>[ <badge>]` for the bottleneck window.

    E.g. `\U000F00ED 30% 4d` (7-day window, ok) or `\U000F0954 92% 40m \U000F0029`
    (5-hour block, critical).
    """
    now = time.time() if now is None else now
    worst = bottleneck(data, now)
    if worst is None:
        return None
    w = worst.window
    if w.detail is not None:
        body = w.detail
    else:
        left = until(w.resets_at, now)
        body = "%d%%%s" % (w.pct, " " + left if left else "")
    text = "%s %s" % (ICON[w.kind], body)
    badge = BADGE.get(worst.level)
    return text + " " + badge if badge else text


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
