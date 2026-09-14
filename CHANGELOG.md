# Changelog

## 0.1.0

- Sticky done: an agent that finishes a turn stays done until it works again or
  is marked reviewed, instead of clearing when its pane is viewed.
- Attention-ordered Agents view (blocked, done, working, idle) sorted only by
  plugin tokens, so viewing or clicking never reorders rows.
- Actions: mark-reviewed, mark-unread, mark-all-reviewed, reapply, clear.
- Done state survives server restarts, keyed by agent conversation.
