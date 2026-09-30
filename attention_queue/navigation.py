"""Pure selection in the rendered, server-local attention queue."""


def candidates(agents, live):
    # Python's stable sort preserves agent.list's workspace/tab/pane layout
    # order for equal millisecond timestamps, matching the sidebar's view.
    eligible = [t for t in agents if live[t.terminal_id]["attn"] in ("blocked", "done")]
    return sorted(eligible, key=lambda t: (
        0 if live[t.terminal_id]["attn"] == "blocked" else 1,
        live[t.terminal_id]["attn_ns"] // 10**6,
    ))


def select(agents, live, anchor, direction):
    queue = candidates(agents, live)
    if not queue:
        return None
    index = next((i for i, t in enumerate(queue) if t.pane_id == anchor), None)
    return queue[0] if index is None else queue[(index + direction) % len(queue)]


def signature(t, live):
    """Identity, location and eligibility epoch for immediate revalidation."""
    rec = live[t.terminal_id]
    return (t.terminal_id, t.agent, t.session, t.pane_id, t.workspace_id,
            t.tab_id, t.seq, rec["attn"], rec["attn_ns"])
