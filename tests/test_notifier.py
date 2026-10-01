import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest

from attention_queue import notifier
from attention_queue.notifier import LOCAL, Machine

NOW = 1_800_000_000_000
LOCAL_M = Machine(LOCAL, "Local")
NUC = Machine("nuc-id", "NUC", "nuc", "homelab")
DEV = Machine("dev-id", "spencer-dev", "spencer-dev.exe.xyz")


def attn(pane="w1:p1", state="done", prev="working", ts=NOW - 1000, **extra):
    line = {
        "v": 1,
        "kind": "attn",
        "ts_ms": ts,
        "pane_id": pane,
        "workspace_id": pane.split(":")[0],
        "tab_id": pane.split(":")[0] + ":t1",
        "agent": "claude",
        "label": None,
        "workspace": "data-pipeline",
        "attn": state,
        "prev": prev,
        "restoring": False,
    }
    line.update(extra)
    return line


ATTN_OF_RANK = {"0": "blocked", "1": "done", "2": "working", "3": "waiting", "4": "idle", "5": "unknown"}


def agent(pane, rank, ts, focused=False):
    tokens = {"attn_rank": rank, "attn_ts": ts, "attn": ATTN_OF_RANK.get(rank, "idle")}
    return {"pane_id": pane, "focused": focused, "tokens": tokens}


class FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.result = threading.Event()
        self.output = ""
        self.terminated = False

    def communicate(self):
        self.result.wait(5)
        return self.output, ""

    def click(self, output='{"activationType": "contentsClicked"}'):
        self.output = output
        self.result.set()

    def terminate(self):
        self.terminated = True
        self.result.set()


class FakeIO:
    alerter = "alerter"

    def __init__(self):
        self.sent = []
        self.removed = []
        self.front = "herdr mac: data-pipeline"
        self.selected_key = LOCAL
        self.focused = {LOCAL: "w9:p9"}
        self.lists = {}
        self.calls = []
        self.switch_on_jump = None

    def now_ms(self):
        return NOW

    def notify(self, argv):
        proc = FakeProc(argv)
        self.sent.append(proc)
        return proc

    def remove(self, group):
        self.removed.append(group)

    def front_title(self):
        return self.front

    def selected(self):
        return self.selected_key

    def focused_pane(self, machine):
        return self.focused.get(machine.key)

    def agents(self, machine):
        return self.lists.get(machine.key)

    def focus_terminal(self):
        self.calls.append(("focus_terminal",))
        return "focused"

    def focus_agent(self, machine, pane):
        self.calls.append(("focus_agent", machine.key, pane))
        return True

    def notice(self, machine, text):
        self.calls.append(("notice", machine.key, text))

    def jump(self, n):
        self.calls.append(("jump", n))
        if self.switch_on_jump:
            self.selected_key = self.switch_on_jump
        return True


class DecideTest(unittest.TestCase):
    def test_notify_on_entering_blocked_or_done(self):
        self.assertEqual(notifier.decide(attn(state="done"), NOW), "notify")
        self.assertEqual(notifier.decide(attn(state="blocked", prev="working"), NOW), "notify")

    def test_other_states_and_focus_remove(self):
        for state in ("working", "waiting", "idle", "unknown"):
            self.assertEqual(notifier.decide(attn(state=state, prev="done"), NOW), "remove")
        self.assertEqual(notifier.decide({"kind": "focus", "pane_id": "w1:p1", "ts_ms": NOW}, NOW), "remove")

    def test_ignored_lines(self):
        self.assertIsNone(notifier.decide(attn(prev=None), NOW))
        self.assertIsNone(notifier.decide(attn(restoring=True), NOW))
        self.assertIsNone(notifier.decide(attn(ts=NOW - 16 * 60 * 1000), NOW))
        self.assertIsNone(notifier.decide(attn(prev="done"), NOW))
        self.assertIsNone(notifier.decide({"kind": "ping", "ts_ms": NOW}, NOW))


