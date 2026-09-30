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
        # Lexical pane ordering and nanoseconds both disagree with layout ties.
        self.agents = [truth(p) for p in ("w1:pZ", "w1:p2", "w2:p1", "w3:p1")]
        self.live = {t.terminal_id: {"attn": "done", "attn_ns": 1999999}
                     for t in self.agents}
        self.live["w1:p2"]["attn_ns"] = 1000001
        self.live["w2:p1"] = {"attn": "blocked", "attn_ns": 9000000}
        self.live["w3:p1"]["attn"] = "waiting"

    def test_priority_and_millisecond_layout_ties(self):
        self.assertEqual([t.pane_id for t in navigation.candidates(self.agents, self.live)],
                         ["w2:p1", "w1:pZ", "w1:p2"])

    def test_traversal_wrap_and_missing_anchor(self):
        order = ["w2:p1", "w1:pZ", "w1:p2"]
        for i, pane in enumerate(order):
            for direction in (1, -1):
                result = navigation.select(self.agents, self.live, pane, direction)
                self.assertEqual(result.pane_id, order[(i + direction) % 3])
        for anchor in (None, "shell", "w3:p1"):
            for direction in (1, -1):
                self.assertEqual(navigation.select(self.agents, self.live, anchor, direction).pane_id,
                                 order[0])

    def test_oldest_first_empty_singleton_and_excluded_states(self):
        self.live["w1:pZ"]["attn_ns"] = 2000000
        self.assertEqual(navigation.candidates(self.agents, self.live)[1].pane_id, "w1:p2")
        for state in ("working", "waiting", "idle", "unknown"):
            for rec in self.live.values():
                rec["attn"] = state
            self.assertIsNone(navigation.select(self.agents, self.live, None, 1))
        self.live["w1:pZ"]["attn"] = "done"
        self.assertEqual(navigation.select(self.agents, self.live, "w1:pZ", -1).pane_id, "w1:pZ")

    def test_invocation_context_never_server_focus(self):
        with patch.dict(os.environ, {"HERDR_PANE_ID": "caller", "HERDR_PLUGIN_CONTEXT_JSON":
                                     '{"focused_pane_id":"context"}'}, clear=True):
            self.assertEqual(hooks.navigation_anchor(), "caller")
            del os.environ["HERDR_PANE_ID"]
            self.assertEqual(hooks.navigation_anchor(), "context")
            os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = '[]'
            self.assertIsNone(hooks.navigation_anchor())


