import json
import os
import threading
import unittest
from unittest.mock import patch

from attention_queue import herdr, hooks, navigation, store
from attention_queue.model import Truth
from tests.test_hooks_e2e import HookTestCase


def truth(pane):
    return Truth(pane, pane, "pi", "session:" + pane, "idle", 1, {})


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.agents = [truth(p) for p in ("w1:pZ", "w1:p2", "w2:p1", "w3:p1")]
        self.live = {t.terminal_id: {"attn": "done", "attn_ns": 1999999} for t in self.agents}
        self.live["w1:p2"]["attn_ns"] = 1000001
        self.live["w2:p1"] = {"attn": "blocked", "attn_ns": 9000000}
        self.live["w3:p1"]["attn"] = "waiting"

    def test_priority_and_millisecond_layout_ties(self):
        self.assertEqual([t.pane_id for t in navigation.candidates(self.agents, self.live)],
                         ["w2:p1", "w1:pZ", "w1:p2"])
        self.assertEqual(navigation.select(self.agents, self.live).pane_id, "w2:p1")

    def test_selection_does_not_advance_or_consume_work(self):
        before = json.dumps(self.live, sort_keys=True)
        for _ in range(5):
            self.assertEqual(navigation.select(self.agents, self.live).pane_id, "w2:p1")
        self.assertEqual(json.dumps(self.live, sort_keys=True), before)
        self.live["w2:p1"]["attn"] = "working"  # Responding removes blocked.
        self.assertEqual(navigation.select(self.agents, self.live).pane_id, "w1:pZ")
        self.live["w1:pZ"]["attn"] = "idle"  # Explicit review removes done.
        self.assertEqual(navigation.select(self.agents, self.live).pane_id, "w1:p2")

    def test_oldest_first_empty_singleton_and_excluded_states(self):
        self.live["w1:pZ"]["attn_ns"] = 2000000
        self.assertEqual(navigation.candidates(self.agents, self.live)[1].pane_id, "w1:p2")
        for state in ("working", "waiting", "idle", "unknown"):
            for rec in self.live.values():
                rec["attn"] = state
            self.assertIsNone(navigation.select(self.agents, self.live))
        self.live["w1:pZ"]["attn"] = "done"
        self.assertEqual(navigation.select(self.agents, self.live).pane_id, "w1:pZ")

    def test_invocation_context_never_server_focus(self):
        with patch.dict(os.environ, {"HERDR_PANE_ID": "caller", "HERDR_PLUGIN_CONTEXT_JSON":
                                     '{"focused_pane_id":"context"}'}, clear=True):
            self.assertEqual(hooks.navigation_anchor(), "caller")
            del os.environ["HERDR_PANE_ID"]
            self.assertEqual(hooks.navigation_anchor(), "context")
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = '[]'
            self.assertIsNone(hooks.navigation_anchor())


