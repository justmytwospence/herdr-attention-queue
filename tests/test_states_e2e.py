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


class RowTokenTest(HookTestCase):
    def row(self, pane):
        return self.fake.tokens(pane).get(model.ROW_TOKEN)

    def test_row_is_icon_and_workspace_and_follows_state_and_renames(self):
        f = self.fake
        f.add_agent("w1:p1", "working")
        self.event("w1:p1")
        self.assertEqual(self.row("w1:p1"), model.ICON["working"] + "  ws w1")
        f.set_status("w1:p1", "idle")
        self.event("w1:p1")
        self.assertEqual(self.row("w1:p1"), model.ICON["done"] + "  ws w1")
        f.workspace_names["w1"] = "data-pipeline"
        self.run_hook("event", event={"event": "workspace_renamed", "data": {"workspace_id": "w1"}})
        self.assertEqual(self.row("w1:p1"), model.ICON["done"] + "  data-pipeline")
        self.run_hook("action", "clear")
        self.assertIsNone(self.row("w1:p1"))
        self.assertEqual(f.violations, [])

    def test_agents_sharing_a_workspace_are_told_apart_by_tab(self):
        f = self.fake
        f.workspace_names["w1"] = "homelab"
        f.add_agent("w1:p1", "working", session="a")
        self.event("w1:p1")
        self.assertEqual(self.row("w1:p1"), model.ICON["working"] + "  homelab")
        f.add_agent("w1:p2", "working", session="b")
        f.agents["w1:p2"]["tab_id"] = "w1:t2"
        f.tab_names.update({"w1:t1": "navigation", "w1:t2": "attention queue"})
        self.run_hook("event", event={"event": "pane_agent_detected", "data": {"pane_id": "w1:p2"}})
        self.assertEqual(self.row("w1:p1"), model.ICON["working"] + "  homelab › navigation")
        self.assertEqual(self.row("w1:p2"), model.ICON["working"] + "  homelab › attention queue")
        f.tab_names["w1:t1"] = "nav"
        self.run_hook("event", event={"event": "tab_renamed", "data": {"tab_id": "w1:t1"}})
        self.assertEqual(self.row("w1:p1"), model.ICON["working"] + "  homelab › nav")
        self.assertEqual(f.violations, [])


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

    def test_ticker_hands_over_after_a_plugin_update(self):
        import os
        import tempfile
        import shutil
        import subprocess
        from tests.test_hooks_e2e import ROOT

        # A copy of the plugin, so the test can "update" it.
        root = tempfile.mkdtemp(prefix="aq-root-", dir="/tmp")
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copy(os.path.join(ROOT, "attention.py"), root)
        shutil.copytree(os.path.join(ROOT, "attention_queue"), os.path.join(root, "attention_queue"))
        self.fake.add_agent("w1:p1", "working")
        self.run_hook("event", event=self.status_event("w1:p1"), HERDR_PLUGIN_ROOT=root)
        self.assertTrue(self.wait_for(self.ticker_running))
        # No shell: on Linux `sh -c pgrep ...` matches its own command line,
        # so the old os.popen predicate saw two PIDs instead of one forever.
        def pids():
            result = subprocess.run(
                ["pgrep", "-f", root + "/attention.py ticker"],
                capture_output=True, text=True,
            )
            return set(result.stdout.split())
        self.assertTrue(self.wait_for(lambda: len(pids()) == 1))
        first = pids()
        model_py = os.path.join(root, "attention_queue", "model.py")
        os.utime(model_py, ns=(os.stat(model_py).st_atime_ns, os.stat(model_py).st_mtime_ns + 10**9))
        self.assertTrue(self.wait_for(lambda: len(pids()) == 1 and pids() != first), "no fresh ticker")
        self.fake.set_status("w1:p1", "idle")
        self.assertTrue(self.wait_for(lambda: not pids()))

    def test_one_ticker_at_a_time(self):
        self.fake.add_agent("w1:p1", "working")
        self.event("w1:p1")
        self.assertTrue(self.wait_for(self.ticker_running))
        self.run_hook("ticker")  # returns at once: the lock is held
        self.fake.set_status("w1:p1", "idle")
        self.assertTrue(self.wait_for(lambda: not self.ticker_running()))


class ActivityE2ETest(HookTestCase):
    def test_planning_reported_while_idle_shows_working_after_refresh(self):
        f = self.fake
        f.add_agent("w1:p1", "idle", agent="pi", session="/tmp/p.jsonl", skipped=True)
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "idle")
        # A token change emits no plugin event; the integration asks for a refresh.
        f.set_token("w1:p1", "activity", "working")
        self.run_hook("action", "refresh")
        self.assertEqual(self.attn("w1:p1"), "working")
        # The ticker watches the token, so its clearing shows without an event.
        self.assertTrue(self.wait_for(self.ticker_running))
        f.set_token("w1:p1", "activity", None)
        # The planner run ended: a finished turn to review.
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "done"))
        self.assertTrue(self.wait_for(lambda: not self.ticker_running()))
        self.assertEqual(f.violations, [])

    def test_question_under_a_spinner_is_blocked_without_any_event(self):
        f = self.fake
        f.add_agent("w1:p1", "working", agent="pi")
        self.event("w1:p1")
        self.assertTrue(self.wait_for(self.ticker_running))
        f.set_token("w1:p1", "activity", "blocked")
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "blocked"))
        f.set_token("w1:p1", "activity", None)
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "working"))
        f.set_status("w1:p1", "idle")
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "done"))
        self.assertEqual(f.violations, [])


class ReportedTurnE2ETest(HookTestCase):
    def test_an_agent_herdr_cannot_read_reports_its_turn_and_finishes_as_done(self):
        f = self.fake
        f.add_agent("w1:p1", "unknown", agent="codex", session="c")
        f.set_token("w1:p1", "activity", "working")
        self.run_hook("action", "refresh")
        self.assertEqual(self.attn("w1:p1"), "working")
        self.assertTrue(self.wait_for(self.ticker_running))
        f.set_token("w1:p1", "activity", "idle")  # its Stop hook; no herdr event
        self.assertTrue(self.wait_for(lambda: self.attn("w1:p1") == "done"))
        self.assertTrue(self.wait_for(lambda: not self.ticker_running()))
        self.assertEqual(f.violations, [])
