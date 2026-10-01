// herdr-attention-queue's pi bridge. The plugin installs it into pi's
// extensions directory on every herdr server it runs on (see pi_bridge.py), so
// pi in a herdr pane reports what herdr's own pi integration does not:
//
// 1. blocked: pi emits ui_prompt_start/ui_prompt_end around every blocking
//    extension dialog (ask_user_question, plan-mode questions and menus,
//    confirmations). They are forwarded as herdr:blocked pairs, which herdr's
//    pi integration turns into the blocked state. A dialog opened while an
//    extension holds `herdr:working` is a progress view (plan-mode's live
//    planner traces), not a question, and is not forwarded until the work ends.
// 2. questions at the end of a turn: when a turn ends by asking the user
//    something ("Want me to push?"), the plugin's Jev judge
//    (`attention.py ask-check`) says so and the agent shows blocked until the
//    next prompt. Off without TYPESAFE_API_KEY.
// 3. activity: the pane token `activity` says blocked (an open dialog, a
//    question, or another extension's herdr:blocked hold) or working (an
//    extension's herdr:working hold), so it holds even where herdr reads the
//    screen.
// 4. waiting: the pane token `bg` counts background work that will wake the
//    agent, which herdr-attention-queue shows as waiting.
//
// Event-bus conventions any extension can use, each as {active: true, label}
// then {active: false} pairs:
//
//   herdr:blocked     waiting on the user (herdr's pi integration counts these too)
//   herdr:working     working outside the agent's turn (e.g. planner runs)
//   herdr:background  background work that will wake the agent
//
// plus two adapters: pi-subagents' herdr:busy, and pi-background-tasks' tasks
// with triggerOnCompletion.
//
// Tokens have a 90 s TTL, are refreshed every 30 s while set, and are cleared
// when their state ends, so a crashed pi cannot leave them behind. After each
// change the bridge asks the plugin to refresh, since token changes emit no
// plugin event. Only a TUI root session inside herdr reports. /reload applies
// an update to a running session.
import { execFile, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// The installer writes the plugin's checkout here; see pluginRoot().
const PLUGIN_ROOT = "__HERDR_ATTENTION_QUEUE_ROOT__";
const SOURCE = "user:attention-bridge";
const BG_TOKEN = "bg";
const ACTIVITY_TOKEN = "activity";
const TTL_MS = 90_000;
const REFRESH_MS = 30_000;
const STATUS_TIMEOUT_MS = 2_000;
const ASK_TIMEOUT_MS = 15_000;
const REQUEST = "pi-background-tasks:request:v1";
const RESPONSE = "pi-background-tasks:response:v1";
const TERMINAL = "pi-background-tasks:terminal:v1";
const REQUEST_SCHEMA = "pi-background-tasks.extension-request.v1";
export const BLOCKED = "herdr:blocked";
export const WORKING = "herdr:working";
export const BACKGROUND = "herdr:background";
const SUBAGENTS_BUSY = "herdr:busy";
const REFRESH_ACTION = "attention-queue.refresh";
// A dialog is forwarded as blocked only after this grace period, so a progress
// view whose herdr:working hold starts just after it opens, or one that closes
// right as its work ends, never flashes blocked.
const GRACE_MS = 150;
// Two copies loaded at once (an old install beside a new one, or a reload that
// keeps the old runtime alive) would report everything twice: the most recently
// loaded copy wins and earlier ones go quiet.
const LOADED = Symbol.for("herdr-attention-queue.pi-bridge");

export type Activity = "blocked" | "working";

export interface Report {
  bg: number;
  activity?: Activity;
}

export interface Bus {
  emit(channel: string, data: unknown): void;
  on(channel: string, handler: (data: unknown) => void): (() => void) | void;
}

export interface Task {
  id?: string;
  status?: string;
  triggerOnCompletion?: boolean;
}

/** Background tasks that will start a new turn when they finish. */
export function wakingTasks(tasks: readonly Task[]): number {
  return tasks.filter((task) => task?.status === "running" && task.triggerOnCompletion === true).length;
}

export function bgCount(subagentsBusy: boolean, holds: number, tasks: readonly Task[]): number {
  return (subagentsBusy ? 1 : 0) + holds + wakingTasks(tasks);
}

/** herdr CLI arguments that set, refresh or clear both tokens. */
export function reportArgs(pane: string, report: Report, seq: number): string[] {
  const args = ["pane", "report-metadata", pane, "--source", SOURCE, "--seq", String(seq)];
  let set = false;
  if (report.bg > 0) {
    args.push("--token", `${BG_TOKEN}=${report.bg}`);
    set = true;
  } else {
    args.push("--clear-token", BG_TOKEN);
  }
  if (report.activity) {
    args.push("--token", `${ACTIVITY_TOKEN}=${report.activity}`);
    set = true;
  } else {
    args.push("--clear-token", ACTIVITY_TOKEN);
  }
  if (set) args.push("--ttl-ms", String(TTL_MS));
  return args;
}

/** The text of the turn's final assistant message, if the turn ended normally. */
export function finalText(messages: readonly unknown[] | undefined): string | undefined {
  for (let i = (messages?.length ?? 0) - 1; i >= 0; i--) {
    const message = messages?.[i] as { role?: string; stopReason?: string; content?: unknown };
    if (message?.role !== "assistant") continue;
    if (message.stopReason && message.stopReason !== "stop") return undefined;
    const content = message.content;
    const text =
      typeof content === "string"
        ? content
        : Array.isArray(content)
          ? content
              .filter((part) => part?.type === "text" && typeof part.text === "string")
              .map((part) => part.text as string)
              .join("\n")
          : "";
    return text.trim() || undefined;
  }
  return undefined;
}

/** Ask pi-background-tasks for its tasks over the event bus; [] when it is absent. */
export function fetchTasks(bus: Bus, timeoutMs = STATUS_TIMEOUT_MS): Promise<Task[]> {
  return new Promise((resolve) => {
    const requestId = `attention-bridge:${randomUUID()}`;
    let off: (() => void) | void;
    const finish = (tasks: Task[]) => {
      clearTimeout(timer);
      if (typeof off === "function") off();
      resolve(tasks);
    };
    const timer = setTimeout(() => finish([]), timeoutMs);
    timer.unref?.();
    off = bus.on(RESPONSE, (frame) => {
      const response = frame as { request_id?: string; ok?: boolean; result?: { tasks?: Task[] } };
      if (response?.request_id !== requestId) return;
      finish(response.ok && Array.isArray(response.result?.tasks) ? response.result.tasks : []);
    });
    try {
      bus.emit(REQUEST, { schema_version: REQUEST_SCHEMA, request_id: requestId, operation: "status", payload: {} });
    } catch {
      finish([]);
    }
  });
}

/** The plugin checkout: written in by the installer, else this file's repo. */
export function pluginRoot(): string | undefined {
  if (!PLUGIN_ROOT.startsWith("__")) return PLUGIN_ROOT;
  try {
    return path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  } catch {
    return undefined;
  }
}

/** Run the plugin's Jev judge on a turn's final message; false on any failure. */
export function askCheck(text: string, root = pluginRoot()): Promise<boolean> {
  if (!root) return Promise.resolve(false);
  return new Promise((resolve) => {
    let out = "";
    let done = false;
    const finish = (asks: boolean) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(asks);
    };
    const child = spawn("python3", ["-B", path.join(root, "attention.py"), "ask-check"], {
      stdio: ["pipe", "pipe", "ignore"],
    });
    const timer = setTimeout(() => {
      child.kill();
      finish(false);
    }, ASK_TIMEOUT_MS);
    timer.unref?.();
    child.on("error", () => finish(false));
    child.stdout?.on("data", (chunk) => {
      out += String(chunk);
    });
    child.on("close", () => {
      try {
        finish(JSON.parse(out).asks === true);
      } catch {
        finish(false);
      }
    });
    child.stdin?.on("error", () => undefined);
    child.stdin?.end(JSON.stringify({ message: text }));
  });
}

