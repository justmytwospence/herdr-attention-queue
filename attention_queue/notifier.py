"""macOS notifier for every herdr machine (run as a LaunchAgent on the Mac).

It follows the transition log of the local server directly and of every saved
herdr machine over ssh (`attention.py follow`), and sends a clickable alerter
notification when an agent becomes blocked or done. The notification is removed
when the agent leaves that state or its pane gets focus.

A click brings the Ghostty terminal running the herdr client forward (its title
starts with "herdr ", from `ui.window_title`), focuses the agent's pane on its
own server, and, when the client shows another machine, switches machines by
sending herdr's focus-agent key (prefix, then alt+N) for the agent's position N
in the combined Agents list. herdr has no API that makes a client switch
machines; the key is the one thing that does.

Everything that touches the system goes through `SystemIO`, so `Notifier` and
the helpers here are testable without macOS.
"""

import json
import os
import queue
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, NamedTuple, Optional, Sequence

from . import config, herdr, store as store_mod

LOCAL = "local"
NOTIFY_STATES = ("blocked", "done")
MAX_AGE_MS = 15 * 60 * 1000
MACHINES_REFRESH_S = 60
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 120.0
# follow writes a ping every 30 s; a quiet source is dead.
STALE_S = 95.0
HERDR_TITLE_PREFIX = "herdr "
JUMP_MAX = 9
SSH_OPTIONS = (
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ClearAllForwardings=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "ConnectTimeout=10",
)
ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icons")
ICONS = {
    "claude": "claude-color.png",
    "codex": "codex-color.png",
    "pi": "pi.png",
    "omp": "omp.png",
    "cursor": "cursor.png",
    "copilot": "githubcopilot.png",
    "opencode": "opencode.png",
    "gemini": "gemini-color.png",
    "grok": "grok.png",
    "kimi": "kimi-color.png",
    "devin": "devin-color.png",
    "qwen": "qwen-color.png",
    "kiro": "kiro-color.png",
}


class Machine(NamedTuple):
    key: str  # "local" or the herdr machine profile id
    label: str
    target: Optional[str] = None  # ssh target; None for local
    session: str = "default"

    @property
    def local(self) -> bool:
        return self.key == LOCAL


# -- pure helpers ------------------------------------------------------------


def decide(line: dict, now_ms: int, max_age_ms: int = MAX_AGE_MS) -> Optional[str]:
    """"notify", "remove", or None for one transition-log line."""
    kind = line.get("kind")
    if kind == "focus":
        return "remove"
    if kind != "attn":
        return None
    if line.get("attn") not in NOTIFY_STATES:
        return "remove"
    if line.get("prev") is None or line.get("restoring"):
        # A first sighting (plugin start, new agent) or a restart replay.
        return None
    if line.get("prev") == line.get("attn"):
        return None
    try:
        age = now_ms - int(line.get("ts_ms") or 0)
    except (TypeError, ValueError):
        return None
    return "notify" if age <= max_age_ms else None


def group_for(machine_key: str, pane_id: str) -> str:
    return "herdr-%s-%s" % (machine_key, pane_id)


def agent_name(line: dict) -> str:
    return str(line.get("agent") or "agent")


def content(line: dict, machine: Machine) -> dict:
    """title, subtitle and message for a blocked or done line."""
    where = line.get("workspace") or line.get("workspace_id") or "herdr"
    what = "needs input" if line.get("attn") == "blocked" else "finished"
    label = line.get("label")
    if label:
        message = str(label)
    elif line.get("attn") == "blocked":
        message = "Waiting for your answer or approval."
    else:
        message = "Finished its turn. Open the pane to review."
    return {
        "title": "%s \u00b7 %s %s" % (where, agent_name(line), what),
        "subtitle": None if machine.local else machine.label,
        "message": message,
    }


def icon_for(agent: Optional[str], icon_dir: str = ICON_DIR) -> Optional[str]:
    name = ICONS.get(str(agent or "").lower())
    if not name:
        return None
    path = os.path.join(icon_dir, name)
    return path if os.path.exists(path) else None


def alerter_argv(alerter: str, text: dict, group: str, icon: Optional[str]) -> List[str]:
    argv = [alerter, "--title", text["title"], "--message", text["message"], "--group", group]
    if text.get("subtitle"):
        argv += ["--subtitle", text["subtitle"]]
    if icon:
        argv += ["--app-icon", icon]
    return argv + ["--actions", "Focus", "--close-label", "Dismiss", "--json"]


