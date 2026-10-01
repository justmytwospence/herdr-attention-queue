import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

const root = process.env.PI_PACKAGE_DIR;
assert.ok(root, "Set PI_PACKAGE_DIR to the installed @earendil-works/pi-coding-agent directory");
const entry = fileURLToPath(new URL("../herdr-attention-bridge.ts", import.meta.url));
const { createJiti } = await import(pathToFileURL(path.join(root, "dist/core/extensions/jiti-loader.js")));
const jiti = createJiti(import.meta.url, { moduleCache: false });
const mod = await jiti.import(entry);

function bus() {
  const handlers = new Map();
  const emitted = [];
  return {
    emitted,
    emit(channel, data) {
      emitted.push([channel, data]);
      for (const handler of handlers.get(channel) ?? []) handler(data);
    },
    on(channel, handler) {
      handlers.set(channel, [...(handlers.get(channel) ?? []), handler]);
      return () => handlers.set(channel, (handlers.get(channel) ?? []).filter((h) => h !== handler));
    },
  };
}

function bridge(tasks = []) {
  const b = bus();
  const reports = [];
  const refreshes = [];
  const timers = [];
  const deferred = [];
  const asked = [];
  const state = { tasks, asks: false };
  const bridge = mod.createBridge({
    bus: b,
    report: (report) => { reports.push(report); },
    refresh: () => { refreshes.push(reports.length); },
    fetch: async () => state.tasks,
    ask: async (text) => { asked.push(text); return state.asks; },
    defer: (fn) => { deferred.push(fn); },
    setInterval: (fn) => { timers.push(fn); return timers.length; },
    clearInterval: (id) => { timers[id - 1] = undefined; },
  });
  const flush = async () => {
    while (deferred.length) deferred.shift()();
    await new Promise((resolve) => setImmediate(resolve));
  };
  return { b, bridge, reports, refreshes, timers, state, flush, asked };
}

const blocked = (b) => b.emitted.filter(([channel]) => channel === "herdr:blocked").map(([, data]) => data);

test("counts subagents once plus waking background tasks", () => {
  const tasks = [
    { status: "running", triggerOnCompletion: true },
    { status: "running", triggerOnCompletion: false },
    { status: "completed", triggerOnCompletion: true },
    { status: "running", triggerOnCompletion: true },
  ];
  assert.equal(mod.wakingTasks(tasks), 2);
  assert.equal(mod.bgCount(true, 0, tasks), 3);
  assert.equal(mod.bgCount(false, 2, tasks), 4);
  assert.equal(mod.bgCount(false, 0, []), 0);
});

test("report arguments set tokens with a TTL and clear the rest", () => {
  const base = ["pane", "report-metadata", "w1:p2", "--source", "user:attention-bridge", "--seq", "7"];
  assert.deepEqual(mod.reportArgs("w1:p2", { bg: 2 }, 7), [
    ...base, "--token", "bg=2", "--clear-token", "activity", "--ttl-ms", "90000",
  ]);
  assert.deepEqual(mod.reportArgs("w1:p2", { bg: 0, activity: "working" }, 7), [
    ...base, "--clear-token", "bg", "--token", "activity=working", "--ttl-ms", "90000",
  ]);
  assert.deepEqual(mod.reportArgs("w1:p2", { bg: 0 }, 7), [...base, "--clear-token", "bg", "--clear-token", "activity"]);
});

test("reports only changes, refreshes while set, and clears on stop", async () => {
  const { bridge: br, reports, refreshes, timers, state } = bridge([]);
  await br.start();
  assert.deepEqual(reports, []);
  await br.onBusy({ active: true, label: "2 subagents" });
  state.tasks = [{ status: "running", triggerOnCompletion: true }];
  await br.recount();
  await br.recount();
  assert.deepEqual(reports, [{ bg: 1 }, { bg: 2 }]);
  assert.deepEqual(refreshes, [1, 2]);
  assert.equal(timers.filter(Boolean).length, 1);
  timers[0]();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(reports.at(-1), { bg: 2 });
  assert.deepEqual(refreshes, [1, 2], "a TTL refresh is not a change");
  await br.onBusy({ active: false });
  state.tasks = [];
  await br.recount();
  assert.deepEqual(reports.slice(-2), [{ bg: 1 }, { bg: 0 }]);
  assert.equal(timers.filter(Boolean).length, 0);
  state.tasks = [{ status: "running", triggerOnCompletion: true }];
  await br.recount();
  await br.stop();
  assert.deepEqual(reports.at(-1), { bg: 0 });
});