class NavigationE2E(HookTestCase):
    def jump(self, pane=None, **extra):
        return self.run_hook("action", "jump-attention", pane=pane, **extra)

    def test_stays_at_head_even_with_other_work_until_explicit_review(self):
        for p in ("w2:p1", "w1:p1", "w3:p1"):
            self.fake.add_agent(p, "done", session=p)
        self.fake.agents["w2:p1"]["tab_id"] = "w2:t3"
        self.jump("w1:p1")
        self.assertEqual(self.fake.focus, ("w2", "w2:t3", "w2:p1"))
        self.assertEqual(self.fake.agents["w2:p1"]["agent_status"], "idle")
        calls = self.fake.count("agent.focus")
        self.jump("w2:p1")
        self.jump("w2:p1")
        self.assertEqual(self.fake.count("agent.focus"), calls)
        self.assertEqual(self.attn("w2:p1"), "done")
        self.run_hook("action", "mark-reviewed", pane="w2:p1")
        self.jump("w2:p1")
        self.assertEqual(self.fake.focus[-1], "w1:p1")
        self.assertEqual(self.attn("w2:p1"), "idle")
        self.assertEqual(self.fake.violations, [])
        self.assertEqual(self.fake.count("agent.view.set"), 0)

    def test_responding_to_blocked_advances_priority_not_focus_alone(self):
        self.fake.add_agent("w1:p1", "done")
        self.fake.add_agent("w2:p1", "blocked", session="blocked")
        self.jump("w1:p1")
        self.assertEqual(self.fake.focus[-1], "w2:p1")
        self.jump("w2:p1")
        self.assertEqual(self.fake.count("agent.focus"), 1)
        self.fake.set_status("w2:p1", "working")
        self.jump("w2:p1")
        self.assertEqual(self.fake.focus[-1], "w1:p1")

    def test_empty_singleton_context_and_waiting(self):
        self.assertIn("No agents need attention", self.jump().stdout)
        self.fake.add_agent("w1:p1", "done")
        self.fake.set_token("w1:p1", "bg", "1")
        self.jump()
        self.assertIsNone(self.fake.focus)
        self.fake.set_token("w1:p1", "bg", None)
        self.jump(HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"focused_pane_id": "w1:p1"}))
        self.assertIsNone(self.fake.focus)
        self.jump("shell")
        self.assertEqual(self.fake.focus[-1], "w1:p1")

    def test_retained_done_during_unknown_flap(self):
        self.fake.add_agent("w1:p1", "done")
        self.jump("w1:p1")
        self.fake.set_status("w1:p1", "unknown")
        self.jump("shell")
        self.assertEqual(self.fake.focus[-1], "w1:p1")

    def test_revalidates_new_higher_priority_arrival(self):
        self.fake.add_agent("w1:p1", "done")
        reads = [0]
        def arrival(params):
            reads[0] += 1
            if reads[0] == 2:
                self.fake.add_agent("w2:p1", "blocked", session="urgent")
        self.fake.before_list = arrival
        self.jump("shell")
        self.assertEqual(reads[0], 4)
        self.assertEqual(self.fake.focus[-1], "w2:p1")

    def test_churn_reselects_moved_replaced_closed_and_ineligible(self):
        for change in ("move", "replace", "close", "working"):
            with self.subTest(change=change):
                for pane in list(self.fake.order):
                    self.fake.remove_agent(pane)
                self.fake.focus = None
                self.fake.add_agent("w1:p1", "done", session="old-" + change)
                self.fake.add_agent("w2:p1", "done", session="other-" + change)
                reads = [0]
                def churn(params):
                    reads[0] += 1
                    if reads[0] != 2:
                        return
                    if change == "working":
                        self.fake.set_status("w1:p1", "working")
                    else:
                        self.fake.remove_agent("w1:p1")
                        if change == "replace":
                            self.fake.add_agent("w1:p1", "idle", session="replacement")
                        if change == "move":
                            self.fake.add_agent("w3:p1", "done", session="old-" + change)
                self.fake.before_list = churn
                self.jump("shell")
                self.assertNotEqual(self.fake.focus[-1], "w1:p1")
                self.fake.before_list = None

    def test_bounded_churn_and_stale_focus_rejection(self):
        self.fake.add_agent("w1:p1", "done")
        self.fake.add_agent("w2:p1", "done", session="two")
        reads = [0]
        def churn(params):
            reads[0] += 1
            if reads[0] % 2 == 0:
                self.fake.agents["w1:p1"]["terminal_id"] += "new"
        self.fake.before_list = churn
        self.assertIn("queue changed", self.jump("shell").stdout)
        self.assertEqual(reads[0], 4)
        self.assertEqual(self.fake.count("agent.focus"), 0)
        self.fake.before_list = None
        def close_once(params):
            self.fake.before_focus = None
            self.fake.remove_agent(params["target"])
        self.fake.before_focus = close_once
        self.jump("shell")
        self.assertEqual(self.fake.focus[-1], "w2:p1")

    def test_focus_outside_lock_and_no_retry_ambiguous_timeout(self):
        self.fake.add_agent("w1:p1", "done")
        def check_lock(params):
            fd = store.try_lock(os.path.join(self.state, "sessions", "default", "lock"))
            self.assertIsNotNone(fd)
            os.close(fd)
        self.fake.before_focus = check_lock
        self.jump()
        with patch.dict(os.environ, self.env(), clear=True):
            real_call = herdr.call
            def call(method, params, **kwargs):
                if method == "agent.focus":
                    raise herdr.Unavailable("timeout")
                return real_call(method, params, **kwargs)
            with patch.object(herdr, "call", side_effect=call) as mock:
                hooks.action("jump-attention")
                self.assertEqual(sum(c.args[0] == "agent.focus" for c in mock.call_args_list), 1)

    def test_named_session_isolation_unavailable_and_concurrent_calls(self):
        self.fake.add_agent("w1:p1", "done")
        named = os.path.join(self.fake.dir, "sessions", "homelab", "herdr.sock")
        os.makedirs(os.path.dirname(named))
        os.symlink(self.fake.path, named)
        self.jump(HERDR_SOCKET_PATH=named)
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", "homelab", "state.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions", "default", "state.json")))
        self.assertIn("No such file", self.jump(HERDR_SOCKET_PATH="/tmp/no-aq-server.sock").stdout)
        errors = []
        def invoke():
            try:
                self.jump("shell")
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=invoke) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.fake.violations, [])

    def test_old_traversal_actions_are_not_exposed(self):
        import subprocess
        for action in ("next-attention", "previous-attention"):
            result = subprocess.run(self.argv("action", action), env=self.env(), capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)