def clicked(output: str) -> bool:
    """True if alerter's --json result is a click on the notification or Focus."""
    try:
        result = json.loads(output or "{}")
    except ValueError:
        return False
    kind = result.get("activationType")
    if kind == "contentsClicked":
        return True
    return kind == "actionClicked" and result.get("activationValue") == "Focus"


def follow_command(entry: str, session: str, since_ms: int, python: str = "python3") -> List[str]:
    return [python, "-B", entry, "follow", "--session", session, "--since-ms", str(int(since_ms))]


def ssh_argv(target: str, entry: str, session: str, since_ms: int, ssh: str = "ssh") -> List[str]:
    # The remote shell expands ~ in the entry path; everything else is plain.
    remote = " ".join(follow_command(entry, session, since_ms))
    return [ssh] + list(SSH_OPTIONS) + [target, remote]


def _attn_key(agent: dict):
    tokens = agent.get("tokens") or {}
    rank, ts = tokens.get("attn_rank"), tokens.get("attn_ts")
    # herdr sorts a missing token after every present one.
    return (rank is None, rank or "", ts is None, ts or "")


def jump_index(lists: Sequence, machine_key: str, pane_id: str) -> Optional[int]:
    """1-based position of an agent in the client's combined Agents list.

    `lists` is [(machine_key, agents or None)] in the client's machine order
    (Local first, then saved machines); None marks an unreachable machine, which
    the client leaves out of its focus-agent targets too. Rows sort like the
    plugin view (attn_rank, attn_ts); ties keep machine and list order, as
    herdr's stable sort does.
    """
    rows = []
    for m_index, (key, agents) in enumerate(lists):
        if agents is None:
            continue
        for a_index, agent in enumerate(agents):
            rows.append((_attn_key(agent), m_index, a_index, key, agent.get("pane_id")))
    rows.sort(key=lambda row: row[:3])
    for index, row in enumerate(rows):
        if row[3] == machine_key and row[4] == pane_id:
            return index + 1
    return None


def looking_at(front_title: Optional[str], selected: str, focused: Optional[str], machine_key: str, pane_id: str) -> bool:
    """The user is already looking at this pane; every check must pass."""
    return (
        bool(front_title)
        and front_title.startswith(HERDR_TITLE_PREFIX)
        and selected == machine_key
        and focused == pane_id
    )


def applescript_string(text: str) -> str:
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


FRONT_TITLE_SCRIPT = """
if application "Ghostty" is not running then return ""
tell application "Ghostty"
    if not frontmost then return ""
    try
        return name of focused terminal of selected tab of front window
    on error
        return ""
    end try
end tell
"""


def focus_script(prefix: str = HERDR_TITLE_PREFIX) -> str:
    return """
tell application "Ghostty"
    set found to missing value
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in terminals of t
                if name of s starts with %s then
                    set found to s
                    exit repeat
                end if
            end repeat
            if found is not missing value then exit repeat
        end repeat
        if found is not missing value then exit repeat
    end repeat
    if found is missing value then
        activate
        return "activated"
    end if
    focus found
    activate
    return "focused"
end tell
""" % applescript_string(prefix)


# The herdr client turns on the kitty keyboard protocol, and Ghostty's `send key`
# carries no text or codepoint, so under that protocol it emits nothing. `perform
# action "csi:..."` writes the exact sequences a real keypress would produce.
PREFIX_SEQUENCE = "98;5u"  # ctrl+b
ALT_DIGIT_SEQUENCE = "%d;3u"  # alt+<digit>, by the digit's codepoint


def jump_sequences(n: int, prefix: str = PREFIX_SEQUENCE) -> List[str]:
    """Kitty-protocol CSI bodies for the herdr prefix, then alt+N."""
    return [prefix, ALT_DIGIT_SEQUENCE % (ord("0") + n)]


def jump_script(n: int, prefix_sequence: str = PREFIX_SEQUENCE, prefix: str = HERDR_TITLE_PREFIX) -> str:
    """Send the herdr prefix, then alt+N, to the herdr client's terminal."""
    first, second = jump_sequences(n, prefix_sequence)
    return """
tell application "Ghostty"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in terminals of t
                if name of s starts with %(prefix)s then
                    perform action %(first)s on s
                    delay 0.05
                    perform action %(second)s on s
                    return "sent"
                end if
            end repeat
        end repeat
    end repeat
    return "missing"
end tell
""" % {
        "prefix": applescript_string(prefix),
        "first": applescript_string("csi:" + first),
        "second": applescript_string("csi:" + second),
    }


