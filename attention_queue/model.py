"""Pure attention model. No I/O lives here.

Every function maps (stored record, herdr truth, time) to a new record, so the
whole state machine is unit-testable.

Attention states, most urgent first:

  blocked  herdr sees an approval or question prompt; the agent needs input
  done     the agent finished a turn and the user has not acted on it yet
  working  the agent is running
  waiting  the agent's own turn is paused or over, but background work it
           started (subagents, background tasks) will wake it
  idle     the agent is waiting for the user and nothing is owed
  unknown  herdr cannot classify the agent

"waiting" comes from two signals herdr cannot express as a status:

  - herdr says working, but the detection rule that matched is a background
    one (`background_*`, e.g. Claude's "Waiting for N background agents").
  - herdr says idle, but the pane's `bg` token (reported by the agent, e.g. a
    pi extension) counts pending background work.

An agent's own hooks can report its status in the pane's `activity` token,
which then stands in for herdr's status: `blocked` while it waits on the user
(a question herdr's screen rules do not know), `working` while it works (also
outside a turn, like a plan-mode planner run), `idle` when its turn is over
(for agents herdr reads as unknown after a response, like Codex). Blocked from
either source wins. A turn the token ends (working to idle, by the token
changing) is a completion; while the token is set, herdr's own completion
signals are ignored, since the agent knows better.

"done" is sticky. herdr's own `done` means "idle and not yet seen" and flips
back to idle as soon as the pane is merely viewed. Here it holds until the agent
works again (the user sent a prompt or answered) or the user marks it reviewed.

herdr reports no previous status with an event, and viewing a pane emits no
event at all, so completion is detected from the stored record: the agent was
working and is now idle, with herdr's state_change_seq having advanced. herdr
0.9.2 also reports `completion_seq`, set when the current idle ended real work;
when the server has it, that decides completions instead of the stored guess.
"""

from typing import Dict, NamedTuple, Optional, Sequence

BUSY = ("working", "blocked")
STATES = ("blocked", "done", "working", "waiting", "idle", "unknown")
RANK = {state: str(i) for i, state in enumerate(STATES)}
# Nerd Font glyphs; client config styles the token by state with display rules.
ICON = {
    "blocked": "\uf06a",  # nf-fa-exclamation_circle
    "done": "\uf058",  # nf-fa-check_circle
    "working": "\uf144",  # nf-fa-play_circle
    "waiting": "\uf252",  # nf-fa-hourglass_half
    "idle": "\uf10c",  # nf-fa-circle_o
    "unknown": "\uf059",  # nf-fa-question_circle
}
TOKEN_NAMES = ("attn", "attn_rank", "attn_ts", "attn_icon")
# The icon and workspace label as one token. herdr puts " · " between any two
# row tokens except after its built-in state_icon, so a row of $attn_icon and
# workspace reads "icon · name"; one token reads "icon name".
ROW_TOKEN = "attn_row"
# Pane token an agent reports with its count of pending background work.
BG_TOKEN = "bg"
# Pane token an agent reports with a state herdr cannot see: blocked or working.
ACTIVITY_TOKEN = "activity"
ACTIVITIES = ("blocked", "working", "idle")
# Detection rules that mean "working, but only on background work".
BACKGROUND_RULE_PREFIX = "background_"

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
    completion_seq: Optional[int] = None
    detection_skipped: bool = False
    background: bool = False
    workspace_id: Optional[str] = None
    tab_id: Optional[str] = None


def bg_count(tokens: Dict[str, str]) -> int:
    """Pending background work the agent reported in its `bg` token."""
    try:
        return max(0, int(str(tokens.get(BG_TOKEN) or "0").strip()))
    except ValueError:
        return 0


def activity(tokens: Dict[str, str]) -> Optional[str]:
    """The state the agent reported in its `activity` token, if it is one we know."""
    value = str(tokens.get(ACTIVITY_TOKEN) or "").strip().lower()
    return value if value in ACTIVITIES else None


