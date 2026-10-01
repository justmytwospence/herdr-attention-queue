"""Pure selection in the rendered, server-local attention queue."""


def candidates(agents, live):
    # Python's stable sort preserves agent.list's workspace/tab/pane layout
    # order for equal millisecond timestamps, matching the sidebar's view.
    eligible = [t for t in agents if live[t.terminal_id]["attn"] in ("blocked", "done")]
    return sorted(eligible, key=lambda t: (
        0 if live[t.terminal_id]["attn"] == "blocked" else 1,
        live[t.terminal_id]["attn_ns"] // 10**6,
    ))


def cycle(rows, here):
    """Index of the row to jump to, or None when nothing needs attention.

    `rows` are (key, attn) in the Agents panel's visual order; `here` is the
    current agent's key. The most urgent state present (blocked, else done)
    forms the tier. Outside the tier, go to its first row; on a row of the
    tier, go to the next one, wrapping. A lone tier row that is `here` is
    returned as is: stay.
    """
    states = {attn for _, attn in rows}
    tier = next((s for s in ("blocked", "done") if s in states), None)
    if tier is None:
        return None
    tied = [i for i, (_, attn) in enumerate(rows) if attn == tier]
    keys = [rows[i][0] for i in tied]
    if here in keys:
        return tied[(keys.index(here) + 1) % len(tied)]
    return tied[0]


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