class NavigationE2E(HookTestCase):
    def test_focus_seen_preserves_sticky_and_requires_explicit_ack(self):
        for p in ("w1:p1", "w2:p1", "w3:p1"):
            self.fake.add_agent(p, "done", session=p)
        self.fake.agents["w2:p1"]["tab_id"] = "w2:t3"
        self.run_hook("action", "next-attention", pane="w1:p1")
        self.assertEqual(self.fake.focus, ("w2", "w2:t3", "w2:p1"))
        self.assertEqual(self.fake.agents["w2:p1"]["agent_status"], "idle")
        self.run_hook("action", "previous-attention", pane="w2:p1")
        self.assertEqual(self.fake.focus[-1], "w1:p1")
        self.assertEqual(self.attn("w2:p1"), "done")
        self.run_hook("action", "mark-reviewed", pane="w2:p1")
        self.run_hook("action", "next-attention", pane="w1:p1")
        self.assertEqual(self.fake.focus[-1], "w3:p1")
        self.assertEqual(self.attn("w2:p1"), "idle")
        self.assertEqual(self.fake.violations, [])
        self.assertEqual(self.fake.count("agent.view.set"), 0)

    def test_empty_singleton_context_and_waiting(self):
        out = self.run_hook("action", "next-attention")
        self.assertIn("No agents need attention", out.stdout)
        self.fake.add_agent("w1:p1", "done")
        self.fake.set_token("w1:p1", "bg", "1")
        self.run_hook("action", "next-attention")
        self.assertIsNone(self.fake.focus)
        self.fake.set_token("w1:p1", "bg", None)
        self.run_hook("action", "previous-attention", HERDR_PLUGIN_CONTEXT_JSON=
                      json.dumps({"focused_pane_id": "w1:p1"}))
        self.assertIsNone(self.fake.focus)
        self.run_hook("action", "next-attention", pane="shell")
        self.assertEqual(self.fake.focus[-1], "w1:p1")

    def test_retained_done_during_unknown_flap(self):
        self.fake.add_agent("w1:p1", "done")
        self.run_hook("action", "next-attention", pane="w1:p1")
        self.fake.set_status("w1:p1", "unknown")
        self.run_hook("action", "next-attention", pane="shell")
        self.assertEqual(self.fake.focus[-1], "w1:p1")

    def test_churn_reselects_moved_replaced_closed_and_ineligible(self):
        for change in ("move", "replace", "close", "working"):
            with self.subTest(change=change):
                # Reset live fixtures and persisted state between cases.
                for pane in list(self.fake.order):
                    self.fake.remove_agent(pane)
                self.fake.focus = None
                self.fake.add_agent("w1:p1", "done", session="old-" + change)
                self.fake.add_agent("w2:p1", "done", session="other-" + change)
                n = [0]
                def churn(params):
                    n[0] += 1
                    if n[0] != 2:
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
                self.run_hook("action", "next-attention", pane="shell")
                self.assertNotEqual(self.fake.focus[-1], "w1:p1")
                self.assertEqual(self.fake.count("agent.focus"),
                                 ("move", "replace", "close", "working").index(change) + 1)
                self.fake.before_list = None

    def test_bounded_churn_and_stale_focus_rejection(self):
        self.fake.add_agent("w1:p1", "done")
        self.fake.add_agent("w2:p1", "done", session="two")
        n = [0]
        def churn(params):
            n[0] += 1
            if n[0] % 2 == 0:
                self.fake.agents["w1:p1"]["terminal_id"] += "new"
        self.fake.before_list = churn
        out = self.run_hook("action", "next-attention", pane="shell")
        self.assertEqual(n[0], 4)
        self.assertIn("queue changed", out.stdout)
        self.assertEqual(self.fake.count("agent.focus"), 0)
        self.fake.before_list = None
        def close_once(params):
            self.fake.before_focus = None
            self.fake.remove_agent(params["target"])
        self.fake.before_focus = close_once
        self.run_hook("action", "next-attention", pane="shell")
        self.assertEqual(self.fake.focus[-1], "w2:p1")

    def test_focus_outside_lock_and_no_retry_ambiguous_timeout(self):
        self.fake.add_agent("w1:p1", "done")
        def check_lock(params):
            lock = os.path.join(self.state, "sessions", "default", "lock")
            fd = store.try_lock(lock)
            self.assertIsNotNone(fd)
            os.close(fd)
        self.fake.before_focus = check_lock
        self.run_hook("action", "next-attention")
        with patch.dict(os.environ, self.env(), clear=True):
            real_call = herdr.call
            def call(method, params, **kwargs):
                if method == "agent.focus":
                    raise herdr.Unavailable("timeout")
                return real_call(method, params, **kwargs)
            with patch.object(herdr, "call", side_effect=call) as mock:
                hooks.action("next-attention")
                self.assertEqual(sum(c.args[0] == "agent.focus" for c in mock.call_args_list), 1)

    def test_named_session_isolation_unavailable_and_concurrent_calls(self):
        self.fake.add_agent("w1:p1", "done")
        named = os.path.join(self.fake.dir, "sessions", "homelab", "herdr.sock")
        os.makedirs(os.path.dirname(named))
        os.symlink(self.fake.path, named)
        self.run_hook("action", "next-attention", HERDR_SOCKET_PATH=named)
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", "homelab", "state.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "sessions", "default", "state.json")))
        out = self.run_hook("action", "next-attention", HERDR_SOCKET_PATH="/tmp/no-aq-server.sock")
        self.assertIn("No such file", out.stdout)
        errors = []
        def invoke():
            try:
                self.run_hook("action", "next-attention", pane="shell")
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=invoke) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.fake.violations, [])
