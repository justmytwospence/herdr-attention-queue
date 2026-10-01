# Changelog

## 0.13.0

- The pi bridge ships with the plugin (`pi/herdr-attention-bridge.ts`) and the
  plugin installs it into pi's extensions directory on every server, keeping it
  current; `{"pi_bridge": false}` removes it. Remove any copy installed by hand.
  A second copy loaded beside it goes quiet.
- `herdr:background` event-bus holds: any pi extension can report background
  work that will wake the agent, beside the pi-subagents and
  pi-background-tasks adapters.
- Turns that end asking you something show blocked until your next prompt.
  Jev judges the final message (`attention.py ask-check`); the pi bridge asks
  after every turn, and Claude Code and Codex `Stop` hooks can call it. Needs
  `TYPESAFE_API_KEY`.
- `attention.py activity STATE` reports the `activity` token from any
  harness's hooks.

## 0.12.0

- `jump-attention` cycles through every blocked, working and waiting agent in
  the Agents panel's order and wraps back to the most urgent one. Done and idle
  agents are skipped.
- `activity=idle`: a harness can say its turn is over. The token now stands in
  for herdr's status, with completions of its own: working to idle by the
  report is a finished turn (done), herdr's completion signals are ignored
  while it reports, and herdr's blocked still wins. Codex, which herdr reads
  as unknown after a response, no longer stays working forever once its hooks
  report. A plan-mode planner run that ends now shows done.

## 0.11.0

- `jump-attention` follows the Agents panel's order and cycles: from outside
  the most urgent tier (blocked, else done) it goes to the tier's first row;
  from a row of the tier, to the next one, wrapping. A lone tier row stays.
- Much faster. The action only appends a `jump` line; the notifier does the
  jump over the ssh pipes it already holds. `follow` now answers `list`,
  `focus` and `notice` commands on its input (milliseconds, where each
  `herdr --machine` call opened a new ssh connection), and polls the log every
  50 ms instead of 250 ms. The Ghostty keystroke tries the frontmost terminal
  first. Restart the notifier after updating.

## 0.10.0

- `jump-attention` spans machines also while the client shows a remote one.
  The remote server cannot see the other machines, so it appends a `jump` line
  to its transition log and the notifier on the client's host, which follows
  that log, does the cross-machine jump. Without a notifier reading the log,
  the jump stays on that machine. Restart the notifier after updating.

## 0.9.0

- `jump-attention` spans every connected machine, like the sidebar: a blocked
  agent on the NUC now beats a done one on Local. Remote agents are focused on
  their server, and the client is switched to them with the notifier's Ghostty
  `focus_agent` key (macOS); elsewhere a notice names the machine and agent.
  It spans machines while the client shows Local, whose host has the saved
  machines.

## 0.8.1

- Places use ` › ` between parts (`homelab › navigation`) instead of ` > `.

## 0.8.0

- `usage` names its window with a leading icon (clock: 5-hour block, calendar:
  7-day window, wand: a model's cap, cash: extra spend) instead of text, and
  marks a problem with a trailing badge (alert: warn, octagon: critical).
  Colour it with `contains` rules on the badges; update display rules that
  matched the 0.7.0 gauges.
- `attn_row` tells agents in the same workspace apart: `homelab > navigation`
  and `homelab > attention queue` by tab, then by agent kind and pane within
  one tab. Notifier titles use the same place. Hooks on `tab.renamed` and
  `pane.moved` keep it current.

## 0.7.0

- `activity` pane token: any harness can correct herdr's status with
  `blocked` (waiting on the user: a question no screen rule knows, or an agent
  whose integration lost the pane) or `working` (busy outside a turn, such as a
  plan-mode planner run). Blocked from either source wins; reported working
  beats idle, done and waiting. The ticker watches the token while it is set.
- `refresh` action: reconcile now, for integrations that change `activity` or
  `bg` (token changes emit no plugin event).
- `usage` shows only the window most likely to stop work, ranked by level and
  by its projected use at the reset, with a leading gauge for the level: ok,
  warn (75%, or on pace to hit the cap before the reset) or critical (90%, or on
  pace to hit it within the hour). Colour it with `starts_with` rules on the
  gauges (see `examples/config.toml`). Extra-usage spend shows only once it is
  itself nearly spent.

## 0.6.0

- Replace `next-attention` / `previous-attention` traversal with a single
  `jump-attention` action: always focus the highest-priority blocked/sticky-done
  obligation. Stay at the head until the agent is acted on; focus never reviews.
- Revalidation also catches newly arrived higher-priority candidates.
- Example binding is Ctrl-b Enter. Update existing traversal bindings when upgrading;
  the old action IDs are removed. State format and acknowledgement are unchanged.

## 0.5.0

- `next-attention` / `previous-attention` actions traverse blocked and sticky-done
  agents on the selected server/session. The example binds Ctrl-b Alt-n / Alt-p
  like tmux's alert-window navigation. Priority, millisecond timestamps and layout
  ties match the sidebar. Navigation never acknowledges work or changes the view.
- Revalidate agent identity/location/eligibility before focus, with one reselection
  on churn; focus runs outside the state lock and ambiguous timeouts are not retried.

## 0.4.2

- A running ticker notices when the plugin's code changes (an update or a
  submodule bump) and hands over to a fresh ticker. Before, it kept writing
  tokens with the old code for up to an hour, undoing the update.

