# herdr-attention-queue

A [herdr](https://herdr.dev) plugin that turns the Agents panel into an
attention queue:

1. **blocked**: waiting on an approval or a question
2. **done**: finished a turn you have not acted on yet
3. **working**
4. **idle**

"done" is sticky. herdr's own `done` means "idle and not yet seen", so it turns
back into `idle` the moment you look at the pane, and a finished agent drops
out of the queue before you have dealt with it. Here an agent stays done until
it works again (you sent a prompt or answered it) or you mark it reviewed.
Looking at a pane, or clicking a row, never reorders the panel.

Within each group, rows keep the order in which they entered it.

## Requirements

- herdr 0.9.0 or newer
- `python3` 3.9 or newer on the PATH of the herdr **server** (stdlib only)
- macOS or Linux

## Install

```sh
herdr plugin install justmytwospence/herdr-attention-queue --yes
# or, from a checkout:
herdr plugin link /path/to/herdr-attention-queue
```

Installing or linking does not run the plugin's startup hook, so apply the view
once (a server restart does the same):

```sh
herdr plugin action invoke attention-queue.reapply
```

## Configure the client

The plugin reports tokens and an Agents view. Row appearance and keybindings are
client config; see [`examples/config.toml`](examples/config.toml).

- **Sort mode.** Set `agent_panel_sort = "spaces"`. With more than one herdr
  machine connected, herdr 0.9.0's `priority` mode re-sorts the combined list
  itself and discards plugin views. If you ever clicked the Agents header's sort
  toggle, herdr saved that choice in
  `~/.local/state/herdr/client-shell/*.json`, and it overrides config: detach,
  delete the `agent_panel_sort` key from that file, and reattach.
- **Rows.** Render `$attn` in `[ui.sidebar.agents]` rows, styled with rules.
- **Keys.** Bind actions with `type = "plugin_action"`.

Tokens reported on each agent pane (source `plugin:attention-queue`):

| token | values |
|---|---|
| `attn` | `blocked`, `done`, `working`, `idle`, `unknown` |
| `attn_rank` | `0` to `4`, in that order |
| `attn_ts` | wall-clock milliseconds when the agent entered its current state, zero-padded |
| `usage` | Claude plan usage, e.g. `󰥔 38% 7h55m 󰃭 15% 6d 󰁨 8% 󰄔 $154.21/$150 off` (see below) |

## Claude usage and session names

Two extras, both on by default:

- **`usage`**: the Claude plan gauges on every agent row, whichever agent it is:
  the 5-hour block and 7-day window with time to reset, each model's weekly cap,
  and extra-usage spend (`off` when extra usage is disabled). The data comes from
  the OAuth usage endpoint Claude Code's `/status` reads, with the token Claude
  Code stores (the macOS Keychain item `Claude Code-credentials`, else
  `~/.claude/.credentials.json`, or `CLAUDE_CODE_OAUTH_TOKEN`). The reply is
  cached in the plugin state directory for 5 minutes; hooks never wait on the
  network, and a stale cache is refreshed by one detached process that then
  updates every row. Without a Claude login there is no token and no gauge.
  Render it with `{ token = "$usage", dim = true }`; the icons are Nerd Font
  glyphs.
- **Claude session names**: a Claude pane's title and agent label become the
  name Claude Code gave the session (`Claude: wireguard snowflake routing`),
  read from `~/.claude/jobs/<session>/state.json`.

Turn either off in `config.json` in the plugin config directory
(`herdr plugin config-dir attention-queue`):

```json
{"usage": false, "claude_names": false}
```

## Actions

| action | does |
|---|---|
| `attention-queue.mark-reviewed` | clear done on the focused agent |
| `attention-queue.mark-unread` | put the focused idle agent back into done |
| `attention-queue.mark-all-reviewed` | clear done on every agent on this server |
| `attention-queue.reapply` | set the Agents view and refresh every agent's tokens |
| `attention-queue.clear` | remove the plugin's tokens and view (run before uninstalling) |

## Multiple machines

Install the plugin on every herdr server you connect to with `herdr machine`:
each server orders its own agents.

On herdr 0.9.0 the client shows each machine's agents as a block, local machine
first. herdr PR #3784 (merged, not yet released) lets the selected machine's view
order every machine's agents together. The plugin sorts only by its own tokens,
never by herdr's per-server `state_change_seq`, so once the client and every
server run a release with that change the panel becomes one queue. Keep the
machines' clocks NTP-synced.

## How it works

- Hooks on `pane.agent_status_changed`, `pane.agent_detected` and `pane.closed`
  re-read `agent.list` under a lock and advance each agent through a pure state
  machine (`attention_queue/model.py`).
  - herdr events carry no previous status, and viewing a pane emits no event.
  - So a completion is detected from the stored record: the agent was working
    and is now idle.
  - Hooks that run out of order converge on herdr's current truth.
- Tokens are written only when they differ from what herdr already holds.
- Durable state is keyed by the agent's conversation (`agent_session`), under
  the plugin state directory, one namespace per herdr session.
- herdr keeps tokens and views in memory only. After a restart, the startup
  hook reapplies the view and a short detached reseed restores done states as
  agents come back.

## Limitations

- Interrupting a turn (Ctrl-C) looks like a completion. Mark it reviewed.
- For 30 seconds after a server restart, completions are ignored. herdr
  relaunches agents in that window and reports spurious state changes.
- Agents without a native session reference lose their done state across
  restarts.
- Tokens stay on panes if the plugin is removed without `clear`, until the panes
  close or the server restarts. The view clears itself.
- The usage countdown advances when agents change state; with every agent idle
  it can lag until the next event.

## Development

```sh
python3 -m unittest discover -s tests -t . -v
python3 scripts/verify.py        # read-only check against a live server
```

Set `HERDR_ATTENTION_QUEUE_DEBUG=1` in the server environment to log hook
activity to `debug.log` in the plugin state directory.

## Uninstall

```sh
herdr plugin action invoke attention-queue.clear
herdr plugin uninstall attention-queue   # or: herdr plugin unlink attention-queue
```

## License

MIT