# -- system access -------------------------------------------------------------


class SystemIO:
    """Everything the notifier does to the outside world."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.alerter = settings.get("alerter") or shutil.which("alerter") or "/opt/homebrew/bin/alerter"
        self.herdr = settings.get("herdr") or shutil.which("herdr") or "/opt/homebrew/bin/herdr"
        # CSI body of the herdr prefix key in the kitty protocol; ctrl+b by default.
        self.prefix_sequence = settings.get("prefix_csi") or PREFIX_SEQUENCE

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def run(self, argv: List[str], timeout: float = 10.0) -> Optional[str]:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def osascript(self, script: str, timeout: float = 5.0) -> Optional[str]:
        out = self.run(["/usr/bin/osascript", "-e", script], timeout)
        return out.strip() if out is not None else None

    # notifications
    def notify(self, argv: List[str]) -> subprocess.Popen:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def remove(self, group: str) -> None:
        self.run([self.alerter, "--remove", group], timeout=5)

    # where the user is looking
    def front_title(self) -> Optional[str]:
        return self.osascript(FRONT_TITLE_SCRIPT) or None

    def machines(self) -> Optional[List[dict]]:
        out = self.run([self.herdr, "machine", "list", "--json"], timeout=10)
        try:
            data = json.loads(out) if out else None
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    def selected(self) -> Optional[str]:
        machines = self.machines()
        if machines is None:
            return None
        for m in machines:
            if m.get("selected"):
                return str(m.get("id"))
        return LOCAL

    def local_socket(self, session: str) -> str:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr")
        if session and session != "default":
            return os.path.join(base, "sessions", session, "herdr.sock")
        return os.path.join(base, "herdr.sock")

    def local_call(self, machine: Machine, method: str, params: dict) -> dict:
        previous = os.environ.get("HERDR_SOCKET_PATH")
        os.environ["HERDR_SOCKET_PATH"] = self.local_socket(machine.session)
        try:
            return herdr.call(method, params, timeout=3.0)
        finally:
            if previous is None:
                os.environ.pop("HERDR_SOCKET_PATH", None)
            else:
                os.environ["HERDR_SOCKET_PATH"] = previous

    def agents(self, machine: Machine) -> Optional[List[dict]]:
        try:
            if machine.local:
                result = self.local_call(machine, "agent.list", {})
            else:
                out = self.run([self.herdr, "--machine", machine.key, "agent", "list"], timeout=10)
                result = (json.loads(out) or {}).get("result") or {} if out else None
                if result is None:
                    return None
        except (herdr.Unavailable, herdr.HerdrError, ValueError):
            return None
        agents = result.get("agents")
        return agents if isinstance(agents, list) else None

    def focused_pane(self, machine: Machine) -> Optional[str]:
        agents = self.agents(machine) if machine.local else None
        for a in agents or []:
            if a.get("focused"):
                return a.get("pane_id")
        return None

    # clicking
    def focus_terminal(self) -> Optional[str]:
        return self.osascript(focus_script())

    def focus_agent(self, machine: Machine, pane_id: str) -> bool:
        if machine.local:
            try:
                self.local_call(machine, "agent.focus", {"target": pane_id})
                return True
            except (herdr.Unavailable, herdr.HerdrError):
                return False
        return self.run([self.herdr, "--machine", machine.key, "agent", "focus", pane_id], timeout=10) is not None

    def jump(self, n: int) -> bool:
        return self.osascript(jump_script(n, self.prefix_sequence)) == "sent"


# -- the notifier ------------------------------------------------------------------


class Notifier:
    def __init__(self, io, machines: Sequence[Machine], log=None):
        self.io = io
        self.machines = {m.key: m for m in machines}
        self.order = [m.key for m in machines]
        self.pending: Dict[str, subprocess.Popen] = {}
        self.focused: Dict[str, str] = {}
        self.log = log or (lambda message: None)
        self.lock = threading.Lock()
        self.click_lock = threading.Lock()

    def set_machines(self, machines: Sequence[Machine]) -> None:
        with self.lock:
            self.machines = {m.key: m for m in machines}
            self.order = [m.key for m in machines]

    def handle(self, machine_key: str, line: dict) -> Optional[str]:
        machine = self.machines.get(machine_key)
        pane = line.get("pane_id")
        if machine is None or not isinstance(pane, str):
            return None
        action = decide(line, self.io.now_ms())
        if line.get("kind") == "focus":
            self.focused[machine_key] = pane
        if action == "remove":
            self.withdraw(group_for(machine_key, pane))
        elif action == "notify":
            if self.already_looking(machine, pane):
                self.log("skip %s %s: already looking" % (machine_key, pane))
                return "skipped"
            self.send(machine, line)
        return action

    def already_looking(self, machine: Machine, pane_id: str) -> bool:
        front = self.io.front_title()
        if not front or not front.startswith(HERDR_TITLE_PREFIX):
            return False
        if self.io.selected() != machine.key:
            return False
        focused = self.io.focused_pane(machine) if machine.local else None
        focused = focused or self.focused.get(machine.key)
        return looking_at(front, machine.key, focused, machine.key, pane_id)

    def send(self, machine: Machine, line: dict) -> None:
        group = group_for(machine.key, line["pane_id"])
        # A new notification in the same group replaces the old one.
        self.withdraw(group, remove=False)
        argv = alerter_argv(self.io.alerter, content(line, machine), group, icon_for(line.get("agent")))
        try:
            proc = self.io.notify(argv)
        except OSError as e:
            self.log("alerter failed: %s" % e)
            return
        with self.lock:
            self.pending[group] = proc
        self.log("notify %s %s" % (group, line.get("attn")))
        threading.Thread(target=self._await, args=(group, proc, machine, line["pane_id"]), daemon=True).start()

    def _await(self, group: str, proc, machine: Machine, pane_id: str) -> None:
        try:
            out, _ = proc.communicate()
        except (OSError, ValueError):
            out = ""
        with self.lock:
            if self.pending.get(group) is proc:
                del self.pending[group]
            else:
                return  # withdrawn or replaced
        if clicked(out):
            self.click(machine, pane_id)

    def withdraw(self, group: str, remove: bool = True) -> None:
        """Take back a notification this process sent, if it is still showing."""
        with self.lock:
            proc = self.pending.pop(group, None)
        if proc is None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        if remove:
            self.io.remove(group)

    def click(self, machine: Machine, pane_id: str) -> None:
        with self.click_lock:
            self.log("click %s %s" % (machine.key, pane_id))
            self.io.focus_terminal()
            self.io.focus_agent(machine, pane_id)
            if self.io.selected() == machine.key:
                return
            for attempt in range(2):
                n = jump_index([(k, self.io.agents(self.machines[k])) for k in list(self.order) if k in self.machines], machine.key, pane_id)
                if n is None or n > JUMP_MAX:
                    self.log("no key jump: position %s" % n)
                    return
                self.io.jump(n)
                if self._wait_selected(machine.key):
                    return
            self.log("machine switch to %s not confirmed" % machine.key)

    def _wait_selected(self, key: str, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.io.selected() == key:
                return True
            time.sleep(0.2)
        return False


# -- sources and the daemon ----------------------------------------------------------


def settings() -> dict:
    value = config.load().get("notifier")
    return value if isinstance(value, dict) else {}


def notifier_dir() -> str:
    path = os.path.join(store_mod.state_root(), "notifier")
    os.makedirs(path, exist_ok=True)
    return path


class Progress:
    """Per-machine timestamp of the last line handled, so nothing is sent twice."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.marks: Dict[str, int] = {}
        self.dirty_since = None
        try:
            with open(path) as f:
                data = json.load(f)
            self.marks = {k: int(v) for k, v in data.items() if isinstance(v, int)}
        except (OSError, ValueError, AttributeError):
            pass

    def get(self, key: str) -> int:
        with self.lock:
            return self.marks.get(key, 0)

    def advance(self, key: str, ts_ms: int) -> None:
        with self.lock:
            if ts_ms > self.marks.get(key, 0):
                self.marks[key] = ts_ms
                self.dirty_since = self.dirty_since or time.monotonic()

    def flush(self, force: bool = False) -> None:
        with self.lock:
            if self.dirty_since is None or (not force and time.monotonic() - self.dirty_since < 2):
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.marks, f)
            os.replace(tmp, self.path)
            self.dirty_since = None


