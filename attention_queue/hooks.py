"""Reconcile herdr truth into attention tokens: event, startup, reseed, actions.

Every entry point re-reads `agent.list` under the session lock and advances
each agent with the pure model, so hooks that run out of order still converge.
Tokens are written only when they differ from what herdr already holds.
"""

import json
import os
import subprocess
import sys
import time

from . import config, herdr, labels, model, navigation, translog, usage
from . import store as store_mod

ACTIONS = ("mark-reviewed", "mark-unread", "mark-all-reviewed", "reapply", "clear",
           "jump-attention", "refresh")
# Tokens this plugin owns besides the attention ones.
EXTRA_TOKEN_NAMES = ("usage", model.ROW_TOKEN)

# Seconds between reseed passes after a server start; sums to 150.
DEFAULT_RESEED_SCHEDULE = (1, 1, 1, 2, 2, 3, 5, 5, 10, 10, 10, 20, 20, 30, 30)
SEEN_REFRESH_NS = 600 * 10**9
# The ticker polls while any agent is working or waiting, or reports an
# `activity`: background detection rules, `bg` and `activity` tokens and their
# expiry change without any plugin event.
TICK_S = 3.0
TICKER_IDLE_PASSES = 2
TICKER_MAX_S = 3600
ACTIVE = ("working", "waiting")
LOCK_TIMEOUT_S = 10.0
DEBUG_MAX_BYTES = 256 * 1024

PLUGIN_ROOT = os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)


def _seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def restore_ns() -> int:
    return int(_seconds("HERDR_ATTENTION_QUEUE_RESTORE_S", 180) * 1e9)


def settle_ns() -> int:
    return int(_seconds("HERDR_ATTENTION_QUEUE_SETTLE_S", model.SETTLE_NS / 1e9) * 1e9)


def reseed_schedule():
    raw = os.environ.get("HERDR_ATTENTION_QUEUE_RESEED_SCHEDULE")
    if raw:
        try:
            return [max(0.0, float(x)) for x in raw.split(",") if x.strip()]
        except ValueError:
            pass
    return list(DEFAULT_RESEED_SCHEDULE)


def open_store() -> store_mod.Store:
    return store_mod.Store(store_mod.session_dir(herdr.socket_path()))


def debug(store: store_mod.Store, message: str) -> None:
    if os.environ.get("HERDR_ATTENTION_QUEUE_DEBUG") != "1":
        return
    path = os.path.join(store.dir, "debug.log")
    try:
        if os.path.getsize(path) > DEBUG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        with open(path, "a") as f:
            f.write(
                "%s pid=%d %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), os.getpid(), message)
            )
    except OSError:
        pass


