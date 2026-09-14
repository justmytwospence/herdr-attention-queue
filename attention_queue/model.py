"""Pure attention model. No I/O lives here.

Every function maps (stored record, herdr truth, time) to a new record, so the
whole state machine is unit-testable.

Attention states, most urgent first:

  blocked  herdr sees an approval or question prompt; the agent needs input
  done     the agent finished a turn and the user has not acted on it yet
  working  the agent is running
  idle     the agent is waiting and nothing is owed
  unknown  herdr cannot classify the agent

"done" is sticky. herdr's own `done` means "idle and not yet seen" and flips
back to idle as soon as the pane is merely viewed. Here it holds until the agent
works again (the user sent a prompt or answered) or the user marks it reviewed.

herdr reports no previous status with an event, and viewing a pane emits no
event at all, so completion is detected from the stored record: the agent was
working and is now idle, with herdr's state_change_seq having advanced.
"""

from typing import Dict, NamedTuple, Optional, Sequence

BUSY = ("working", "blocked")
RANK = {"blocked": "0", "done": "1", "working": "2", "idle": "3", "unknown": "4"}
TOKEN_NAMES = ("attn", "attn_rank", "attn_ts")

VIEW_LABEL = "attention"
# Only plugin tokens and layout order. Never `seen`, `attention`, `status` or
# `state_change_seq`: those change when a row is viewed, and state_change_seq is
# a per-server counter that cannot order agents from different machines.
VIEW_SORT = [
    {"field": {"token": "attn_rank"}, "order": "asc"},
    {"field": {"token": "attn_ts"}, "order": "asc"},
    {"field": "workspace_order", "order": "asc"},
    {"field": "tab_order", "order": "asc"},
    {"field": "pane_order", "order": "asc"},
]

# After a server restart herdr relaunches agents and can report working -> idle
# without any real work (and marks resumed panes done, herdr issue #3990).
# Completions inside this window are ignored.
SETTLE_NS = 30 * 10**9


class Truth(NamedTuple):
    """One agent as herdr reports it in agent.list."""

    terminal_id: str
    pane_id: str
    agent: Optional[str]
    session: Optional[str]
    status: str
    seq: int
    tokens: Dict[str, str]


def session_key(ref) -> Optional[str]:
    """Durable key for an agent conversation, from herdr's agent_session."""
    if not isinstance(ref, dict):
        return None
    parts = (ref.get("agent"), ref.get("kind"), ref.get("value"))
    if not all(isinstance(p, str) and p for p in parts):
        return None
    return "%s:%s:%s" % parts


def raw_status(status: str) -> str:
    """herdr status without the seen bit: `done` is just an unviewed idle."""
    if status == "done":
        return "idle"
    if status in ("idle", "working", "blocked"):
        return status
    return "unknown"


def render(sticky: bool, raw: str, prev_attn: Optional[str]) -> str:
    if raw in BUSY:
        return raw
    if sticky:
        return "done"
    if raw == "unknown":
        # Detection flaps hold the last attention state.
        return prev_attn or "unknown"
    return "idle"


def _finish(rec: dict, prev_attn: Optional[str], raw: str, now_ns: int) -> dict:
    attn = render(rec["sticky"], raw, prev_attn)
    if attn != prev_attn:
        rec["attn_ns"] = now_ns
    rec["attn"] = attn
    return rec


def fresh_record(
    t: Truth,
    now_ns: int,
    restoring: bool,
    durable: Optional[dict],
    settle_ns: int = SETTLE_NS,
) -> dict:
    """First sighting of an agent in a terminal (or a new agent/session there)."""
    raw = raw_status(t.status)
    use = durable is not None and (restoring or durable.get("closed_ns") is None)
    if raw in BUSY:
        sticky = False
    elif use:
        sticky = bool(durable.get("sticky"))
    else:
        # herdr's done on first sight is a real unviewed completion, except while
        # a restart replays agents (herdr issue #3990).
        sticky = t.status == "done" and not restoring
    rec = {
        "pane_id": t.pane_id,
        "agent": t.agent,
        "session": t.session,
        "base": "idle" if raw == "unknown" else raw,
        "seq": t.seq,
        "gap_from": None,
        "sticky": sticky,
        "settle_until_ns": now_ns + settle_ns if restoring else 0,
        "attn": None,
        "attn_ns": now_ns,
    }
    rec["attn"] = render(sticky, raw, durable.get("attn") if use else None)
    if use and durable.get("attn") == rec["attn"] and isinstance(durable.get("attn_ns"), int):
        # Keep the row's place in its group across restarts.
        rec["attn_ns"] = durable["attn_ns"]
    return rec


def step(
    rec: Optional[dict],
    t: Truth,
    hints: Sequence[str],
    now_ns: int,
    restoring: bool,
    durable: Optional[dict],
    settle_ns: int = SETTLE_NS,
) -> dict:
    """Advance a record to the current herdr truth.

    `hints` are statuses reported by events for this pane that may already be
    stale; only "working" is used, to resolve a short turn whose working hook
    reconciled after the agent was idle again.
    """
    if (
        rec is None
        or rec.get("agent") != t.agent
        or rec.get("session") != t.session
        or t.seq < rec.get("seq", 0)
    ):
        return fresh_record(t, now_ns, restoring, durable, settle_ns)

    raw = raw_status(t.status)
    new = dict(rec)
    new["pane_id"] = t.pane_id
    advanced = t.seq > rec["seq"]
    new["seq"] = max(rec["seq"], t.seq)

    if raw in BUSY:
        # The user acted, or the agent resumed on its own.
        new.update(base=raw, sticky=False, gap_from=None)
    elif raw == "idle":
        if now_ns >= rec.get("settle_until_ns", 0):
            completed = rec["base"] == "working" or (
                t.status == "done" and rec["base"] != "blocked"
            )
            if advanced and completed:
                new.update(sticky=True, gap_from=None)
            elif advanced and t.seq >= rec["seq"] + 2 and new.get("gap_from") is None:
                # Several state changes since the last look and idle again: a
                # short turn may have run whose working hook has not reconciled.
                new["gap_from"] = rec["seq"]
            if new.get("gap_from") is not None and "working" in hints:
                new.update(sticky=True, gap_from=None)
        # blocked -> idle is not a completion: leaving blocked required an answer.
        new["base"] = "idle"
    # unknown: hold base and sticky across detection flaps.

    return _finish(new, rec.get("attn"), raw, now_ns)


def apply_ack(rec: dict, raw: str, now_ns: int) -> dict:
    """User marked the agent reviewed."""
    new = dict(rec, sticky=False, gap_from=None)
    return _finish(new, rec.get("attn"), raw, now_ns)


def apply_unread(rec: dict, raw: str, now_ns: int) -> dict:
    """User put an idle agent back into the done group."""
    if raw in BUSY:
        return dict(rec)
    new = dict(rec, sticky=True, gap_from=None)
    return _finish(new, rec.get("attn"), raw, now_ns)


def tokens_for(rec: dict) -> Dict[str, str]:
    attn = rec["attn"]
    return {
        "attn": attn,
        "attn_rank": RANK[attn],
        "attn_ts": "%013d" % (rec["attn_ns"] // 1000000),
    }


def durable_from(rec: dict, now_ns: int) -> dict:
    return {
        "sticky": rec["sticky"],
        "attn": rec["attn"],
        "attn_ns": rec["attn_ns"],
        "seen_ns": now_ns,
        "closed_ns": None,
    }