class ContentTest(unittest.TestCase):
    def test_titles_subtitles_and_groups(self):
        text = notifier.content(attn(state="blocked"), NUC)
        self.assertEqual(text["title"], "data-pipeline \u00b7 claude needs input")
        self.assertEqual(text["subtitle"], "NUC")
        text = notifier.content(attn(state="done", label="cache layer"), LOCAL_M)
        self.assertEqual(text["title"], "data-pipeline \u00b7 claude finished")
        self.assertIsNone(text["subtitle"])
        self.assertEqual(text["message"], "cache layer")
        self.assertEqual(notifier.group_for("nuc-id", "w1:p2"), "herdr-nuc-id-w1:p2")

    def test_alerter_argv(self):
        argv = notifier.alerter_argv("alerter", notifier.content(attn(), NUC), "g", "/i.png")
        self.assertEqual(argv[0], "alerter")
        for flag, value in (("--group", "g"), ("--subtitle", "NUC"), ("--app-icon", "/i.png"), ("--actions", "Focus")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--json", argv)
        local = notifier.alerter_argv("alerter", notifier.content(attn(), LOCAL_M), "g", None)
        self.assertNotIn("--subtitle", local)
        self.assertNotIn("--app-icon", local)

    def test_icons_exist_for_common_agents(self):
        for name in ("claude", "pi", "codex"):
            self.assertTrue(os.path.exists(notifier.icon_for(name)), name)
        self.assertIsNone(notifier.icon_for("mystery"))
        self.assertIsNone(notifier.icon_for(None))

    def test_clicked(self):
        self.assertTrue(notifier.clicked('{"activationType": "contentsClicked"}'))
        self.assertTrue(notifier.clicked('{"activationType": "actionClicked", "activationValue": "Focus"}'))
        self.assertFalse(notifier.clicked('{"activationType": "closed", "activationValue": "Dismiss"}'))
        self.assertFalse(notifier.clicked('{"activationType": "timeout"}'))
        self.assertFalse(notifier.clicked("garbage"))

    def test_ssh_argv(self):
        argv = notifier.ssh_argv("nuc", "~/dotfiles/plugins/herdr-attention-queue/attention.py", "homelab", 42)
        self.assertEqual(argv[0], "ssh")
        for option in ("BatchMode=yes", "ClearAllForwardings=yes", "ServerAliveInterval=15"):
            self.assertIn(option, argv)
        self.assertEqual(argv[-2], "nuc")
        self.assertEqual(
            argv[-1],
            "python3 -B ~/dotfiles/plugins/herdr-attention-queue/attention.py follow --session homelab --since-ms 42",
        )

    def test_scripts_quote_their_strings(self):
        self.assertEqual(notifier.applescript_string('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(notifier.jump_sequences(3), ["98;5u", "51;3u"])
        self.assertEqual(notifier.jump_sequences(9, "32;5u"), ["32;5u", "57;3u"])
        script = notifier.jump_script(3)
        self.assertIn('perform action "csi:98;5u" on s', script)
        self.assertIn('perform action "csi:51;3u" on s', script)
        self.assertIn('starts with "herdr "', notifier.focus_script())


class JumpIndexTest(unittest.TestCase):
    def test_interleaves_machines_by_rank_then_time(self):
        lists = [
            (LOCAL, [agent("w1:p1", "4", "0000000000005"), agent("w1:p2", "1", "0000000000009")]),
            ("nuc-id", [agent("w1:p1", "1", "0000000000003")]),
            ("dev-id", [agent("w2:p1", "0", "0000000000010")]),
        ]
        self.assertEqual(notifier.jump_index(lists, "dev-id", "w2:p1"), 1)
        self.assertEqual(notifier.jump_index(lists, "nuc-id", "w1:p1"), 2)
        self.assertEqual(notifier.jump_index(lists, LOCAL, "w1:p2"), 3)
        self.assertEqual(notifier.jump_index(lists, LOCAL, "w1:p1"), 4)

    def test_unreachable_machine_is_skipped(self):
        lists = [
            (LOCAL, [agent("w1:p1", "1", "0000000000005")]),
            ("nuc-id", None),
            ("dev-id", [agent("w1:p1", "2", "0000000000001")]),
        ]
        self.assertEqual(notifier.jump_index(lists, "dev-id", "w1:p1"), 2)
        self.assertIsNone(notifier.jump_index(lists, "nuc-id", "w1:p1"))

    def test_missing_tokens_sort_last_and_ties_keep_machine_order(self):
        lists = [
            (LOCAL, [{"pane_id": "a", "tokens": {}}, agent("b", "2", "0000000000001")]),
            ("nuc-id", [agent("c", "2", "0000000000001")]),
        ]
        self.assertEqual(notifier.jump_index(lists, LOCAL, "b"), 1)
        self.assertEqual(notifier.jump_index(lists, "nuc-id", "c"), 2)
        self.assertEqual(notifier.jump_index(lists, LOCAL, "a"), 3)


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.io = FakeIO()
        self.n = notifier.Notifier(self.io, [LOCAL_M, NUC, DEV])

    def wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.01)
        return predicate()

    def test_notifies_and_withdraws_when_the_agent_moves_on(self):
        self.assertEqual(self.n.handle("nuc-id", attn(state="blocked")), "notify")
        self.assertEqual(len(self.io.sent), 1)
        group = "herdr-nuc-id-w1:p1"
        self.assertEqual(self.io.sent[0].argv[self.io.sent[0].argv.index("--group") + 1], group)
        self.n.handle("nuc-id", attn(state="working", prev="blocked"))
        self.assertTrue(self.io.sent[0].terminated)
        self.assertEqual(self.io.removed, [group])

    def test_focus_line_withdraws(self):
        self.n.handle(LOCAL, attn())
        self.n.handle(LOCAL, {"v": 1, "kind": "focus", "pane_id": "w1:p1", "ts_ms": NOW})
        self.assertEqual(self.io.removed, ["herdr-local-w1:p1"])

    def test_no_removal_for_notifications_never_sent(self):
        self.n.handle(LOCAL, attn(state="working", prev="idle"))
        self.assertEqual(self.io.removed, [])

    def test_skips_only_when_every_check_passes(self):
        self.io.focused[LOCAL] = "w1:p1"
        self.assertEqual(self.n.handle(LOCAL, attn()), "skipped")
        for change in (
            lambda io: setattr(io, "front", None),  # Ghostty not frontmost
            lambda io: setattr(io, "front", "vim notes.md"),  # another terminal
            lambda io: setattr(io, "selected_key", "nuc-id"),  # another machine
            lambda io: io.focused.__setitem__(LOCAL, "w1:p2"),  # another pane
        ):
            with self.subTest(change=change):
                self.setUp()
                self.io.focused[LOCAL] = "w1:p1"
                change(self.io)
                self.assertEqual(self.n.handle(LOCAL, attn()), "notify")

    def test_remote_skip_uses_focus_lines(self):
        self.io.selected_key = "nuc-id"
        self.assertEqual(self.n.handle("nuc-id", attn()), "notify")
        self.n.handle("nuc-id", {"v": 1, "kind": "focus", "pane_id": "w1:p1", "ts_ms": NOW})
        self.assertEqual(self.n.handle("nuc-id", attn(prev="working")), "skipped")

    def test_unknown_machine_is_ignored(self):
        self.assertIsNone(self.n.handle("gone", attn()))
        self.assertEqual(self.io.sent, [])

    def test_click_on_the_selected_machine_focuses_without_a_key_jump(self):
        self.n.handle(LOCAL, attn())
        self.io.sent[0].click()
        self.assertTrue(self.wait(lambda: ("focus_agent", LOCAL, "w1:p1") in self.io.calls))
        self.assertEqual(self.io.calls[0], ("focus_terminal",))
        self.assertFalse(any(c[0] == "jump" for c in self.io.calls))

    def test_click_on_another_machine_jumps_to_its_position(self):
        self.io.lists = {
            LOCAL: [agent("w1:p1", "4", "0000000000001")],
            "nuc-id": [agent("w3:p2", "1", "0000000000002")],
            "dev-id": None,
        }
        self.io.switch_on_jump = "nuc-id"
        self.n.handle("nuc-id", attn(pane="w3:p2"))
        self.io.sent[0].click('{"activationType": "actionClicked", "activationValue": "Focus"}')
        self.assertTrue(self.wait(lambda: ("jump", 1) in self.io.calls))
        self.assertIn(("focus_agent", "nuc-id", "w3:p2"), self.io.calls)
        self.assertEqual(sum(1 for c in self.io.calls if c[0] == "jump"), 1)

    def test_unconfirmed_switch_retries_once(self):
        self.io.lists = {"nuc-id": [agent("w3:p2", "1", "0000000000002")]}
        self.n._wait_selected = lambda key, timeout=2.0: False
        self.n.click(NUC, "w3:p2")
        self.assertEqual([c for c in self.io.calls if c[0] == "jump"], [("jump", 1), ("jump", 1)])

    def test_no_jump_past_nine(self):
        self.io.lists = {"nuc-id": [agent("w1:p%d" % i, "1", "%013d" % i) for i in range(12)]}
        self.n.click(NUC, "w1:p10")
        self.assertFalse(any(c[0] == "jump" for c in self.io.calls))

    def test_dismiss_does_nothing(self):
        self.n.handle(LOCAL, attn())
        self.io.sent[0].click('{"activationType": "closed", "activationValue": "Dismiss"}')
        time.sleep(0.1)
        self.assertEqual(self.io.calls, [])

    def test_replacing_a_notification_ignores_the_old_click(self):
        self.n.handle(LOCAL, attn(state="blocked"))
        self.n.handle(LOCAL, attn(state="done", prev="blocked"))
        self.assertTrue(self.io.sent[0].terminated)
        self.assertEqual(len(self.io.sent), 2)
        time.sleep(0.1)
        self.assertEqual(self.io.calls, [])


class FakeChannel:
    """A machine's follow pipe: answers list commands like `follow` would."""

    def __init__(self, notifier_, key, agents, delay=0.0):
        self.notifier, self.key, self.agents, self.delay = notifier_, key, agents, delay
        self.sent = []

    def send(self, command):
        self.sent.append(command)
        if command.get("op") == "list" and self.agents is not None:
            def reply():
                time.sleep(self.delay)
                self.notifier.handle(self.key, {"kind": "list", "req": command["req"], "agents": self.agents})
            threading.Thread(target=reply, daemon=True).start()
        return True


class JumpRequestTest(unittest.TestCase):
    """jump-attention hands the jump to the notifier, which sees every machine."""

    def setUp(self):
        self.io = FakeIO()
        self.n = notifier.Notifier(self.io, [LOCAL_M, NUC, DEV])
        # Panel order: wH:p2 (blocked), w1:p1 (done 5), w1:p3 (done 7), then the working rows.
        self.io.lists = {
            LOCAL: [agent("w1:p1", "1", "0000000000005"), agent("w1:p2", "2", "0000000000001"),
                    agent("w1:p3", "1", "0000000000007")],
            "nuc-id": [agent("wH:p1", "2", "0000000000001"), agent("wH:p2", "0", "0000000000009")],
            "dev-id": None,
        }

    def jump(self, origin="nuc-id", pane="wH:p1", ts=NOW - 500):
        self.io.calls = []
        line = {"v": 1, "kind": "jump", "ts_ms": ts, "pane_id": pane}
        self.assertEqual(self.n.handle(origin, line), "jump")
        time.sleep(0.05)
        with self.n.click_lock:
            return list(self.io.calls)

    def test_most_urgent_first_on_the_same_machine_is_a_server_focus(self):
        self.assertEqual(self.jump(), [("focus_agent", "nuc-id", "wH:p2")])

    def test_another_machine_is_reached_with_the_focus_agent_key_only(self):
        self.io.lists["nuc-id"][1] = agent("wH:p2", "2", "0000000000009")
        self.assertEqual(self.jump(), [("jump", 1)])

    def test_ties_cycle_in_panel_order_and_wrap(self):
        self.io.lists["nuc-id"][1] = agent("wH:p2", "1", "0000000000006")
        # Done tier in panel order: w1:p1 (5), wH:p2 (6), w1:p3 (7).
        self.assertEqual(self.jump(origin=LOCAL, pane="w1:p2"), [("focus_agent", LOCAL, "w1:p1")])
        self.assertEqual(self.jump(origin=LOCAL, pane="w1:p1"), [("jump", 2)])
        self.assertEqual(self.jump(origin="nuc-id", pane="wH:p2"), [("jump", 3)])
        self.assertEqual(self.jump(origin=LOCAL, pane="w1:p3"), [("focus_agent", LOCAL, "w1:p1")])

    def test_a_lone_urgent_agent_stays(self):
        self.assertEqual(self.jump(pane="wH:p2"), [])

    def test_nothing_needs_attention(self):
        self.io.lists = {LOCAL: [agent("w1:p2", "2", "0000000000001")]}
        self.assertEqual(self.jump(), [("notice", "nuc-id", "No agents need attention")])

    def test_stale_requests_and_unknown_machines_are_ignored(self):
        self.assertIsNone(self.n.handle("nuc-id", {"kind": "jump", "ts_ms": NOW - 60_000, "pane_id": "x"}))
        self.assertIsNone(self.n.handle("nope", {"kind": "jump", "ts_ms": NOW, "pane_id": "x"}))
        time.sleep(0.05)
        self.assertEqual(self.io.calls, [])

    def test_channels_answer_lists_and_carry_focus_without_new_processes(self):
        lists = self.io.lists
        self.io.lists = {}  # direct fetches would see nothing
        self.n.channels = {
            LOCAL: FakeChannel(self.n, LOCAL, lists[LOCAL]),
            "nuc-id": FakeChannel(self.n, "nuc-id", lists["nuc-id"], delay=0.02),
            "dev-id": FakeChannel(self.n, "dev-id", None),  # connected but silent
        }
        started = time.monotonic()
        self.assertEqual(self.jump(), [])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.n.channels["nuc-id"].sent[-1], {"op": "focus", "pane_id": "wH:p2"})

    def test_panel_rows(self):
        lists = [("a", [agent("p1", "2", "1")]), ("b", None), ("c", [agent("p2", "1", "2"), agent("p3", "1", "1")])]
        self.assertEqual(notifier.panel_rows(lists), [("c", "p3", "done"), ("c", "p2", "done"), ("a", "p1", "working")])


class MachineListTest(unittest.TestCase):
    def test_local_first_then_enabled_machines(self):
        io = FakeIO()
        io.machines = lambda: [
            {"id": "a", "label": "NUC", "target": "nuc", "session": "homelab", "enabled": True},
            {"id": "b", "label": "off", "target": "x", "session": "default", "enabled": False},
        ]
        machines = notifier.machine_list(io, "default")
        self.assertEqual(machines, [Machine(LOCAL, "Local", None, "default"), Machine("a", "NUC", "nuc", "homelab")])
        io.machines = lambda: None
        self.assertIsNone(notifier.machine_list(io, "default"))


class SourceAndProgressTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aq-notifier-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_progress_round_trip(self):
        path = os.path.join(self.dir, "progress.json")
        p = notifier.Progress(path)
        p.advance("nuc-id", 5)
        p.advance("nuc-id", 3)
        p.flush(force=True)
        self.assertEqual(notifier.Progress(path).get("nuc-id"), 5)

    def test_source_reads_lines_skips_pings_and_reconnects_from_progress(self):
        script = os.path.join(self.dir, "follow.py")
        with open(script, "w") as f:
            f.write(
                "import json, sys\n"
                "since = int(sys.argv[1])\n"
                "print(json.dumps({'kind': 'ping', 'ts_ms': 1}))\n"
                "print(json.dumps({'kind': 'attn', 'ts_ms': since + 1}))\n"
            )
        seen_since = []

        def argv_for(machine, since):
            seen_since.append(since)
            return [sys.executable, script, str(since)]

        lines = queue.Queue()
        progress = notifier.Progress(os.path.join(self.dir, "progress.json"))
        progress.advance("nuc-id", NOW)
        source = notifier.Source(NUC, argv_for, lines, progress, lambda m: None)
        old = notifier.BACKOFF_MIN_S
        notifier.BACKOFF_MIN_S = 0.01
        try:
            source.start()
            key, line = lines.get(timeout=5)
            self.assertEqual((key, line["kind"], line["ts_ms"]), ("nuc-id", "attn", NOW + 1))
            lines.get(timeout=5)  # it reconnected after the script exited
        finally:
            source.stop()
            source.join(5)
            notifier.BACKOFF_MIN_S = old
        self.assertGreaterEqual(len(seen_since), 2)
        self.assertEqual(seen_since[0], NOW)


if __name__ == "__main__":
    unittest.main()