export interface BridgeDeps {
  bus: Bus;
  report(report: Report): Promise<void> | void;
  /** Ask herdr-attention-queue to pick up a changed token now. */
  refresh?(): Promise<void> | void;
  fetch?: (bus: Bus) => Promise<Task[]>;
  /** Does this finished turn's final message ask the user something? */
  ask?: (text: string) => Promise<boolean>;
  /** Run fn after the grace period. */
  defer?: (fn: () => void) => void;
  setInterval?: typeof setInterval;
  clearInterval?: typeof clearInterval;
}

const OWN = "attention-bridge";

function isActive(data: unknown): boolean {
  return Boolean((data as { active?: unknown })?.active);
}

/** The bridge's state machine, separate from pi for tests. */
export function createBridge(deps: BridgeDeps) {
  const fetch = deps.fetch ?? fetchTasks;
  const ask = deps.ask ?? askCheck;
  const defer =
    deps.defer ??
    ((fn: () => void) => {
      const timer = setTimeout(fn, GRACE_MS);
      (timer as { unref?: () => void }).unref?.();
    });
  const every = deps.setInterval ?? setInterval;
  const stopEvery = deps.clearInterval ?? clearInterval;
  let active = false;
  let busy = false;
  let holds = 0;
  let bg = 0;
  // The open dialog (pi reports only the outermost one), and whether it was
  // forwarded as herdr:blocked.
  let prompt: string | undefined;
  let forwarded = false;
  let deferred = false;
  // herdr:working holds, and other extensions' herdr:blocked holds.
  let working = 0;
  let external = 0;
  // The last finished turn asked the user something; `turn` invalidates
  // judgments that arrive after a new turn started.
  let asking = false;
  let turn = 0;
  let reported: Report = { bg: 0 };
  let refresh: ReturnType<typeof setInterval> | undefined;
  let chain: Promise<void> = Promise.resolve();

  const send = (report: Report, changed: boolean) => {
    // Serialize reports; each carries the state at the time it was made.
    chain = chain
      .then(async () => {
        await deps.report(report);
        if (changed) await deps.refresh?.();
      })
      .catch(() => undefined);
    return chain;
  };

  const desired = (): Report => {
    let activity: Activity | undefined =
      forwarded || external > 0 || asking ? "blocked" : working > 0 ? "working" : undefined;
    // While a dialog waits out its grace period, hold the last activity.
    if (activity === undefined && deferred && prompt !== undefined) activity = reported.activity;
    return activity ? { bg, activity } : { bg };
  };

  const syncTimer = () => {
    const holding = reported.bg > 0 || reported.activity !== undefined;
    if (holding && !refresh) {
      refresh = every(() => void send(reported, false), REFRESH_MS);
      (refresh as { unref?: () => void }).unref?.();
    } else if (!holding && refresh) {
      stopEvery(refresh);
      refresh = undefined;
    }
  };

  const publish = () => {
    if (!active) return chain;
    const next = desired();
    if (next.bg === reported.bg && next.activity === reported.activity) return chain;
    reported = next;
    syncTimer();
    return send(next, true);
  };

  const emitBlocked = (data: Record<string, unknown>) => {
    try {
      deps.bus.emit(BLOCKED, { ...data, source: OWN });
    } catch {
      // A failing listener must not break the bridge.
    }
  };

  const shouldForward = () => active && prompt !== undefined && working === 0;

  /** Forward the open dialog as blocked, after the grace period, unless an extension is working. */
  const sync = () => {
    if (!shouldForward() && forwarded) {
      forwarded = false;
      emitBlocked({ active: false });
    } else if (shouldForward() && !forwarded && !deferred) {
      deferred = true;
      defer(() => {
        deferred = false;
        if (!shouldForward() || forwarded) return;
        forwarded = true;
        emitBlocked({ active: true, label: prompt });
        void publish();
      });
    }
    return publish();
  };

  const recount = async () => {
    if (!active) return;
    const count = bgCount(busy, holds, await fetch(deps.bus));
    if (!active) return;
    bg = count;
    await publish();
  };

  return {
    get count() {
      return reported.bg;
    },
    get activity() {
      return reported.activity;
    },
    start() {
      active = true;
      return recount();
    },
    recount,
    onBusy(data: unknown) {
      busy = isActive(data);
      return recount();
    },
    onBackground(data: unknown) {
      if (isActive(data)) holds += 1;
      else holds = Math.max(0, holds - 1);
      return recount();
    },
    onWorking(data: unknown) {
      if (isActive(data)) working += 1;
      else working = Math.max(0, working - 1);
      return sync();
    },
    /** Another extension's herdr:blocked hold; the bridge's own are tagged and skipped. */
    onBlocked(data: unknown) {
      if ((data as { source?: unknown })?.source === OWN) return chain;
      if (isActive(data)) external += 1;
      else external = Math.max(0, external - 1);
      return publish();
    },
    promptStart(label: string) {
      if (!active) return chain;
      prompt = label;
      return sync();
    },
    promptEnd() {
      if (!active || prompt === undefined) return chain;
      prompt = undefined;
      return sync();
    },
    /** A new turn started (a prompt or a wake-up): any earlier question is answered. */
    turnStart() {
      turn += 1;
      if (!asking) return chain;
      asking = false;
      return publish();
    },
    /** The turn ended and pi is idle: judge whether its final message asks the user something. */
    async turnEnd(text: string | undefined) {
      if (!active || !text) return;
      const mine = turn;
      const asks = await ask(text);
      if (!active || turn !== mine || !asks) return;
      asking = true;
      await publish();
    },
    async stop() {
      if (!active) return;
      prompt = undefined;
      working = 0;
      external = 0;
      holds = 0;
      asking = false;
      bg = 0;
      sync();
      await publish();
      active = false;
      if (refresh) stopEvery(refresh);
      refresh = undefined;
      await chain;
    },
  };
}