def parse_event(raw):
    """(pane_id, hint) from HERDR_PLUGIN_EVENT_JSON; the hint is the reported status."""
    try:
        data = (json.loads(raw) or {}).get("data") or {}
    except (TypeError, ValueError, AttributeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    pane = data.get("pane_id")
    hint = "released" if data.get("released") else data.get("agent_status")
    return (
        pane if isinstance(pane, str) else None,
        hint if isinstance(hint, str) else None,
    )


def _remember(st: dict, rec: dict, now_ns: int, force: bool = False) -> None:
    """Mirror a live record into its durable per-conversation record."""
    key = rec.get("session")
    if not key:
        return
    prev = st["sessions"].get(key)
    same = (
        prev is not None
        and prev.get("closed_ns") is None
        and all(prev.get(k) == rec[k] for k in ("sticky", "attn", "attn_ns"))
    )
    if force or not same or now_ns - int(prev.get("seen_ns") or 0) > SEEN_REFRESH_NS:
        st["sessions"][key] = model.durable_from(rec, now_ns)


def read_truth():
    """(agents, completions): herdr's agents and whether it reports completion_seq."""
    try:
        version = herdr.server_version()
    except herdr.HerdrError:
        version = ()
    return herdr.list_agents(), herdr.reports_completions(version)


def with_background(t: model.Truth, rec, explained: dict) -> model.Truth:
    """Mark a screen-detected working agent whose matched rule is a background one.

    Only the ticker asks herdr which rule matched (`explained`, by terminal).
    Other entry points reuse the last answer while herdr's state is unchanged.
    """
    if model.raw_status(t.status) != "working" or t.detection_skipped:
        return t
    if t.terminal_id in explained:
        background = bool(explained[t.terminal_id])
    else:
        background = rec is not None and rec.get("background_seq") == t.seq
    return t._replace(background=background)


def restoring_at(st: dict, now_ns: int) -> bool:
    return now_ns < int(st["boot"].get("restore_until_ns") or 0)


def reconcile(
    st: dict,
    agents,
    hints: dict,
    now_ns: int,
    completions: bool = False,
    explained=None,
    changes=None,
) -> dict:
    """Advance every agent to herdr truth; return {pane_id: tokens} to write.

    Appends (truth, previous attn, record) to `changes` for every attn change.
    """
    restoring = restoring_at(st, now_ns)
    live = st["live"]
    pending = {}
    present = set()
    for t in agents:
        present.add(t.terminal_id)
        durable = st["sessions"].get(t.session) if t.session else None
        t = with_background(t, live.get(t.terminal_id), explained or {})
        rec = model.step(
            live.get(t.terminal_id),
            t,
            hints.get(t.pane_id, ()),
            now_ns,
            restoring,
            durable,
            settle_ns(),
            completions,
        )
        rec["background_seq"] = t.seq if t.background else None
        rec["activity"] = model.activity(t.tokens)
        prev = (live.get(t.terminal_id) or {}).get("attn")
        if changes is not None and rec["attn"] != prev:
            changes.append((t, prev, rec))
        live[t.terminal_id] = rec
        _remember(st, rec, now_ns)
        want = model.tokens_for(rec)
        if any(t.tokens.get(k) != v for k, v in want.items()):
            pending[t.pane_id] = want
    for tid in [k for k in live if k not in present]:
        gone = live.pop(tid)
        key = gone.get("session")
        if key and key in st["sessions"] and not restoring:
            # The agent exited or its pane closed. That counts as acting on it,
            # so resuming the conversation later does not bring "done" back.
            st["sessions"][key]["closed_ns"] = now_ns
    store_mod.gc_sessions(st, now_ns)
    return pending


def workspace_labels(store: store_mod.Store):
    """{workspace_id: label}, or None when herdr cannot say."""
    try:
        workspaces = herdr.call("workspace.list", {}).get("workspaces") or []
    except (herdr.Unavailable, herdr.HerdrError) as e:
        debug(store, "workspace.list failed: %s" % e)
        return None
    return {
        w.get("workspace_id"): w["label"]
        for w in workspaces
        if isinstance(w, dict) and isinstance(w.get("label"), str)
    }


def tab_labels(store: store_mod.Store):
    """{tab_id: label}; empty when herdr cannot say (rows then skip the tab)."""
    try:
        tabs = herdr.call("tab.list", {}).get("tabs") or []
    except (herdr.Unavailable, herdr.HerdrError) as e:
        debug(store, "tab.list failed: %s" % e)
        return {}
    return {
        t.get("tab_id"): t["label"]
        for t in tabs
        if isinstance(t, dict) and isinstance(t.get("label"), str) and t["label"].strip()
    }


def decorate(st: dict, agents, pending: dict, places=None) -> dict:
    """Add usage, the attn_row token and Claude session names to what reconcile wants written.

    `places` maps pane ids to where the agent is (model.places); without it
    attn_row is left as is.
    Returns {pane_id: name} for every named Claude pane; a pane whose name is new
    gets an entry in `pending` (possibly with no tokens) so the name is reported.
    """
    text = usage.current(store_mod.state_root(), spawn_usage_refresh) if config.enabled("usage") else None
    names = {}
    for t in agents:
        want = None
        if text and t.tokens.get("usage") != text:
            want = dict(pending.get(t.pane_id) or {})
            want["usage"] = text
        rec = st["live"].get(t.terminal_id)
        if places is not None and rec is not None:
            row = model.row_text(rec["attn"], places.get(t.pane_id))
            if t.tokens.get(model.ROW_TOKEN) != row:
                want = dict(want if want is not None else pending.get(t.pane_id) or {})
                want[model.ROW_TOKEN] = row
        if config.enabled("claude_names") and t.agent == "claude":
            name = labels.claude_name(t.session)
            rec = st["live"].get(t.terminal_id)
            if name:
                names[t.pane_id] = name
                if rec is not None and rec.get("label") != name:
                    rec["label"] = name
                    if want is None:
                        want = dict(pending.get(t.pane_id) or {})
        if want is not None:
            pending[t.pane_id] = want
    return names


def log_changes(store: store_mod.Store, st: dict, changes, now_ns: int, places=None) -> None:
    """Append attn changes to the transition log (under the session lock)."""
    if not changes:
        return
    places = places or {}
    restoring = restoring_at(st, now_ns)
    lines = []
    for t, prev, rec in changes:
        workspace = places.get(t.pane_id) if rec["attn"] in ("blocked", "done") else None
        lines.append(
            translog.attn_line(
                t, rec["attn"], prev, restoring, workspace, rec.get("label"), now_ns // 10**6
            )
        )
    try:
        translog.append(store.dir, lines)
    except OSError as e:
        debug(store, "transition log write failed: %s" % e)


def publish(store: store_mod.Store, st: dict, agents, pending: dict, changes, now_ns: int) -> None:
    """Write tokens and names, and log the attention changes."""
    st.pop("cleared", None)
    workspaces = workspace_labels(store)
    places = model.places(agents, workspaces, tab_labels(store)) if workspaces is not None else None
    names = decorate(st, agents, pending, places)
    write_tokens(store, st, pending, names)
    log_changes(store, st, changes, now_ns, places)


def write_tokens(store: store_mod.Store, st: dict, pending: dict, names=None) -> None:
    for pane_id, tokens in pending.items():
        seq = max(time.time_ns(), int(st.get("report_seq") or 0) + 1)
        st["report_seq"] = seq
        try:
            herdr.report_tokens(pane_id, tokens, seq, (names or {}).get(pane_id))
        except herdr.HerdrError as e:
            # Usually the pane closed between agent.list and this write; the
            # next reconcile sees the new truth.
            debug(store, "report %s failed: %s" % (pane_id, e))


def spawn(mode: str) -> None:
    """Run `attention.py <mode>` detached.

    DEVNULL for every stream: herdr captures hook output, and a child holding
    those pipes would keep the hook "running" in the plugin log.
    """
    subprocess.Popen(
        [sys.executable, "-B", os.path.join(PLUGIN_ROOT, "attention.py"), mode],
        cwd=PLUGIN_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def spawn_usage_refresh() -> None:
    """Fetch usage in a detached process; it pushes the new text when done."""
    spawn("usage-refresh")


def tick_s() -> float:
    return max(0.01, _seconds("HERDR_ATTENTION_QUEUE_TICK_S", TICK_S))


def active(st: dict) -> bool:
    """An agent is working or waiting, or reports itself blocked or working.

    Token changes and expiry emit no plugin event, so the ticker watches them;
    a turn the agent ends by reporting `idle` is seen while it still works.
    """
    return any(
        r.get("attn") in ACTIVE or r.get("activity") in model.BUSY for r in st["live"].values()
    )


def ticker_lock_path(store: store_mod.Store) -> str:
    return os.path.join(store.dir, "ticker.lock")


def ensure_ticker(store: store_mod.Store, st: dict) -> None:
    """Start the ticker if an agent is working or waiting and none is running."""
    if not active(st):
        return
    fd = store_mod.try_lock(ticker_lock_path(store))
    if fd is None:
        return
    os.close(fd)
    spawn("ticker")


def explain_background(agents) -> dict:
    """{terminal_id: matched a background_* rule} for screen-detected working agents."""
    explained = {}
    for t in agents:
        if model.raw_status(t.status) != "working" or t.detection_skipped:
            continue
        try:
            rule = herdr.matched_rule(t.pane_id)
        except herdr.HerdrError:
            continue
        explained[t.terminal_id] = bool(rule and rule.startswith(model.BACKGROUND_RULE_PREFIX))
    return explained


def code_signature() -> tuple:
    """Modification times of the plugin's code, to notice an update underneath."""
    package = os.path.join(PLUGIN_ROOT, "attention_queue")
    paths = [os.path.join(PLUGIN_ROOT, "attention.py")]
    try:
        paths += [os.path.join(package, n) for n in sorted(os.listdir(package)) if n.endswith(".py")]
    except OSError:
        pass
    signature = []
    for path in paths:
        try:
            signature.append((path, os.stat(path).st_mtime_ns))
        except OSError:
            signature.append((path, None))
    return tuple(signature)


def ticker() -> int:
    """Poll while agents are working or waiting; exit once none are.

    A ticker lives up to an hour, so after a plugin update it would keep
    writing tokens with the old code. It hands over to a fresh ticker instead.
    """
    store = open_store()
    guard = store_mod.try_lock(ticker_lock_path(store))
    if guard is None:
        return 0
    code = code_signature()
    updated = False
    try:
        quiet = 0
        deadline = time.monotonic() + TICKER_MAX_S
        while quiet < TICKER_IDLE_PASSES and time.monotonic() < deadline:
            if code_signature() != code:
                updated = True
                break
            try:
                with store.locked(LOCK_TIMEOUT_S):
                    st = store.load()
                    if st.get("cleared"):
                        # `clear` ran: do not put the tokens back.
                        break
                    before = store.dumps(st)
                    agents, completions = read_truth()
                    explained = explain_background(agents)
                    now, changes = time.time_ns(), []
                    pending = reconcile(st, agents, {}, now, completions, explained, changes)
                    publish(store, st, agents, pending, changes, now)
                    store.save_if_changed(st, before)
                    busy = active(st)
            except store_mod.LockTimeout:
                busy = True
            except herdr.Unavailable:
                # The server is gone; a hook on the next server starts a new ticker.
                break
            quiet = 0 if busy else quiet + 1
            time.sleep(tick_s())
    finally:
        os.close(guard)
    if updated:
        spawn("ticker")
    return 0


def usage_refresh() -> int:
    root = store_mod.state_root()
    try:
        fetched = usage.refresh(root)
    finally:
        usage.release_refresh(root)
    if fetched:
        # Push the new text to every agent row now rather than on the next event.
        store = open_store()
        with store.locked(LOCK_TIMEOUT_S):
            st = store.load()
            before = store.dumps(st)
            agents, completions = read_truth()
            now, changes = time.time_ns(), []
            pending = reconcile(st, agents, {}, now, completions, None, changes)
            publish(store, st, agents, pending, changes, now)
            store.save_if_changed(st, before)
    return 0


def set_view(store: store_mod.Store, attempts: int = 3) -> bool:
    for attempt in range(attempts):
        try:
            herdr.set_view()
            return True
        except (herdr.Unavailable, herdr.HerdrError) as e:
            debug(store, "agent.view.set failed: %s" % e)
            if attempt + 1 < attempts:
                time.sleep(0.5)
    return False


def on_event() -> int:
    pane, hint = parse_event(os.environ.get("HERDR_PLUGIN_EVENT_JSON"))
    hints = {pane: (hint,)} if pane and hint else {}
    store = open_store()
    with store.locked(LOCK_TIMEOUT_S):
        st = store.load()
        before = store.dumps(st)
        now = time.time_ns()
        agents, completions = read_truth()
        changes = []
        pending = reconcile(st, agents, hints, now, completions, None, changes)
        publish(store, st, agents, pending, changes, now)
        store.save_if_changed(st, before)
    ensure_ticker(store, st)
    if pending:
        debug(store, "event pane=%s hint=%s wrote %s" % (pane, hint, sorted(pending)))
    return 0


def on_focus() -> int:
    """Log that a pane got focus, so the notifier can drop its notification."""
    pane, _ = parse_event(os.environ.get("HERDR_PLUGIN_EVENT_JSON"))
    if not pane:
        return 0
    store = open_store()
    with store.locked(LOCK_TIMEOUT_S):
        translog.append(store.dir, [translog.focus_line(pane, time.time_ns() // 10**6)])
    return 0


def session_socket(session: str) -> str:
    """The socket of a herdr session on this host."""
    base = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr")
    if session == "default":
        return os.path.join(base, "herdr.sock")
    return os.path.join(base, "sessions", session, "herdr.sock")


# Agent fields the notifier needs to rebuild the Agents panel order.
LIST_TOKENS = ("attn", "attn_rank", "attn_ts")


def serve_command(command: dict):
    """A notifier request arriving on `follow`'s input; returns the reply line."""
    op, req = command.get("op"), command.get("req")
    reply = {"v": translog.VERSION, "kind": op, "req": req, "ts_ms": time.time_ns() // 10**6}
    try:
        if op == "list":
            agents = []
            for a in herdr.call("agent.list", {}).get("agents") or []:
                tokens = a.get("tokens") or {}
                agents.append({
                    "pane_id": a.get("pane_id"),
                    "focused": bool(a.get("focused")),
                    "tokens": {k: tokens[k] for k in LIST_TOKENS if isinstance(tokens.get(k), str)},
                })
            reply["agents"] = agents
        elif op == "focus":
            herdr.call("agent.focus", {"target": command.get("pane_id")})
            reply["ok"] = True
        elif op == "notice":
            herdr.call("notification.show", {"title": str(command.get("text") or ""), "sound": "none"})
            reply["ok"] = True
        else:
            return None
    except (herdr.HerdrError, herdr.Unavailable) as e:
        reply.update(kind="error", op=op, error=str(e))
    return reply


def follow(session: str, since_ms: int) -> int:
    """Print the session's transition log from since_ms on, then follow it."""
    if not store_mod.valid_session_name(session):
        print("attention-queue: bad session name %r" % session, file=sys.stderr)
        return 2
    directory = os.path.join(store_mod.state_root(), "sessions", session)
    if not os.environ.get("HERDR_SOCKET_PATH"):
        os.environ["HERDR_SOCKET_PATH"] = session_socket(session)
    parent = os.getppid()
    try:
        return translog.follow(
            directory,
            since_ms,
            stop=lambda: os.getppid() != parent,
            commands=sys.stdin,
            on_command=serve_command,
        )
    except BrokenPipeError:
        # The reader (usually an ssh session) went away.
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0


def on_startup() -> int:
    store = open_store()
    handoff = False
    st = None
    try:
        with store.locked(LOCK_TIMEOUT_S):
            st = store.load()
            before = store.dumps(st)
            now = time.time_ns()
            agents, completions = read_truth()
            # A live handoff keeps terminals; a cold start relaunches agents
            # after this hook, with new terminal ids and no events.
            handoff = bool(set(st["live"]) & {t.terminal_id for t in agents})
            if not handoff:
                st["expect"] = sorted(
                    {r["session"] for r in st["live"].values() if r.get("session")}
                )
                st["live"] = {}
                st["boot"] = {"started_ns": now, "restore_until_ns": now + restore_ns()}
            changes = []
            pending = reconcile(st, agents, {}, now, completions, None, changes)
            publish(store, st, agents, pending, changes, now)
            store.save_if_changed(st, before)
    except store_mod.LockTimeout:
        pass
    set_view(store)
    if not handoff:
        spawn_reseed()
    elif st is not None:
        ensure_ticker(store, st)
    return 0


def spawn_reseed() -> None:
    spawn("reseed")


def reseed() -> int:
    """Reconcile repeatedly while restored agents come back after a restart."""
    store = open_store()
    guard = store_mod.try_lock(os.path.join(store.dir, "reseed.lock"))
    if guard is None:
        return 0
    try:
        satisfied = 0
        for delay in reseed_schedule():
            time.sleep(delay)
            finished = False
            try:
                with store.locked(LOCK_TIMEOUT_S):
                    st = store.load()
                    before = store.dumps(st)
                    now = time.time_ns()
                    agents, completions = read_truth()
                    changes = []
                    pending = reconcile(st, agents, {}, now, completions, None, changes)
                    publish(store, st, agents, pending, changes, now)
                    expect = set(st.get("expect") or [])
                    if expect:
                        present = {t.session for t in agents if t.session}
                        satisfied = satisfied + 1 if expect <= present else 0
                        if satisfied >= 2:
                            st["expect"] = []
                            finished = True
                    store.save_if_changed(st, before)
            except (store_mod.LockTimeout, herdr.Unavailable, herdr.HerdrError) as e:
                debug(store, "reseed pass skipped: %s" % e)
                continue
            if finished:
                break
        set_view(store, attempts=1)
        try:
            ensure_ticker(store, store.load())
        except OSError:
            pass
    finally:
        os.close(guard)
    return 0


def navigation_anchor():
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        return pane
    try:
        context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
        pane = context.get("focused_pane_id")
        return pane if isinstance(pane, str) else None
    except (ValueError, AttributeError):
        return None


def navigation_snapshot(store):
    """Reconcile/publish normally, never acknowledging; release lock before focus."""
    with store.locked(LOCK_TIMEOUT_S):
        st = store.load()
        before = store.dumps(st)
        agents, completions = read_truth()
        now, changes = time.time_ns(), []
        pending = reconcile(st, agents, {}, now, completions, None, changes)
        publish(store, st, agents, pending, changes, now)
        store.save_if_changed(st, before)
    ensure_ticker(store, st)
    return agents, st["live"]


def show_notice(message: str) -> None:
    print("attention-queue: " + message)
    try:
        herdr.call("notification.show", {"title": message, "sound": "none"})
    except (herdr.HerdrError, herdr.Unavailable):
        pass


def hand_off_jump(store) -> bool:
    """Ask the notifier to jump, if one follows this server's log.

    It sees every connected machine and switches the client between them; this
    server sees only itself. Cheap on purpose: no herdr call, no reconcile.
    """
    if not translog.follower_alive(store.dir):
        return False
    with store.locked(LOCK_TIMEOUT_S):
        translog.append(store.dir, [translog.jump_line(navigation_anchor(), time.time_ns() // 10**6)])
    return True


def jump_attention(store):
    if hand_off_jump(store):
        return 0
    # No notifier: jump among this server's agents only.
    anchor = navigation_anchor()
    for attempt in range(2):
        agents, live = navigation_snapshot(store)
        target = navigation.select(agents, live, anchor)
        if target is None:
            show_notice("No agents need attention on this server")
            return 0
        expected = navigation.signature(target, live)
        # A fresh reconciled read catches a moved/replaced/closed agent and
        # changes to rendered eligibility. No focus RPC holds the state lock.
        current, live = navigation_snapshot(store)
        found = navigation.select(current, live, anchor)
        if found is None or navigation.signature(found, live) != expected:
            continue  # Also catches a newly arrived higher-priority agent.
        if target.pane_id == anchor:
            return 0  # The only agent of its tier: stay.
        try:
            herdr.call("agent.focus", {"target": target.pane_id})
            return 0
        except herdr.HerdrError as e:
            # A definitive stale-target rejection permits one fresh selection.
            if e.code not in ("agent_not_found", "pane_not_found"):
                print("attention-queue: focus failed: %s" % e)
                return 0
        except herdr.Unavailable as e:
            # A timeout may have focused successfully. Never blindly retry it.
            print("attention-queue: focus outcome unknown; not retrying: %s" % e)
            return 0
    print("attention-queue: queue changed during navigation; try again")
    return 0


def action(name: str) -> int:
    store = open_store()
    if name == "jump-attention":
        return jump_attention(store)
    if name == "clear":
        return clear(store)
    with store.locked(LOCK_TIMEOUT_S):
        st = store.load()
        before = store.dumps(st)
        now = time.time_ns()
        agents, completions = read_truth()
        changes = []
        pending = reconcile(st, agents, {}, now, completions, None, changes)
        by_pane = {t.pane_id: t for t in agents}
        targets = []
        if name in ("mark-reviewed", "mark-unread"):
            pane = os.environ.get("HERDR_PANE_ID")
            if pane in by_pane:
                targets = [by_pane[pane]]
            else:
                print("attention-queue: focused pane %s has no agent" % (pane or "(none)"))
        elif name == "mark-all-reviewed":
            targets = list(agents)
        for t in targets:
            rec = st["live"][t.terminal_id]
            t = with_background(t, rec, {})
            prev = rec.get("attn")
            if name == "mark-unread":
                rec = model.apply_unread(rec, t, now)
            else:
                rec = model.apply_ack(rec, t, now)
            if rec["attn"] != prev:
                changes.append((t, prev, rec))
            st["live"][t.terminal_id] = rec
            _remember(st, rec, now, force=True)
            want = model.tokens_for(rec)
            if any(t.tokens.get(k) != v for k, v in want.items()):
                pending[t.pane_id] = want
            else:
                pending.pop(t.pane_id, None)
            print("attention-queue: %s -> %s" % (t.pane_id, rec["attn"]))
        publish(store, st, agents, pending, changes, now)
        store.save_if_changed(st, before)
    ensure_ticker(store, st)
    if name == "reapply":
        ok = set_view(store)
        print("attention-queue: view %s" % ("applied" if ok else "not applied"))
        return 0 if ok else 1
    return 0


def clear(store: store_mod.Store) -> int:
    with store.locked(LOCK_TIMEOUT_S):
        st = store.load()
        before = store.dumps(st)
        owned = model.TOKEN_NAMES + EXTRA_TOKEN_NAMES
        nulls = {k: None for k in owned}
        for t in herdr.list_agents():
            named = t.agent == "claude"
            if not named and not any(k in t.tokens for k in owned):
                continue
            seq = max(time.time_ns(), int(st.get("report_seq") or 0) + 1)
            st["report_seq"] = seq
            try:
                herdr.report_tokens(t.pane_id, nulls, seq, clear_label=named)
            except herdr.HerdrError as e:
                debug(store, "clear %s failed: %s" % (t.pane_id, e))
        # Durable per-conversation state is kept; only live rendering is reset.
        st["live"] = {}
        st["cleared"] = True
        store.save_if_changed(st, before)
    try:
        herdr.clear_view()
    except herdr.HerdrError as e:
        print("attention-queue: agent.view.clear failed: %s" % e)
        return 1
    print("attention-queue: cleared tokens and view")
    return 0
