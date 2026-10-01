"""Pure selection in the rendered, server-local attention queue."""


def candidates(agents, live):
    # Python's stable sort preserves agent.list's workspace/tab/pane layout
    # order for equal millisecond timestamps, matching the sidebar's view.
    eligible = [t for t in agents if live[t.terminal_id]["attn"] in ("blocked", "done")]
    return sorted(eligible, key=lambda t: (
        0 if live[t.terminal_id]["attn"] == "blocked" else 1,
        live[t.terminal_id]["attn_ns"] // 10**6,
    ))


def select(agents, live):
    """Highest-priority actionable obligation, independent of the caller."""
    queue = candidates(agents, live)
    return queue[0] if queue else None


def signature(t, live):
    """Identity, location and eligibility epoch for immediate revalidation."""
    rec = live[t.terminal_id]
    return (t.terminal_id, t.agent, t.session, t.pane_id, t.workspace_id,
            t.tab_id, t.seq, rec["attn"], rec["attn_ns"])


def _remote_ms(agent: dict):
    try:
        return int((agent.get("tokens") or {}).get("attn_ts") or "")
    except ValueError:
        return None


def select_across(agents, live, remotes):
    """Highest-priority obligation across this server and the saved machines.

    `remotes` is [(machine key, raw agent.list dicts or None)] in the client's
    machine order after this one; their attention comes from the tokens their
    own plugin wrote. Returns (None, Truth) for a local agent, (machine key,
    pane id) for a remote one, or None. Blocked first, then sticky done; oldest
    state-entry millisecond first; ties keep machine order, then list order.
    """
    rows = []
    local = candidates(agents, live)
    if local:
        t = local[0]
        rec = live[t.terminal_id]
        rows.append(((0 if rec["attn"] == "blocked" else 1, rec["attn_ns"] // 10**6, 0, 0), (None, t)))
    for m_index, (key, remote_agents) in enumerate(remotes or (), start=1):
        for a_index, agent in enumerate(remote_agents or ()):
            attn = (agent.get("tokens") or {}).get("attn")
            ms = _remote_ms(agent)
            if attn not in ("blocked", "done") or ms is None or not isinstance(agent.get("pane_id"), str):
                continue
            rows.append(((0 if attn == "blocked" else 1, ms, m_index, a_index), (key, agent["pane_id"])))
    if not rows:
        return None
    return min(rows, key=lambda row: row[0])[1]
