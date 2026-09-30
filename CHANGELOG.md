# Changelog

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