def machine_list(io, local_session: str) -> Optional[List[Machine]]:
    raw = io.machines()
    if raw is None:
        return None
    machines = [Machine(LOCAL, "Local", None, local_session)]
    for m in raw:
        if not m.get("enabled", True) or not m.get("id") or not m.get("target"):
            continue
        machines.append(Machine(str(m["id"]), str(m.get("label") or m["target"]), str(m["target"]), str(m.get("session") or "default")))
    return machines


class Source(threading.Thread):
    """Follows one machine's log and puts (machine key, line) on the queue."""

    def __init__(self, machine: Machine, argv_for, lines: "queue.Queue", progress: Progress, log):
        super().__init__(daemon=True, name="source-" + machine.key)
        self.machine = machine
        self.argv_for = argv_for
        self.lines = lines
        self.progress = progress
        self.log = log
        self.stopped = threading.Event()
        self.proc = None

    def stop(self) -> None:
        self.stopped.set()
        proc = self.proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def run(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self.stopped.is_set():
            since = max(self.progress.get(self.machine.key), int(time.time() * 1000) - MAX_AGE_MS)
            started = time.monotonic()
            got = self.follow(self.argv_for(self.machine, since))
            if self.stopped.is_set():
                break
            if got or time.monotonic() - started > 60:
                backoff = BACKOFF_MIN_S
            self.log("source %s disconnected; retry in %.0fs" % (self.machine.key, backoff))
            self.stopped.wait(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 2)

    def follow(self, argv: List[str]) -> bool:
        got = False
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.log("source %s: %s" % (self.machine.key, e))
            return False
        stream = self.proc.stdout
        buf = b""
        last = time.monotonic()
        try:
            while not self.stopped.is_set():
                ready, _, _ = select.select([stream], [], [], 1.0)
                if not ready:
                    if time.monotonic() - last > STALE_S:
                        self.log("source %s: stale" % self.machine.key)
                        break
                    continue
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    break
                last = time.monotonic()
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    try:
                        line = json.loads(raw.decode())
                    except ValueError:
                        continue
                    if isinstance(line, dict) and line.get("kind") != "ping":
                        got = True
                        self.lines.put((self.machine.key, line))
        finally:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            stream.close()
        return got


def run_daemon() -> int:
    """The LaunchAgent entry point: `attention.py notifier`."""
    options = settings()
    io = SystemIO(options)
    directory = notifier_dir()
    log_path = os.path.join(directory, "notifier.log")

    def log(message: str) -> None:
        try:
            if os.path.getsize(log_path) > 256 * 1024:
                os.replace(log_path, log_path + ".1")
        except OSError:
            pass
        with open(log_path, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))

    local_session = str(options.get("local_session") or "default")
    local_entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "attention.py")
    home = os.path.expanduser("~")
    default_remote = "~/" + os.path.relpath(local_entry, home) if local_entry.startswith(home + os.sep) else local_entry
    remote_entries = options.get("remote_entry") or {}

    def argv_for(machine: Machine, since: int) -> List[str]:
        if machine.local:
            return follow_command(local_entry, machine.session, since, python=sys.executable)
        entry = default_remote
        if isinstance(remote_entries, dict):
            entry = remote_entries.get(machine.label) or remote_entries.get(machine.key) or entry
        elif isinstance(remote_entries, str):
            entry = remote_entries
        return ssh_argv(machine.target, entry, machine.session, since)

    progress = Progress(os.path.join(directory, "progress.json"))
    lines: "queue.Queue" = queue.Queue()
    machines = machine_list(io, local_session) or [Machine(LOCAL, "Local", None, local_session)]
    notifier = Notifier(io, machines, log)
    sources: Dict[Machine, Source] = {}
    stopping = threading.Event()

    def stop(signum, frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def sync_sources(current: List[Machine]) -> None:
        for m in [m for m in sources if m not in current]:
            sources.pop(m).stop()
        for m in current:
            if m not in sources:
                sources[m] = Source(m, argv_for, lines, progress, log)
                sources[m].start()

    log("notifier started with %s" % ", ".join(m.label for m in machines))
    sync_sources(machines)
    refreshed = time.monotonic()
    while not stopping.is_set():
        try:
            key, line = lines.get(timeout=1.0)
        except queue.Empty:
            key = None
        if key is not None:
            try:
                notifier.handle(key, line)
            except Exception as e:  # never let one line kill the daemon
                log("handle failed: %r" % e)
            try:
                progress.advance(key, int(line.get("ts_ms") or 0))
            except (TypeError, ValueError):
                pass
        progress.flush()
        if time.monotonic() - refreshed > MACHINES_REFRESH_S:
            refreshed = time.monotonic()
            current = machine_list(io, local_session)
            if current is not None:
                notifier.set_machines(current)
                sync_sources(current)
    for source in sources.values():
        source.stop()
    progress.flush(force=True)
    log("notifier stopped")
    return 0