def session_key(ref) -> Optional[str]:
    """Durable key for an agent conversation, from herdr's agent_session."""
    if not isinstance(ref, dict):
        return None
    parts = (ref.get("agent"), ref.get("kind"), ref.get("value"))
    if not all(isinstance(p, str) and p for p in parts):
        return None
    return "%s:%s:%s" % parts


def effective_status(t: "Truth") -> str:
    """herdr's status (without the seen bit), as the agent's `activity` corrects it.

    herdr's blocked always wins: a screen rule saw a prompt the hooks did not.
    """
    raw = raw_status(t.status)
    if raw == "blocked":
        return raw
    return activity(t.tokens) or raw


def raw_status(status: str) -> str:
    """herdr status without the seen bit: `done` is just an unviewed idle."""
    if status == "done":
        return "idle"
    if status in ("idle", "working", "blocked"):
        return status
    return "unknown"


def render(
    sticky: bool,
    raw: str,
    prev_attn: Optional[str],
    background: bool = False,
    bg: int = 0,
) -> str:
    """The attention state from the effective status, by the first rule that applies:

    blocked; working on background work only -> waiting; working; idle with
    background work pending -> waiting; an unacted completion -> done; idle.
    """
    if raw == "blocked":
        return "blocked"
    if raw == "working":
        return "waiting" if background else "working"
    if raw == "unknown":
        # Detection flaps hold the last attention state.
        return prev_attn or "unknown"
    if bg > 0:
        return "waiting"
    if sticky:
        return "done"
    return "idle"


def _render_truth(sticky: bool, t: Truth, prev_attn: Optional[str]) -> str:
    # A background detection rule describes herdr's screen, not the agent's report.
    background = t.background and activity(t.tokens) is None
    return render(sticky, effective_status(t), prev_attn, background, bg_count(t.tokens))


def _finish(rec: dict, prev_attn: Optional[str], t: Truth, now_ns: int) -> dict:
    attn = _render_truth(rec["sticky"], t, prev_attn)
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
    completions: bool = False,
) -> dict:
    """First sighting of an agent in a terminal (or a new agent/session there).

    `completions` says the server reports completion_seq (herdr 0.9.2+).
    """
    raw = effective_status(t)
    act = activity(t.tokens)
    use = durable is not None and (restoring or durable.get("closed_ns") is None)
    if raw in BUSY:
        sticky = False
    elif use:
        sticky = bool(durable.get("sticky"))
    elif act is not None:
        # The agent reports its own state; herdr's done is not its completion.
        sticky = False
    else:
        # herdr's done on first sight is a real unviewed completion, except while
        # a restart replays agents (herdr issue #3990).
        sticky = t.status == "done" and not restoring
        if completions and t.completion_seq != t.seq:
            sticky = False
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
        "act": act,
        "herdr": raw_status(t.status),
    }
    rec["attn"] = _render_truth(sticky, t, durable.get("attn") if use else None)
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
    completions: bool = False,
) -> dict:
    """Advance a record to the current herdr truth.

    `hints` are statuses reported by events for this pane that may already be
    stale; only "working" is used, to resolve a short turn whose working hook
    reconciled after the agent was idle again. `completions` says the server
    reports completion_seq; then it decides completions and hints are unused.
    """
    if (
        rec is None
        or rec.get("agent") != t.agent
        or rec.get("session") != t.session
        or t.seq < rec.get("seq", 0)
    ):
        return fresh_record(t, now_ns, restoring, durable, settle_ns, completions)

    raw = effective_status(t)
    act = activity(t.tokens)
    # The agent's report appeared, changed or was withdrawn since the last look.
    driven = act != rec.get("act")
    new = dict(rec)
    new["pane_id"] = t.pane_id
    new["act"] = act
    new["herdr"] = raw_status(t.status)
    advanced = t.seq > rec["seq"]
    new["seq"] = max(rec["seq"], t.seq)
    settled = now_ns >= rec.get("settle_until_ns", 0)

    if raw in BUSY:
        # The user acted, or the agent resumed on its own.
        new.update(base=raw, sticky=False, gap_from=None)
    elif raw == "idle" and act is not None or driven and raw == "idle" and rec["base"] == "working":
        # The agent's own report decides. Its turn ending (working to idle by the
        # report changing) is a completion; herdr's completion signals are not
        # consulted while it reports, since herdr cannot read this agent well.
        if settled and driven and rec["base"] == "working":
            new.update(sticky=True)
        new.update(base="idle", gap_from=None)
    elif raw == "idle":
        if settled and completions:
            # herdr counts blocked -> idle as a completion; here leaving blocked
            # straight to idle required an answer, so it is not one.
            answered = rec.get("herdr", rec["base"]) == "blocked" and t.seq == rec["seq"] + 1
            if advanced and t.completion_seq == t.seq and not answered:
                new.update(sticky=True)
            new["gap_from"] = None
        elif settled:
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

    return _finish(new, rec.get("attn"), t, now_ns)


