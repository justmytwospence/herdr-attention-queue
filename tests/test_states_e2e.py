"""End-to-end: waiting, icons and completion_seq through the real entrypoint."""

from attention_queue import model
from tests.test_hooks_e2e import HookTestCase


class WaitingE2ETest(HookTestCase):
    def test_bg_token_turns_a_finished_turn_into_waiting_then_done(self):
        f = self.fake
        f.add_agent("w1:p1", "working", agent="pi", session="/tmp/a.jsonl", skipped=True)
        self.event("w1:p1")
        # The agent started background work during its turn.
        f.set_token("w1:p1", "bg", "1")
        f.set_status("w1:p1", "idle")
        self.event("w1:p1")
        tokens = f.tokens("w1:p1")
        self.assertEqual(tokens["attn"], "waiting")
        self.assertEqual(tokens["attn_rank"], model.RANK["waiting"])
        self.assertEqual(tokens["attn_icon"], model.ICON["waiting"])

        f.set_token("w1:p1", "bg", None)
        self.run_hook("action", "reapply")
        self.assertEqual(self.attn("w1:p1"), "done")
        self.assertEqual(f.tokens("w1:p1")["attn_icon"], model.ICON["done"])
        self.assertEqual(f.violations, [])

    def test_focusing_a_done_pane_leaves_every_token_unchanged(self):
        f = self.fake
        f.add_agent("w1:p1", "working")
        self.event("w1:p1")
        f.set_status("w1:p1", "done")
        self.event("w1:p1")
        before = f.tokens("w1:p1")
        reports = f.count("pane.report_metadata")
        # Viewing: herdr's done flips to idle and its seq does not move.
        f.set_status("w1:p1", "idle")
        self.run_hook("event", event={"event": "pane_focused", "data": {"pane_id": "w1:p1"}})
        self.assertEqual(f.tokens("w1:p1"), before)
        self.assertEqual(f.count("pane.report_metadata"), reports)


class CompletionSeqE2ETest(HookTestCase):
    def setUp(self):
        super().setUp()
        self.fake.version = "0.9.2"

    def test_real_completion_is_done(self):
        f = self.fake
        f.add_agent("w1:p1", "working")
        self.event("w1:p1")
        f.set_status("w1:p1", "idle")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "done")

    def test_idle_without_completion_is_not_done(self):
        # herdr 0.9.2 reports done without a completion for e.g. pi /new.
        f = self.fake
        f.add_agent("w1:p1", "idle")
        self.event("w1:p1")
        f.set_status("w1:p1", "working")
        f.agents["w1:p1"]["agent_status"] = "done"
        f.agents["w1:p1"]["state_change_seq"] += 1
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "idle")

    def test_missed_short_turn_is_done_without_a_working_hint(self):
        f = self.fake
        f.add_agent("w1:p1", "idle")
        self.event("w1:p1")
        f.set_status("w1:p1", "working")
        f.set_status("w1:p1", "idle")
        self.event("w1:p1", status="idle")
        self.assertEqual(self.attn("w1:p1"), "done")


class TickerTest(HookTestCase):
    def test_background_rule_is_waiting_without_any_event(self):
        f = self.fake
        f.add_agent("w1:p1", "working")
        self.event("w1:p1")
        self.assertTrue(self.wait_for(self.ticker_running), "no ticker started")
        # Claude: "Waiting for 2 background agents". Same herdr status, no event.
        f.set_rule("w1:p1", "background_agents_working")
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "waiting"))
        f.set_rule("w1:p1", "working_spinner")
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "working"))
        # The background agents woke it, it finished; the ticker catches up
        # before the event hook would.
        f.set_rule("w1:p1", None)
        f.set_status("w1:p1", "idle")
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "done"))
        self.assertTrue(self.wait_for(lambda: not self.ticker_running()), "ticker kept running")
        self.assertEqual(f.violations, [])

    def test_bg_token_expiry_turns_waiting_into_done(self):
        f = self.fake
        f.add_agent("w1:p1", "working", agent="pi", session="/tmp/b.jsonl", skipped=True)
        self.event("w1:p1")
        f.set_token("w1:p1", "bg", "2")
        f.set_status("w1:p1", "idle")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "waiting")
        self.assertTrue(self.ticker_running() or self.wait_for(self.ticker_running))
        f.set_token("w1:p1", "bg", None)
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "done"))
        self.assertTrue(self.wait_for(lambda: not self.ticker_running()))
        # pi reports its own state; the ticker never asks herdr to explain it.
        self.assertEqual(f.count("agent.explain"), 0)

    def test_no_ticker_while_every_agent_is_idle(self):
        self.fake.add_agent("w1:p1", "idle")
        self.event("w1:p1")
        self.assertFalse(self.ticker_running())
        self.assertEqual(self.fake.count("agent.explain"), 0)

    def test_one_ticker_at_a_time(self):
        self.fake.add_agent("w1:p1", "working")
        self.event("w1:p1")
        self.assertTrue(self.wait_for(self.ticker_running))
        self.run_hook("ticker")  # returns at once: the lock is held
        self.fake.set_status("w1:p1", "idle")
        self.assertTrue(self.wait_for(lambda: not self.ticker_running()))
