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


class CycleTest(unittest.TestCase):
    def test_enters_the_most_urgent_tier_at_its_first_row(self):
        rows = [("a", "done"), ("b", "blocked"), ("c", "blocked"), ("d", "working")]
        self.assertEqual(navigation.cycle(rows, "a"), 1)
        self.assertEqual(navigation.cycle(rows, "d"), 1)
        self.assertEqual(navigation.cycle(rows, None), 1)

    def test_cycles_within_the_tier_and_wraps(self):
        rows = [("a", "blocked"), ("b", "working"), ("c", "blocked"), ("d", "blocked")]
        self.assertEqual(navigation.cycle(rows, "a"), 2)
        self.assertEqual(navigation.cycle(rows, "c"), 3)
        self.assertEqual(navigation.cycle(rows, "d"), 0)

    def test_done_only_when_nothing_is_blocked_and_lone_rows_stay(self):
        self.assertEqual(navigation.cycle([("a", "working"), ("b", "done")], "a"), 1)
        self.assertEqual(navigation.cycle([("a", "working"), ("b", "done")], "b"), 1)
        self.assertIsNone(navigation.cycle([("a", "working"), ("b", "idle")], "a"))
        self.assertIsNone(navigation.cycle([], None))


class NavigationE2E(HookTestCase):
    def jump(self, pane=None, **extra):
        return self.run_hook("action", "jump-attention", pane=pane, **extra)

    def test_ties_cycle_in_panel_order_and_a_lone_head_stays(self):
        for p in ("w2:p1", "w1:p1", "w3:p1"):
            self.fake.add_agent(p, "done", session=p)
        self.fake.agents["w2:p1"]["tab_id"] = "w2:t3"
        # All done in the same millisecond: panel (layout) order w2:p1, w1:p1, w3:p1.
        self.jump("shell")
        self.assertEqual(self.fake.focus, ("w2", "w2:t3", "w2:p1"))
        self.assertEqual(self.fake.agents["w2:p1"]["agent_status"], "idle")
        self.assertEqual(self.attn("w2:p1"), "done", "focusing never reviews")
        self.jump("w2:p1")
        self.assertEqual(self.fake.focus[-1], "w1:p1")
        self.jump("w1:p1")
        self.assertEqual(self.fake.focus[-1], "w3:p1")
        self.jump("w3:p1")
        self.assertEqual(self.fake.focus[-1], "w2:p1", "wraps around")
        for p in ("w1:p1", "w3:p1"):
            self.run_hook("action", "mark-reviewed", pane=p)
        calls = self.fake.count("agent.focus")
        self.jump("w2:p1")
        self.assertEqual(self.fake.count("agent.focus"), calls, "the only one left: stay")
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


class DelegateToNotifierE2E(HookTestCase):
    """On a server without saved machines, a jump goes to the notifier reading its log."""

    def session_dir(self):
        return os.path.join(self.state, "sessions", "default")

    def jump_lines(self):
        from attention_queue import translog

        try:
            with open(translog.path(self.session_dir())) as f:
                return [json.loads(x) for x in f if '"jump"' in x]
        except OSError:
            return []

    def test_a_followed_server_asks_the_notifier(self):
        from attention_queue import translog

        self.fake.add_agent("w1:p1", "done", session="a")
        self.run_hook("event", event=self.status_event("w1:p1"))
        translog.touch_alive(self.session_dir())
        self.run_hook("action", "jump-attention", pane="w1:p9")
        self.assertIsNone(self.fake.focus)
        self.assertEqual([(x["kind"], x["pane_id"]) for x in self.jump_lines()], [("jump", "w1:p9")])

    def test_without_a_reader_the_jump_stays_on_this_server(self):
        self.fake.add_agent("w1:p1", "done", session="a")
        self.run_hook("action", "jump-attention", pane="w1:p9")
        self.assertEqual(self.fake.focus[-1], "w1:p1")
        self.assertEqual(self.jump_lines(), [])
