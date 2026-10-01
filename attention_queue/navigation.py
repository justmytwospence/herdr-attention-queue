"""Pure selection in the rendered attention queue."""

# The states Ctrl-b Enter cycles through, most urgent first. Done and idle
# agents are left to the panel and the review keys.
TARGETS = ("blocked", "working", "waiting")


def candidates(agents, live):
    """Targets in the Agents panel's order: state, then entry time, then layout.

    Python's stable sort keeps agent.list's workspace/tab/pane layout order for
    equal milliseconds, like the sidebar's view.
    """
    eligible = [t for t in agents if live[t.terminal_id]["attn"] in TARGETS]
    return sorted(eligible, key=lambda t: (
        TARGETS.index(live[t.terminal_id]["attn"]),
        live[t.terminal_id]["attn_ns"] // 10**6,
    ))


def cycle(rows, here):
    """Index of the row to jump to, or None when no row is a target.

    `rows` are (key, attn) in the Agents panel's visual order; `here` is the
    current agent's key. Targets are blocked, working and waiting rows, in that
    (panel) order. From anywhere else go to the first target, the most urgent;
    from a target go to the next one, wrapping after the last. A lone target
    that is `here` is returned as is: stay.
    """
    targets = [i for i, (_, attn) in enumerate(rows) if attn in TARGETS]
    if not targets:
        return None
    keys = [rows[i][0] for i in targets]
    if here in keys:
        return targets[(keys.index(here) + 1) % len(targets)]
    return targets[0]


def select(agents, live, here=None):
    """The agent to jump to from pane `here`, independent of server-wide focus."""
    queue = candidates(agents, live)
    index = cycle([(t.pane_id, live[t.terminal_id]["attn"]) for t in queue], here)
    return None if index is None else queue[index]


def signature(t, live):
    """Identity, location and eligibility epoch for immediate revalidation."""
    rec = live[t.terminal_id]
    return (t.terminal_id, t.agent, t.session, t.pane_id, t.workspace_id,
            t.tab_id, t.seq, rec["attn"], rec["attn_ns"])