## 0.4.1

- `attn_row` puts two spaces between the icon and the workspace name; with one,
  the Nerd Font glyph looked attached to the name.

## 0.4.0

- `attn_row` token: the state icon and the workspace label as one value
  (`\uf058 data-pipeline`). herdr separates row tokens with " · " except after
  its built-in state_icon, so `$attn_icon` followed by `workspace` read
  "icon · name". Render `$attn_row` with `starts_with` rules on the glyphs; the
  whole label takes the state colour. A `workspace.renamed` hook keeps it
  current. `attn_icon` stays for layouts that want the glyph alone.

## 0.3.1

- The notifier's machine switch works: it writes the prefix and alt+N as
  kitty-protocol sequences with Ghostty's `perform action`. Ghostty's `send key`
  sends no text or codepoint, so under the kitty keyboard protocol the herdr
  client enables it produced nothing. A prefix other than ctrl+b is set with
  `"prefix_csi"` (the key's CSI body, e.g. `"32;5u"` for ctrl+space).

## 0.3.0

- **waiting** state (rank 3, between working and idle): herdr says working on a
  `background_*` detection rule (Claude background agents or MCP tasks), or the
  agent's `bg` token counts pending background work after its turn ended. A
  finished turn with background work pending waits, then becomes done.
- `attn_icon` token: a Nerd Font glyph per state, so rows can show a coloured
  icon instead of the word. Ranks are now blocked 0, done 1, working 2,
  waiting 3, idle 4, unknown 5.
- On herdr 0.9.2+, herdr's `completion_seq` decides done, so restored agents
  and pi `/new` no longer read done; the old heuristic stays for older servers.
- A detached ticker polls every 3 s while agents are working or waiting, since
  detection rules and tokens change without events.
- Transition log (`transitions.jsonl`) and `attention.py follow`; a
  `pane.focused` hook logs focus.
- `attention.py notifier`: macOS notifications for every herdr machine (local
  and saved machines over ssh) that focus the right Ghostty terminal, machine
  and pane on click. Replaces herdr-focus-notify.
- `verify.py` reports versions, checks icons and `bg`, and runs on every
  machine with `--all-machines`.
- Requires herdr 0.9.1: the selected machine's view orders every machine's
  agents, so the combined list is one queue and never reorders on a click.

## 0.2.1

- A failed usage fetch (no token, HTTP 429, offline) waits five minutes before
  the next try instead of retrying on every hook.

## 0.2.0

- `usage` token: Claude plan usage (5-hour block, 7-day window, per-model weekly
  caps, extra-usage spend) on every agent row, from the OAuth usage endpoint with
  Claude Code's stored token. Cached for 5 minutes and refreshed by one detached
  process, so hooks never wait on the network.
- Claude panes take the session name Claude Code assigned as their title and
  agent label.
- Both are on by default and can be turned off in `config.json` in the plugin
  config directory. `clear` also removes them.

## 0.1.0

- Sticky done: an agent that finishes a turn stays done until it works again or
  is marked reviewed, instead of clearing when its pane is viewed.
- Attention-ordered Agents view (blocked, done, working, idle) sorted only by
  plugin tokens, so viewing or clicking never reorders rows.
- Actions: mark-reviewed, mark-unread, mark-all-reviewed, reapply, clear.
- Done state survives server restarts, keyed by agent conversation.