def apply_ack(rec: dict, t: Truth, now_ns: int) -> dict:
    """User marked the agent reviewed."""
    new = dict(rec, sticky=False, gap_from=None)
    return _finish(new, rec.get("attn"), t, now_ns)


def apply_unread(rec: dict, t: Truth, now_ns: int) -> dict:
    """User put an idle agent back into the done group."""
    if effective_status(t) in BUSY:
        return dict(rec)
    new = dict(rec, sticky=True, gap_from=None)
    return _finish(new, rec.get("attn"), t, now_ns)


def tokens_for(rec: dict) -> Dict[str, str]:
    attn = rec["attn"]
    return {
        "attn": attn,
        "attn_rank": RANK[attn],
        "attn_ts": "%013d" % (rec["attn_ns"] // 1000000),
        "attn_icon": ICON[attn],
    }


# Nerd Font icons fill their cell, so one space reads as touching the name.
ROW_GAP = "  "


def row_text(attn: str, place: Optional[str]) -> str:
    """`attn_row`: the state icon, a two-space gap, then where the agent is."""
    icon = ICON.get(attn, ICON["unknown"])
    return icon + ROW_GAP + place if place else icon


PLACE_SEP = " › "  # single right-pointing angle quotation mark


def places(
    agents: Sequence[Truth], workspaces: Dict[str, str], tabs: Optional[Dict[str, str]] = None
) -> Dict[str, Optional[str]]:
    """{pane_id: where the agent is}, as short as tells agents apart.

    The workspace label alone; with several agents in the workspace, then its
    tab (`homelab › navigation`); with several in that tab, then the agent kind
    (`homelab › navigation › claude`), and its pane when even that repeats.
    """
    tabs = tabs or {}
    result: Dict[str, Optional[str]] = {}
    by_workspace: Dict[Optional[str], list] = {}
    for t in agents:
        by_workspace.setdefault(t.workspace_id, []).append(t)
    for workspace_id, group in by_workspace.items():
        base = workspaces.get(workspace_id) if workspace_id else None
        if not base:
            result.update((t.pane_id, None) for t in group)
            continue
        if len(group) == 1:
            result[group[0].pane_id] = base
            continue
        by_tab: Dict[Optional[str], list] = {}
        for t in group:
            by_tab.setdefault(t.tab_id if t.tab_id in tabs else None, []).append(t)
        for tab_id, tab_group in by_tab.items():
            prefix = base + PLACE_SEP + tabs[tab_id] if tab_id is not None else base
            if len(tab_group) == 1 and tab_id is not None:
                result[tab_group[0].pane_id] = prefix
                continue
            kinds = [t.agent or "agent" for t in tab_group]
            for t, kind in zip(tab_group, kinds):
                name = prefix + PLACE_SEP + kind
                if kinds.count(kind) > 1:
                    name += " " + t.pane_id.rsplit(":", 1)[-1]
                result[t.pane_id] = name
    return result


def durable_from(rec: dict, now_ns: int) -> dict:
    return {
        "sticky": rec["sticky"],
        "attn": rec["attn"],
        "attn_ns": rec["attn_ns"],
        "seen_ns": now_ns,
        "closed_ns": None,
    }
