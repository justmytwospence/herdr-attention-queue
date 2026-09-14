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

from . import herdr, model
from . import store as store_mod

ACTIONS = ("mark-reviewed", "mark-unread", "mark-all-reviewed", "reapply", "clear")

# Seconds between reseed passes after a server start; sums to 150.
DEFAULT_RESEED_SCHEDULE = (1, 1, 1, 2, 2, 3, 5, 5, 10, 10, 10, 20, 20, 30, 30)
SEEN_REFRESH_NS = 600 * 10**9
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


def reconcile(st: dict, agents, hints: dict, now_ns: int) -> dict:
    """Advance every agent to herdr truth; return {pane_id: tokens} to write."""
    restoring = now_ns < int(st["boot"].get("restore_until_ns") or 0)
    live = st["live"]
    pending = {}
    present = set()
    for t in agents:
        present.add(t.terminal_id)
        durable = st["sessions"].get(t.session) if t.session else None
        rec = model.step(
            live.get(t.terminal_id),
            t,
            hints.get(t.pane_id, ()),
            now_ns,
            restoring,
            durable,
            settle_ns(),
        )
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


def write_tokens(store: store_mod.Store, st: dict, pending: dict) -> None:
    for pane_id, tokens in pending.items():
        seq = max(time.time_ns(), int(st.get("report_seq") or 0) + 1)
        st["report_seq"] = seq
        try:
            herdr.report_tokens(pane_id, tokens, seq)
        except herdr.HerdrError as e:
            # Usually the pane closed between agent.list and this write; the
            # next reconcile sees the new truth.
            debug(store, "report %s failed: %s" % (pane_id, e))


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
        pending = reconcile(st, herdr.list_agents(), hints, now)
        write_tokens(store, st, pending)
        store.save_if_changed(st, before)
    if pending:
        debug(store, "event pane=%s hint=%s wrote %s" % (pane, hint, sorted(pending)))
    return 0


def on_startup() -> int:
    store = open_store()
    handoff = False
    try:
        with store.locked(LOCK_TIMEOUT_S):
            st = store.load()
            before = store.dumps(st)
            now = time.time_ns()
            agents = herdr.list_agents()
            # A live handoff keeps terminals; a cold start relaunches agents
            # after this hook, with new terminal ids and no events.
            handoff = bool(set(st["live"]) & {t.terminal_id for t in agents})
            if not handoff:
                st["expect"] = sorted(
                    {r["session"] for r in st["live"].values() if r.get("session")}
                )
                st["live"] = {}
                st["boot"] = {"started_ns": now, "restore_until_ns": now + restore_ns()}
            pending = reconcile(st, agents, {}, now)
            write_tokens(store, st, pending)
            store.save_if_changed(st, before)
    except store_mod.LockTimeout:
        pass
    set_view(store)
    if not handoff:
        spawn_reseed()
    return 0


def spawn_reseed() -> None:
    # DEVNULL for every stream: herdr captures hook output, and a child holding
    # those pipes would keep the startup command "running" in the plugin log.
    subprocess.Popen(
        [sys.executable, "-B", os.path.join(PLUGIN_ROOT, "attention.py"), "reseed"],
        cwd=PLUGIN_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


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
                    agents = herdr.list_agents()
                    pending = reconcile(st, agents, {}, now)
                    write_tokens(store, st, pending)
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
    finally:
        os.close(guard)
    return 0


def action(name: str) -> int:
    store = open_store()
    if name == "clear":
        return clear(store)
    with store.locked(LOCK_TIMEOUT_S):
        st = store.load()
        before = store.dumps(st)
        now = time.time_ns()
        agents = herdr.list_agents()
        pending = reconcile(st, agents, {}, now)
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
            raw = model.raw_status(t.status)
            rec = st["live"][t.terminal_id]
            if name == "mark-unread":
                rec = model.apply_unread(rec, raw, now)
            else:
                rec = model.apply_ack(rec, raw, now)
            st["live"][t.terminal_id] = rec
            _remember(st, rec, now, force=True)
            want = model.tokens_for(rec)
            if any(t.tokens.get(k) != v for k, v in want.items()):
                pending[t.pane_id] = want
            else:
                pending.pop(t.pane_id, None)
            print("attention-queue: %s -> %s" % (t.pane_id, rec["attn"]))
        write_tokens(store, st, pending)
        store.save_if_changed(st, before)
    if name == "reapply":
        ok = set_view(store)
        print("attention-queue: view %s" % ("applied" if ok else "not applied"))
        return 0 if ok else 1
    return 0


def clear(store: store_mod.Store) -> int:
    with store.locked(LOCK_TIMEOUT_S):
        st = store.load()
        before = store.dumps(st)
        nulls = {k: None for k in model.TOKEN_NAMES}
        write_tokens(
            store,
            st,
            {
                t.pane_id: nulls
                for t in herdr.list_agents()
                if any(k in t.tokens for k in model.TOKEN_NAMES)
            },
        )
        # Durable per-conversation state is kept; only live rendering is reset.
        st["live"] = {}
        store.save_if_changed(st, before)
    try:
        herdr.clear_view()
    except herdr.HerdrError as e:
        print("attention-queue: agent.view.clear failed: %s" % e)
        return 1
    print("attention-queue: cleared tokens and view")
    return 0