def remote(pane, attn, ts):
    return {"pane_id": pane, "tokens": {"attn": attn, "attn_rank": hooks.model.RANK[attn], "attn_ts": "%013d" % ts}}


class AcrossMachinesTest(unittest.TestCase):
    def setUp(self):
        self.agents = [truth("w1:p1"), truth("w1:p2")]
        self.live = {"w1:p1": {"attn": "done", "attn_ns": 1_000 * 10**6}, "w1:p2": {"attn": "working", "attn_ns": 0}}

    def test_a_remote_blocked_agent_beats_a_local_done_one(self):
        remotes = [("nuc", [remote("wH:p1", "working", 1), remote("wH:p2", "blocked", 5_000)])]
        self.assertEqual(navigation.select_across(self.agents, self.live, remotes), ("nuc", "wH:p2"))

    def test_oldest_wins_within_a_group_and_local_wins_ties(self):
        remotes = [("nuc", [remote("wH:p1", "done", 999)]), ("vm", [remote("w7:p1", "done", 1)])]
        self.assertEqual(navigation.select_across(self.agents, self.live, remotes), ("vm", "w7:p1"))
        remotes = [("nuc", [remote("wH:p1", "done", 1_000)])]
        choice = navigation.select_across(self.agents, self.live, remotes)
        self.assertEqual((choice[0], choice[1].pane_id), (None, "w1:p1"))

    def test_unreachable_and_tokenless_machines_are_skipped(self):
        remotes = [("nuc", None), ("vm", [{"pane_id": "w1:p1", "tokens": {"attn": "blocked"}}])]
        choice = navigation.select_across(self.agents, self.live, remotes)
        self.assertEqual(choice[1].pane_id, "w1:p1")
        self.live["w1:p1"]["attn"] = "idle"
        self.assertIsNone(navigation.select_across(self.agents, self.live, remotes))


FAKE_CLI = """#!/bin/sh
echo "$*" >> "%(log)s"
case "$*" in
  "machine list --json") cat "%(dir)s/machines.json" ;;
  "--machine nuc agent list") cat "%(dir)s/nuc.json" ;;
  "--machine nuc agent focus "*) echo '{"result":{}}' ;;
  *) exit 1 ;;
esac
"""

FAKE_OSASCRIPT = """#!/bin/sh
printf '%%s\\n' "$2" >> "%(dir)s/osascript.log"
echo sent
"""


class AcrossMachinesE2E(HookTestCase):
    def setUp(self):
        super().setUp()
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="aq-cli-", dir="/tmp")
        self.log = os.path.join(self.dir, "cli.log")
        for name, body in (("herdr", FAKE_CLI), ("osascript", FAKE_OSASCRIPT)):
            path = os.path.join(self.dir, name)
            with open(path, "w") as f:
                f.write(body % {"dir": self.dir, "log": self.log})
            os.chmod(path, 0o755)
        machines = [{"id": "nuc", "label": "NUC", "target": "nuc", "enabled": True, "selected": False}]
        with open(os.path.join(self.dir, "machines.json"), "w") as f:
            json.dump(machines, f)
        self.nuc = [remote("wH:p1", "working", 1), remote("wH:p2", "blocked", 5_000)]
        self.write_nuc()

    def tearDown(self):
        import shutil

        super().tearDown()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_nuc(self):
        with open(os.path.join(self.dir, "nuc.json"), "w") as f:
            json.dump({"result": {"agents": self.nuc}}, f)

    def jump(self):
        return self.run_hook("action", "jump-attention", pane="w1:p1",
                             HERDR_BIN_PATH=os.path.join(self.dir, "herdr"),
                             HERDR_ATTENTION_QUEUE_OSASCRIPT=os.path.join(self.dir, "osascript"))

    def cli_calls(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_a_blocked_agent_on_another_machine_beats_a_local_done_one(self):
        self.fake.add_agent("w1:p1", "done", session="a")
        self.jump()
        self.assertIn("--machine nuc agent focus wH:p2", self.cli_calls())
        self.assertIsNone(self.fake.focus, "the local done agent is not focused")
        with open(os.path.join(self.dir, "osascript.log")) as f:
            script = f.read()
        # Combined list: wH:p2 (blocked) first, then the local done row: prefix, then alt+1.
        self.assertIn("csi:49;3u", script)

    def test_a_local_blocked_agent_stays_local(self):
        self.nuc = [remote("wH:p2", "blocked", 9_999_999_999_999)]  # blocked later than the local one
        self.write_nuc()
        self.fake.add_agent("w1:p2", "blocked", session="b")
        self.fake.add_agent("w1:p1", "idle", session="a")
        self.jump()
        self.assertEqual(self.fake.focus[-1], "w1:p2")
        self.assertFalse(any("focus" in c for c in self.cli_calls()))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "osascript.log")))