test("nothing is reported before a TUI session starts", async () => {
  const { bridge: br, reports, b, flush } = bridge([{ status: "running", triggerOnCompletion: true }]);
  await br.recount();
  br.promptStart("Question");
  await br.onWorking({ active: true });
  await flush();
  assert.deepEqual(reports, []);
  assert.deepEqual(b.emitted, []);
});

test("a question is blocked: herdr:blocked pair and activity token", async () => {
  const { bridge: br, b, reports, refreshes, flush } = bridge();
  await br.start();
  br.promptStart("Pick one");
  assert.deepEqual(blocked(b), [], "forwarded only after the grace period");
  await flush();
  assert.deepEqual(blocked(b), [{ active: true, label: "Pick one", source: "attention-bridge" }]);
  assert.equal(br.activity, "blocked");
  await br.promptEnd();
  await br.promptEnd(); // unmatched end is ignored
  assert.deepEqual(blocked(b).at(-1), { active: false, source: "attention-bridge" });
  assert.equal(br.activity, undefined);
  assert.deepEqual(reports, [{ bg: 0, activity: "blocked" }, { bg: 0 }]);
  assert.deepEqual(refreshes, [1, 2]);
  br.promptStart("custom");
  await flush();
  await br.stop();
  assert.deepEqual(blocked(b).slice(-2), [
    { active: true, label: "custom", source: "attention-bridge" },
    { active: false, source: "attention-bridge" },
  ]);
  assert.deepEqual(reports.at(-1), { bg: 0 });
});

test("a dialog that closes within the grace period is never forwarded", async () => {
  const { bridge: br, b, reports, flush } = bridge();
  await br.start();
  br.promptStart("quick");
  await br.promptEnd();
  await flush();
  assert.deepEqual(blocked(b), []);
  assert.deepEqual(reports, []);
});

test("a progress view during herdr:working is working, then blocked once the work ends", async () => {
  const { bridge: br, b, reports, flush } = bridge();
  await br.start();
  await br.onWorking({ active: true, label: "Planning" });
  br.promptStart("custom");
  await flush();
  assert.deepEqual(blocked(b), []);
  assert.equal(br.activity, "working");
  // The planners finished; the trace view now waits on the user.
  await br.onWorking({ active: false });
  await flush();
  assert.equal(br.activity, "blocked");
  await br.promptEnd();
  assert.equal(br.activity, undefined);
  assert.deepEqual(reports.map((r) => r.activity), ["working", "blocked", undefined]);
  assert.equal(blocked(b).length, 2);
});

test("a view that closes as its work ends never flashes blocked", async () => {
  const { bridge: br, b, reports, flush } = bridge();
  await br.start();
  br.promptStart("custom");
  // The working hold can start just after the view opened.
  await br.onWorking({ active: true, label: "Planning" });
  await flush();
  await br.onWorking({ active: false });
  await br.promptEnd();
  await flush();
  assert.deepEqual(blocked(b), []);
  assert.deepEqual(reports.map((r) => r.activity), ["working", undefined]);
});

test("a dialog already forwarded is withdrawn when work starts", async () => {
  const { bridge: br, b, flush } = bridge();
  await br.start();
  br.promptStart("custom");
  await flush();
  await br.onWorking({ active: true });
  assert.deepEqual(blocked(b).map((d) => d.active), [true, false]);
  assert.equal(br.activity, "working");
});

test("other extensions' herdr:blocked holds report blocked; the bridge's own are skipped", async () => {
  const { bridge: br, b, flush } = bridge();
  await br.start();
  b.on("herdr:blocked", (data) => void br.onBlocked(data));
  b.emit("herdr:blocked", { active: true, label: "Plan ready" });
  await flush();
  assert.equal(br.activity, "blocked");
  await br.onWorking({ active: true });
  assert.equal(br.activity, "blocked", "an explicit hold beats working");
  br.promptStart("q");
  await br.onWorking({ active: false });
  await flush();
  b.emit("herdr:blocked", { active: false });
  await flush();
  assert.equal(br.activity, "blocked", "the forwarded dialog still holds it");
  await br.promptEnd();
  assert.equal(br.activity, undefined);
});

