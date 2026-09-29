# Changelog

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