export default function herdrAttentionBridge(pi: ExtensionAPI) {
  const pane = process.env.HERDR_PANE_ID;
  const bin = process.env.HERDR_BIN_PATH;
  if (process.env.HERDR_ENV !== "1" || !pane || !bin) return;
  const registry = globalThis as Record<symbol, unknown>;
  const me = Symbol("bridge");
  registry[LOADED] = me;
  const current = () => registry[LOADED] === me;

  const run = (args: string[]) =>
    new Promise<void>((resolve) => {
      execFile(bin, args, { timeout: 5_000 }, () => resolve());
    });
  const bus = pi.events as unknown as Bus;
  const bridge = createBridge({
    bus,
    report: (report) => (current() ? run(reportArgs(pane, report, Date.now())) : undefined),
    // Fails quietly when herdr-attention-queue is not installed.
    refresh: () => (current() ? run(["plugin", "action", "invoke", REFRESH_ACTION]) : undefined),
  });
  let lastText: string | undefined;
  const on = (channel: string, handler: (data: unknown) => unknown) =>
    pi.events.on(channel, (data) => {
      if (current()) void handler(data);
    });

  on(SUBAGENTS_BUSY, (data) => bridge.onBusy(data));
  on(BACKGROUND, (data) => bridge.onBackground(data));
  on(WORKING, (data) => bridge.onWorking(data));
  on(BLOCKED, (data) => bridge.onBlocked(data));
  on(TERMINAL, () => bridge.recount());
  pi.on("session_start", (_event, ctx) => {
    // TUI only: RPC and print modes (subagents, planners) have no pane of their own.
    if (!current() || ctx?.mode !== "tui") return;
    void bridge.start();
  });
  pi.on("agent_start", () => {
    lastText = undefined;
    void bridge.turnStart();
  });
  pi.on("agent_end", (event) => {
    lastText = finalText(event?.messages);
  });
  pi.on("agent_settled", (_event, ctx) => {
    void bridge.recount();
    if (ctx?.isIdle?.() !== true) return;
    const text = lastText;
    lastText = undefined;
    void bridge.turnEnd(text);
  });
  pi.on("tool_execution_end", () => void bridge.recount());
  pi.on("ui_prompt_start", (event) => void bridge.promptStart(event.title ?? event.kind));
  pi.on("ui_prompt_end", () => void bridge.promptEnd());
  pi.on("session_shutdown", () => bridge.stop());
}