test("any extension's herdr:background holds count as background work", async () => {
  const { bridge: br, reports } = bridge([{ status: "running", triggerOnCompletion: true }]);
  await br.start();
  await br.onBackground({ active: true, label: "indexing" });
  await br.onBackground({ active: true, label: "watching CI" });
  assert.equal(br.count, 3);
  await br.onBackground({ active: false });
  await br.onBackground({ active: false });
  await br.onBackground({ active: false }); // unmatched end is ignored
  assert.deepEqual(reports.map((r) => r.bg), [1, 2, 3, 2, 1]);
});

test("a turn that ends asking the user something is blocked until the next turn", async () => {
  const { bridge: br, reports, state, asked } = bridge();
  await br.start();
  await br.turnEnd("Done. Pushed.");
  assert.equal(br.activity, undefined);
  state.asks = true;
  await br.turnEnd("Want me to push?");
  assert.deepEqual(asked, ["Done. Pushed.", "Want me to push?"]);
  assert.equal(br.activity, "blocked");
  await br.turnStart();
  assert.equal(br.activity, undefined);
  assert.deepEqual(reports.map((r) => r.activity), ["blocked", undefined]);
  await br.turnEnd(undefined); // aborted or tool-ending turns are not judged
  assert.equal(asked.length, 2);
});

test("a judgment that lands after the next turn started is dropped", async () => {
  const { bridge: br, state } = bridge();
  await br.start();
  state.asks = true;
  const pending = br.turnEnd("Shall I?");
  await br.turnStart(); // the user already replied
  await pending;
  assert.equal(br.activity, undefined);
});

test("finalText takes the last assistant message of a normally ended turn", () => {
  const user = { role: "user", content: "hi" };
  const said = (text, stopReason = "stop") => ({ role: "assistant", stopReason, content: [{ type: "thinking", thinking: "x" }, { type: "text", text }] });
  assert.equal(mod.finalText([user, said("first"), said("Want me to push?")]), "Want me to push?");
  assert.equal(mod.finalText([user, said("partial", "aborted")]), undefined);
  assert.equal(mod.finalText([user, said("oops", "error")]), undefined);
  assert.equal(mod.finalText([user, { role: "assistant", content: "plain" }]), "plain");
  assert.equal(mod.finalText([user]), undefined);
  assert.equal(mod.finalText(undefined), undefined);
});

test("fetchTasks talks to pi-background-tasks over the event bus", async () => {
  const b = bus();
  b.on("pi-background-tasks:request:v1", (frame) => {
    assert.equal(frame.operation, "status");
    assert.equal(frame.schema_version, "pi-background-tasks.extension-request.v1");
    b.emit("pi-background-tasks:response:v1", { request_id: "someone-else", ok: true, result: { tasks: [{ id: "x" }] } });
    b.emit("pi-background-tasks:response:v1", {
      request_id: frame.request_id, ok: true, result: { tasks: [{ id: "t1", status: "running", triggerOnCompletion: true }] },
    });
  });
  assert.deepEqual(await mod.fetchTasks(b), [{ id: "t1", status: "running", triggerOnCompletion: true }]);
});

test("fetchTasks gives up quietly without pi-background-tasks", async () => {
  assert.deepEqual(await mod.fetchTasks(bus(), 20), []);
});

test("askCheck runs the plugin's judge: Jev's verdict, and false when it cannot ask", async () => {
  const http = await import("node:http");
  const os = await import("node:os");
  const fs = await import("node:fs");
  let p = 0.95;
  const server = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => res.end(JSON.stringify({ answers: { asks: { type: "noul", noul: p } } })));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const saved = { ...process.env };
  process.env.TYPESAFE_API_KEY = "k";
  process.env.HERDR_ATTENTION_QUEUE_JEV_URL = `http://127.0.0.1:${server.address().port}/v1/systemone`;
  process.env.HERDR_PLUGIN_CONFIG_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "aq-bridge-"));
  try {
    assert.equal(await mod.askCheck("Want me to push?"), true);
    p = 0.2;
    assert.equal(await mod.askCheck("Pushed."), false);
    assert.equal(await mod.askCheck("Go?", "/nonexistent/plugin"), false);
  } finally {
    process.env = saved;
    server.close();
  }
});
