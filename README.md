# herdr-attention-queue

A [herdr](https://herdr.dev) plugin that turns the Agents panel into an
attention queue:

1. **blocked**: waiting on an approval or a question
2. **done**: finished a turn you have not acted on yet
3. **working**: running a turn, or busy outside one (a plan-mode planner run)
4. **waiting**: its turn is paused or over, but background work it started
   (subagents, background tasks) will wake it
5. **idle**

"done" is sticky. herdr's own `done` means "idle and not yet seen", so it turns
back into `idle` the moment you look at the pane, and a finished agent drops
out of the queue before you have dealt with it. Here an agent stays done until
it works again (you sent a prompt or answered it) or you mark it reviewed.
Looking at a pane, or clicking a row, never reorders the panel.

Within each group, rows keep the order in which they entered it. With several
herdr machines connected, the list is one queue across all of them.

## Requirements

- herdr 0.9.1 or newer on every server (0.9.2 or newer recommended: its
  `completion_seq` makes done exact)
- `python3` 3.9 or newer on the PATH of the herdr **server** (stdlib only)
- macOS or Linux; the notifier needs macOS, Ghostty and
  [alerter](https://github.com/vjeantet/alerter)

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

- **Sort mode.** Set `agent_panel_sort = "spaces"`. The plugin view orders the
  list; this only applies during brief fallbacks, such as a machine
  reconnecting, and "spaces" is the fallback that never reorders rows on a
  click. If you ever clicked the Agents header's sort toggle, herdr saved that
  choice in `~/.local/state/herdr/client-shell/*.json`, and it overrides config:
  detach, delete the `agent_panel_sort` key from that file, and reattach.
- **Rows.** Render `$attn_row` (state icon and place, one token) in
  `[ui.sidebar.agents]` rows, coloured with `starts_with` rules on the glyphs.
  Display rules style a token by its own value and cannot replace its text, so
  the icon is a token of its own; and herdr puts " · " between any two row
  tokens except after its built-in `state_icon`, so the icon and workspace share
  one token to read "icon name". The whole label takes the state colour.
- **Keys.** Bind actions with `type = "plugin_action"`, and `focus_agent` for
  the notifier's machine switch.
- **Window title.** `window_title = "herdr {hostname}: {workspace}"` lets the
  notifier find the terminal running the herdr client.

Tokens reported on each agent pane (source `plugin:attention-queue`):

| token | values |
|---|---|
| `attn` | `blocked`, `done`, `working`, `waiting`, `idle`, `unknown` |
| `attn_rank` | `0` to `5`, in that order |
| `attn_ts` | wall-clock milliseconds when the agent entered its current state, zero-padded |
| `attn_icon` | a Nerd Font glyph per state (below) |
| `attn_row` | the glyph, two spaces, and where the agent is, e.g. `\uf058  data-pipeline` (see below) |
| `usage` | the Claude usage window closest to its cap, e.g. `󰃭 30% 4d` (see below) |

The place in `attn_row` is the workspace label, made only as long as it needs
to be to tell agents apart: with several agents in a workspace it adds the tab
(`homelab › navigation`, `homelab › attention queue`); with several in one tab,
the agent kind (`homelab › main › claude`), then the pane when that repeats too
(`homelab › main › pi p2`). The notifier titles use the same place.

| state | icon | glyph |
|---|---|---|
| blocked | exclamation circle | `\uf06a` |
| done | check circle | `\uf058` |
| working | play circle | `\uf144` |
| waiting | hourglass | `\uf252` |
| idle | empty circle | `\uf10c` |
| unknown | question circle | `\uf059` |

## Waiting

An agent is **waiting** when its own turn is paused or over but background work
it started will wake it. Two signals, in this order after blocked:

1. herdr says working, and the detection rule that matched is a background one
   (`background_*`), such as Claude's "Waiting for 2 background agents" or a
   background MCP task.
2. herdr says idle or done, and the pane's `bg` token counts pending background
   work.

A turn that ends while background work is pending shows waiting. When the work
finishes without starting a new turn, the row becomes done.

Agents that report their own state (pi, OMP, OpenCode) are not screen-detected,
so they report background work themselves: set the pane token `bg` to the
number of pending tasks, with a TTL so a crashed agent cannot leave it behind:

```sh
"$HERDR_BIN_PATH" pane report-metadata "$HERDR_PANE_ID" \
  --source user:my-bridge --token bg=2 --ttl-ms 90000
```

Clear it (or let it expire) at zero. Long tool calls inside a turn, such as
sleeps or monitors, are working, not waiting.

## Agent-reported state

herdr's status comes from an agent's integration or from screen rules, and
both miss things:

- a question drawn by an extension or tool that no screen rule knows;
- an agent whose spinner stays on screen under a question;
- work outside a turn, like a plan-mode planner run started by a command;
- agents herdr cannot read after a response: Codex falls back to `unknown`,
  which on its own would keep a finished Codex turn showing as working.

Any harness's hooks can report the agent's status in the pane token
`activity`, which then stands in for herdr's status:

| value | meaning |
|---|---|
| `blocked` | waiting on the user |
| `working` | running a turn, or working outside one |
| `idle` | the turn is over |

- herdr's own blocked always wins: a screen rule saw a prompt the hooks missed.
- A turn the token ends (working to idle, by the token changing or being
  cleared) is a completion, so the agent shows as done.
- While the token is set, herdr's own completion signals are not consulted.
  Answering a reported question is not a completion.
- Report with a TTL so a crashed agent cannot leave it behind, and clear it at
  session start.

A token change emits no plugin event, so ask for a refresh after each change
(otherwise it shows on the next agent event; while an agent reports blocked or
working, the ticker also watches it):

```sh
"$HERDR_BIN_PATH" pane report-metadata "$HERDR_PANE_ID" \
  --source user:my-bridge --token activity=blocked --ttl-ms 86400000
"$HERDR_BIN_PATH" plugin action invoke attention-queue.refresh
```

[justmytwospence/dotfiles](https://github.com/justmytwospence/dotfiles) wires
the three harnesses it uses:

| harness | how |
|---|---|
| pi | `shell/.pi/agent/extensions/herdr-attention-bridge.ts`: blocked for every extension dialog (`ui_prompt_start`) and `herdr:blocked` hold; working while an extension holds `herdr:working` (pi-plan-mode's planner runs and replies, whose trace view is a progress view, not a question) |
| Claude Code | `shell/.local/bin/herdr-activity` from hooks: blocked from `PreToolUse` to `PostToolUse` of `AskUserQuestion` and `ExitPlanMode`; cleared on prompt, stop and session start. Permission prompts are left to herdr's screen rules: no hook fires when one is answered, only when the tool finishes |
| Codex | the same script: working on `UserPromptSubmit`, idle on `Stop`, cleared on session start. Approval prompts are left to herdr's screen rules, which win over the report |

## Claude usage and session names

Two extras, both on by default:

- **`usage`**: on every agent row, whichever agent it is, the one Claude plan
  window most likely to stop work, with its use and time to reset. Its leading
  icon names the window:

  | window | icon |
  |---|---|
  | 5-hour block | `\U000F0954` (clock) |
  | 7-day window | `\U000F00ED` (calendar) |
  | a model's weekly cap | `\U000F0068` (wand) |
  | extra-usage spend, only once it is itself nearly spent and extra usage is on | `\U000F0114` (cash) |

  Windows are ranked by level, then by the use projected at their reset at the
  current pace. A trailing badge marks a problem level, for colouring with
  `contains` rules; ok has none:

  | level | badge | when |
  |---|---|---|
  | ok | | none of the below |
  | warn | `\U000F0026` | 75% used, or on pace to hit the cap before the reset |
  | critical | `\U000F0029` | 90% used, or on pace to hit the cap within the hour |

  Pace counts once a fifth of the window has passed and 30% is used; earlier it
  is noise. A non-normal `severity` from the API raises the level. The data comes from
  the OAuth usage endpoint Claude Code's `/status` reads, with the token Claude
  Code stores (the macOS Keychain item `Claude Code-credentials`, else
  `~/.claude/.credentials.json`, or `CLAUDE_CODE_OAUTH_TOKEN`). The reply is
  cached in the plugin state directory for 5 minutes; hooks never wait on the
  network, and a stale cache is refreshed by one detached process that then
  updates every row. Without a Claude login there is no token and no usage.
  Render it as in [`examples/config.toml`](examples/config.toml); the icons
  are Nerd Font glyphs.
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
| `attention-queue.jump-attention` | focus the next most urgent agent, in panel order, on any connected machine |
| `attention-queue.mark-reviewed` | clear done on the focused agent |
| `attention-queue.mark-unread` | put the focused idle agent back into done |
| `attention-queue.mark-all-reviewed` | clear done on every agent on this server |
| `attention-queue.reapply` | set the Agents view and refresh every agent's tokens |
| `attention-queue.refresh` | reconcile every agent now, after an integration changed `activity` or `bg` |
| `attention-queue.clear` | remove the plugin's tokens and view (run before uninstalling) |

### Attention navigation

The example binds **Ctrl-b Enter**. It jumps to the most urgent agents in the
Agents panel's order, across every connected machine:

- The tier is every `blocked` agent, or every sticky `done` one when nothing is
  blocked. Working, waiting, idle and unknown agents are never targets.
- From outside the tier, go to its first row in the panel.
- From an agent of the tier, go to the next one in panel order, wrapping around
  after the last. The only agent of its tier stays put.
- Nothing needing attention shows a notice.

Responding removes a blocked agent from the tier; marking it reviewed (`Ctrl-b
a`) removes a done one. **Focusing never reviews.** Focus reaches the exact
pane, including zoomed tabs. The current agent is the pane the action ran from,
never another client's focus.

The jump runs in the notifier (see Notifications), which already holds an ssh
pipe to every machine's `follow`:

1. The action, on whichever server the client shows, appends a `jump` line to
   its transition log and exits. It does no herdr calls.
2. The notifier asks every machine for its agents over its pipe (a few
   milliseconds each) and rebuilds the combined panel order from the plugin's
   tokens, as the client does.
3. A target on the machine the client shows is focused over that machine's
   pipe. A target on another machine needs the client to switch, which herdr
   has no API for, so Ghostty sends the `focus_agent` key (prefix, then alt+N)
   for its row N. That needs macOS, Ghostty, `window_title` starting with
   `herdr `, `focus_agent = "prefix+alt+1..9"` and `prefix_csi` if the prefix is
   not ctrl+b. Above row 9 the agent is focused on its server and a notice
   names it.

The action hands over only while a notifier reads its log (`follow` keeps
`follower.alive` in the session state directory fresh). Without one it cycles
among that server's agents alone. Requests older than 10 seconds are dropped,
so a reconnecting notifier never replays one.

## Multiple machines

Install the plugin on every herdr server you connect to with `herdr machine`.
From herdr 0.9.1 the selected machine's view orders every machine's agents
together, and since every server sets the same view, the order does not change
when you select another machine or click a row. The plugin sorts only by its own
tokens, never by herdr's per-server `state_change_seq`. Keep the machines'
clocks NTP-synced: they order rows within a state.

A server older than 0.9.1 sends no view; while it is selected, the client falls
back to one block per machine.

`python3 scripts/verify.py --all-machines` checks this server and every saved
machine: herdr and plugin versions, tokens, and invariants.

## Notifications (macOS)

`attention.py notifier` sends a clickable notification when an agent on any
machine becomes blocked or done, and replaces herdr's own desktop alerts, which
only bring the terminal forward. Keep `[ui.toast] delivery = "herdr"` for the
in-app toasts.

- It follows the local server's transition log, and each saved machine's over
  ssh (`BatchMode`, so key-based login is required), reconnecting with backoff.
  It re-reads `herdr machine list` every minute.
- Title: `<workspace> · <agent> needs input` or `finished`; the machine is the
  subtitle for remote agents; the agent's icon when known.
- Nothing is sent when you are already looking: Ghostty is frontmost, its
  focused terminal is the herdr client, the client shows that machine, and that
  server's focused pane is the agent's. If a check cannot tell, it notifies.
- The notification goes away when the agent leaves blocked or done, or its
  pane gets focus.
- Lines older than 15 minutes, restart replays and first sightings are ignored;
  per-machine progress survives restarts of the notifier.

Clicking it:

1. focuses the Ghostty terminal whose title starts with `herdr ` (else just
   brings Ghostty forward);
2. focuses the agent's pane on its own server (`agent.focus`, over
   `herdr --machine` for remote ones);
3. if the client shows another machine, sends the `focus_agent` key (prefix,
   then alt+N) for the agent's position N in the combined list, then confirms
   the switch and retries once. It writes the keys as kitty-protocol sequences
   with Ghostty's `perform action`, assuming the prefix is ctrl+b; set
   `"prefix_csi"` to your prefix's CSI body otherwise (`"32;5u"` is ctrl+space). herdr has no API to make a client switch
   machines. Above position 9 there is no key; the pane is still focused on its
   machine.

Setup:

```sh
brew install vjeantet/tap/alerter
# every server: window_title and focus_agent as in examples/config.toml
cp examples/com.example.herdr-attention-notifier.plist ~/Library/LaunchAgents/
# edit PLUGIN_ROOT and HOME in it, then
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.herdr-attention-notifier.plist
```

The first click asks for Automation permission for python3 to control Ghostty.
Options go under `"notifier"` in `config.json` in the plugin config directory:

```json
{"notifier": {"local_session": "default",
              "remote_entry": "~/dotfiles/plugins/herdr-attention-queue/attention.py",
              "alerter": "/opt/homebrew/bin/alerter",
              "prefix_csi": "98;5u"}}
```

`remote_entry` defaults to the local checkout's path relative to your home
directory; it can also map machine labels to paths. The log is
`notifier/notifier.log` in the plugin state directory.

## How it works

- Hooks on `pane.agent_status_changed`, `pane.agent_detected`, `pane.closed`,
  `pane.moved`, `workspace.renamed` and `tab.renamed` re-read `agent.list` under a lock and advance each agent through a pure state
  machine (`attention_queue/model.py`).
  - herdr events carry no previous status, and viewing a pane emits no event.
  - On herdr 0.9.2+ a completion is herdr's `completion_seq`. On older servers
    it is detected from the stored record: the agent was working and is now
    idle.
  - Hooks that run out of order converge on herdr's current truth.
- Background detection rules and `bg` tokens change without any plugin event.
  While any agent is working or waiting, a detached ticker reconciles every 3 s
  and asks `agent.explain` which rule matched for screen-detected working
  agents. It exits after two quiet passes.
- Tokens are written only when they differ from what herdr already holds.
- Every attention change appends a line to `transitions.jsonl` in the session's
  state directory (rotated at 256 KB), and a `pane.focused` hook logs focus.
  `attention.py follow --session S --since-ms N` prints and follows it; the
  notifier reads it.
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
- Waiting covers background work that will wake the agent. Claude background
  shell tasks have no background detection rule and show working or idle, as
  do agents that report state without a `bg` token.
- A screen-detected agent's question shows blocked only if a herdr screen rule
  knows it, or the agent reports `activity`.
- The notifier's key jump can land in a herdr popup if one is open.

## Development

```sh
python3 -m unittest discover -s tests -t . -v
# Opt-in: creates and deletes only a uniquely named, isolated Herdr test session.
HERDR_NAVIGATION_LIVE=1 python3 -m unittest tests.test_navigation_live -v
python3 scripts/verify.py        # read-only check against a live server
python3 scripts/verify.py --all-machines
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
