import unittest

from attention_queue import model
from attention_queue.model import Truth

S = 10**9
NOW = 1_800_000_000 * S


def T(
    status,
    seq,
    terminal="t1",
    pane="w1:p1",
    agent="claude",
    session="claude:id:s1",
    tokens=None,
    completion_seq=None,
    background=False,
):
    return Truth(
        terminal,
        pane,
        agent,
        session,
        status,
        seq,
        tokens or {},
        completion_seq=completion_seq,
        background=background,
    )


def run(seq_of_steps, restoring=False, durable=None, settle_ns=0, rec=None, start=NOW, completions=False):
    """Apply steps one second apart; return the final record.

    A step is (status, seq[, hints]) or a Truth.
    """
    now = start
    for item in seq_of_steps:
        if isinstance(item, Truth):
            t, hints = item, ()
        else:
            t = T(item[0], item[1])
            hints = item[2] if len(item) > 2 else ()
        rec = model.step(rec, t, hints, now, restoring, durable, settle_ns, completions)
        now += S
    return rec


class FirstSightTest(unittest.TestCase):
    def test_plain_states(self):
        for status, attn in [
            ("idle", "idle"),
            ("working", "working"),
            ("blocked", "blocked"),
            ("unknown", "unknown"),
        ]:
            with self.subTest(status=status):
                rec = run([(status, 1)])
                self.assertEqual(rec["attn"], attn)
                self.assertFalse(rec["sticky"])

    def test_herdr_done_is_trusted_outside_restore(self):
        self.assertEqual(run([("done", 1)])["attn"], "done")

    def test_herdr_done_is_ignored_while_restoring(self):
        self.assertEqual(run([("done", 1)], restoring=True)["attn"], "idle")

    def test_durable_sticky_restored_while_restoring_even_if_closed(self):
        durable = {"sticky": True, "attn": "done", "attn_ns": NOW - 99 * S, "closed_ns": NOW}
        rec = run([("idle", 1)], restoring=True, durable=durable)
        self.assertEqual(rec["attn"], "done")
        self.assertEqual(rec["attn_ns"], NOW - 99 * S)

    def test_closed_durable_is_ignored_outside_restore(self):
        durable = {"sticky": True, "attn": "done", "attn_ns": NOW, "closed_ns": NOW}
        self.assertEqual(run([("idle", 1)], durable=durable)["attn"], "idle")

    def test_open_durable_is_used_outside_restore(self):
        durable = {"sticky": True, "attn": "done", "attn_ns": NOW, "closed_ns": None}
        self.assertEqual(run([("idle", 1)], durable=durable)["attn"], "done")

    def test_busy_agent_ignores_durable_sticky(self):
        durable = {"sticky": True, "attn": "done", "attn_ns": NOW, "closed_ns": None}
        rec = run([("working", 1)], durable=durable)
        self.assertEqual(rec["attn"], "working")
        self.assertFalse(rec["sticky"])


class TransitionTest(unittest.TestCase):
    def test_working_to_idle_is_sticky_done(self):
        self.assertEqual(run([("working", 1), ("idle", 2)])["attn"], "done")

    def test_completion_reported_as_done_is_sticky(self):
        self.assertEqual(run([("working", 1), ("done", 2)])["attn"], "done")

    def test_viewing_keeps_done(self):
        # Viewing flips herdr's done to idle without moving state_change_seq.
        rec = run([("working", 1), ("done", 2), ("idle", 2), ("idle", 2)])
        self.assertEqual(rec["attn"], "done")

    def test_working_again_clears_done(self):
        rec = run([("working", 1), ("idle", 2), ("working", 3)])
        self.assertEqual(rec["attn"], "working")
        self.assertFalse(rec["sticky"])
        self.assertEqual(run([("idle", 4)], rec=rec)["attn"], "done")

    def test_blocked_working_idle_is_done(self):
        self.assertEqual(run([("blocked", 1), ("working", 2), ("idle", 3)])["attn"], "done")

    def test_blocked_answered_straight_to_idle_is_not_done(self):
        self.assertEqual(run([("blocked", 1), ("idle", 2)])["attn"], "idle")

    def test_unknown_flap_holds_through_completion(self):
        rec = run([("working", 1), ("unknown", 2)])
        self.assertEqual(rec["attn"], "working")
        self.assertEqual(run([("idle", 3)], rec=rec)["attn"], "done")

    def test_idle_unknown_idle_stays_idle(self):
        self.assertEqual(run([("idle", 1), ("unknown", 2), ("idle", 3)])["attn"], "idle")

    def test_gap_resolved_by_later_working_hint(self):
        rec = run([("idle", 5), ("idle", 7)])
        self.assertEqual(rec["attn"], "idle")
        self.assertEqual(rec["gap_from"], 5)
        rec = run([("idle", 7, ("working",))], rec=rec)
        self.assertEqual(rec["attn"], "done")
        self.assertIsNone(rec["gap_from"])

    def test_gap_resolved_by_hint_in_the_same_step(self):
        self.assertEqual(run([("idle", 5), ("idle", 7, ("working",))])["attn"], "done")

    def test_gap_without_working_hint_stays_idle(self):
        self.assertEqual(run([("idle", 5), ("idle", 7, ("idle",))])["attn"], "idle")

    def test_single_step_from_idle_is_not_a_gap(self):
        rec = run([("idle", 5), ("idle", 6, ("working",))])
        self.assertEqual(rec["attn"], "idle")
        self.assertIsNone(rec["gap_from"])

    def test_seq_going_backwards_starts_fresh(self):
        rec = run([("working", 9), ("idle", 10)])
        self.assertEqual(rec["attn"], "done")
        self.assertEqual(run([("idle", 1)], rec=rec)["attn"], "idle")

    def test_new_session_in_same_terminal_starts_fresh(self):
        rec = run([("working", 1), ("idle", 2)])
        rec = model.step(
            rec, T("idle", 3, session="claude:id:other"), (), NOW + 10 * S, False, None, 0
        )
        self.assertEqual(rec["attn"], "idle")

    def test_new_agent_in_same_terminal_starts_fresh(self):
        rec = run([("working", 1), ("idle", 2)])
        rec = model.step(rec, T("working", 3, agent="codex"), (), NOW + 10 * S, False, None, 0)
        self.assertEqual(rec["attn"], "working")
        self.assertEqual(rec["agent"], "codex")

    def test_settle_window_ignores_relaunch_completion(self):
        rec = run([("working", 1)], restoring=True, settle_ns=30 * S)
        rec = model.step(rec, T("idle", 2), (), NOW + 5 * S, False, None, 30 * S)
        self.assertEqual(rec["attn"], "idle")
        rec = model.step(rec, T("working", 3), (), NOW + 40 * S, False, None, 30 * S)
        rec = model.step(rec, T("idle", 4), (), NOW + 41 * S, False, None, 30 * S)
        self.assertEqual(rec["attn"], "done")


class WaitingTest(unittest.TestCase):
    def test_background_rule_while_working_is_waiting(self):
        rec = run([T("working", 1, background=True)])
        self.assertEqual(rec["attn"], "waiting")
        self.assertEqual(run([T("working", 1)], rec=rec)["attn"], "working")

    def test_blocked_beats_background_work(self):
        rec = run([T("blocked", 1, tokens={"bg": "2"}, background=True)])
        self.assertEqual(rec["attn"], "blocked")

    def test_working_with_bg_token_is_working(self):
        self.assertEqual(run([T("working", 1, tokens={"bg": "1"})])["attn"], "working")

    def test_turn_ending_with_background_work_waits_then_is_done(self):
        rec = run([("working", 1), T("idle", 2, tokens={"bg": "1"})])
        self.assertEqual(rec["attn"], "waiting")
        self.assertTrue(rec["sticky"])
        rec = run([T("idle", 2, tokens={})], rec=rec)
        self.assertEqual(rec["attn"], "done")

    def test_bg_token_on_reviewed_agent_is_waiting_then_idle(self):
        rec = run([T("idle", 1, tokens={"bg": "3"})])
        self.assertEqual(rec["attn"], "waiting")
        self.assertEqual(run([T("idle", 1)], rec=rec)["attn"], "idle")

    def test_herdr_done_with_bg_token_is_waiting(self):
        rec = run([("working", 1), T("done", 2, tokens={"bg": "1"})])
        self.assertEqual(rec["attn"], "waiting")

    def test_bg_count_parsing(self):
        for value, count in [("2", 2), (" 1 ", 1), ("0", 0), ("-4", 0), ("x", 0), ("", 0)]:
            with self.subTest(value=value):
                self.assertEqual(model.bg_count({"bg": value}), count)
        self.assertEqual(model.bg_count({}), 0)

    def test_ack_while_waiting_stays_waiting(self):
        rec = run([("working", 1), T("idle", 2, tokens={"bg": "1"})])
        rec = model.apply_ack(rec, T("idle", 2, tokens={"bg": "1"}), NOW + 9 * S)
        self.assertEqual(rec["attn"], "waiting")
        self.assertEqual(run([T("idle", 2)], rec=rec)["attn"], "idle")


class ActivityTest(unittest.TestCase):
    """The agent's own `activity` token corrects what herdr cannot see."""

    def test_parsing(self):
        for value, parsed in [("blocked", "blocked"), (" Working ", "working"), ("idle", None), ("", None)]:
            with self.subTest(value=value):
                self.assertEqual(model.activity({"activity": value}), parsed)
        self.assertIsNone(model.activity({}))

    def test_question_herdr_reads_as_working_is_blocked(self):
        # A screen-detected agent still shows its spinner under a question.
        rec = run([("working", 1), T("working", 1, tokens={"activity": "blocked"})])
        self.assertEqual(rec["attn"], "blocked")
        rec = run([T("working", 1)], rec=rec)
        self.assertEqual(rec["attn"], "working")
        rec = run([("idle", 2)], rec=rec)
        self.assertEqual(rec["attn"], "done")

    def test_reported_blocked_beats_background_work_and_done(self):
        rec = run([T("working", 1, tokens={"activity": "blocked"}, background=True)])
        self.assertEqual(rec["attn"], "blocked")
        rec = run([("working", 1), T("idle", 2, tokens={"activity": "blocked", "bg": "1"})])
        self.assertEqual(rec["attn"], "blocked")

    def test_planning_outside_a_turn_is_working_then_idle(self):
        rec = run([("idle", 1), T("idle", 1, tokens={"activity": "working"})])
        self.assertEqual(rec["attn"], "working")
        self.assertEqual(run([("idle", 1)], rec=rec)["attn"], "idle")

    def test_reported_working_clears_done(self):
        rec = run([("working", 1), ("idle", 2)])
        self.assertEqual(rec["attn"], "done")
        rec = run([T("idle", 2, tokens={"activity": "working"})], rec=rec)
        self.assertEqual(rec["attn"], "working")
        self.assertEqual(run([("idle", 2)], rec=rec)["attn"], "idle")

    def test_herdr_blocked_beats_reported_working(self):
        rec = run([T("blocked", 1, tokens={"activity": "working"})])
        self.assertEqual(rec["attn"], "blocked")

    def test_reported_working_beats_waiting(self):
        rec = run([T("working", 1, tokens={"activity": "working"}, background=True)])
        self.assertEqual(rec["attn"], "working")
        rec = run([T("idle", 1, tokens={"activity": "working", "bg": "2"})])
        self.assertEqual(rec["attn"], "working")

    def test_first_sight_with_activity_is_never_done(self):
        rec = run([T("done", 3, tokens={"activity": "working"})])
        self.assertFalse(rec["sticky"])
        self.assertEqual(rec["attn"], "working")

    def test_completions_still_follow_herdr(self):
        rec = run(
            [("working", 1), T("working", 1, tokens={"activity": "blocked"}), T("idle", 2, completion_seq=2)],
            completions=True,
        )
        self.assertEqual(rec["attn"], "done")

    def test_unread_while_reported_busy_is_a_no_op(self):
        rec = run([T("idle", 1, tokens={"activity": "working"})])
        rec = model.apply_unread(rec, T("idle", 1, tokens={"activity": "working"}), NOW + 5 * S)
        self.assertFalse(rec["sticky"])


class CompletionSeqTest(unittest.TestCase):
    """herdr 0.9.2+ reports completion_seq; it decides completions."""

    def run_c(self, steps, **kw):
        return run(steps, completions=True, **kw)

    def test_completion_is_done(self):
        rec = self.run_c([("working", 1), T("idle", 2, completion_seq=2)])
        self.assertEqual(rec["attn"], "done")

    def test_idle_without_completion_is_idle(self):
        # e.g. a restored agent or pi /new: herdr reports no completion.
        rec = self.run_c([("working", 1), T("idle", 2)])
        self.assertEqual(rec["attn"], "idle")

    def test_missed_short_turn_is_done_without_hints(self):
        rec = self.run_c([("idle", 5), T("idle", 7, completion_seq=7)])
        self.assertEqual(rec["attn"], "done")

    def test_blocked_straight_to_idle_is_not_done(self):
        rec = self.run_c([("blocked", 1), T("idle", 2, completion_seq=2)])
        self.assertEqual(rec["attn"], "idle")

    def test_blocked_then_missed_work_then_idle_is_done(self):
        rec = self.run_c([("blocked", 1), T("idle", 3, completion_seq=3)])
        self.assertEqual(rec["attn"], "done")

    def test_first_sight_needs_a_completion(self):
        self.assertEqual(self.run_c([T("done", 4)])["attn"], "idle")
        self.assertEqual(self.run_c([T("done", 4, completion_seq=4)])["attn"], "done")

    def test_viewing_keeps_done(self):
        rec = self.run_c([("working", 1), T("done", 2, completion_seq=2), T("idle", 2, completion_seq=2)])
        self.assertEqual(rec["attn"], "done")

    def test_settle_window_still_applies(self):
        rec = run([("working", 1)], restoring=True, settle_ns=30 * S, completions=True)
        rec = model.step(rec, T("idle", 2, completion_seq=2), (), NOW + 5 * S, False, None, 30 * S, True)
        self.assertEqual(rec["attn"], "idle")


class TimestampTest(unittest.TestCase):
    def test_attn_ns_moves_only_when_attn_changes(self):
        rec = run([("working", 1)])
        entered = rec["attn_ns"]
        rec = model.step(rec, T("working", 1), (), NOW + 50 * S, False, None, 0)
        self.assertEqual(rec["attn_ns"], entered)
        rec = model.step(rec, T("idle", 2), (), NOW + 60 * S, False, None, 0)
        self.assertEqual(rec["attn_ns"], NOW + 60 * S)


class ActionTest(unittest.TestCase):
    def test_ack_clears_done(self):
        rec = run([("working", 1), ("idle", 2)])
        rec = model.apply_ack(rec, T("idle", 2), NOW + 9 * S)
        self.assertEqual(rec["attn"], "idle")
        self.assertFalse(rec["sticky"])
        self.assertEqual(rec["attn_ns"], NOW + 9 * S)

    def test_ack_on_working_keeps_working(self):
        rec = model.apply_ack(run([("working", 1)]), T("working", 1), NOW + 9 * S)
        self.assertEqual(rec["attn"], "working")

    def test_unread_marks_idle_done_and_survives_viewing(self):
        rec = model.apply_unread(run([("idle", 1)]), T("idle", 1), NOW + 9 * S)
        self.assertEqual(rec["attn"], "done")
        self.assertEqual(run([("idle", 1)], rec=rec)["attn"], "done")

    def test_unread_on_working_is_a_no_op(self):
        rec = run([("working", 1)])
        self.assertEqual(model.apply_unread(rec, T("working", 1), NOW + 9 * S), rec)


class TokenTest(unittest.TestCase):
    def test_token_shape(self):
        tokens = model.tokens_for(run([("working", 1), ("idle", 2)]))
        self.assertEqual(set(tokens), set(model.TOKEN_NAMES))
        self.assertEqual(tokens["attn"], "done")
        self.assertEqual(tokens["attn_rank"], "1")
        self.assertEqual(len(tokens["attn_ts"]), 13)
        self.assertTrue(tokens["attn_ts"].isdigit())

    def test_string_sort_matches_attention_order(self):
        recs = [
            {"attn": "idle", "attn_ns": 1 * S},
            {"attn": "done", "attn_ns": 5 * S},
            {"attn": "blocked", "attn_ns": 9 * S},
            {"attn": "working", "attn_ns": 2 * S},
            {"attn": "done", "attn_ns": 3 * S},
            {"attn": "waiting", "attn_ns": 4 * S},
            {"attn": "unknown", "attn_ns": 0},
        ]
        keyed = sorted(
            recs,
            key=lambda r: (model.tokens_for(r)["attn_rank"], model.tokens_for(r)["attn_ts"]),
        )
        self.assertEqual(
            [(r["attn"], r["attn_ns"]) for r in keyed],
            [
                ("blocked", 9 * S),
                ("done", 3 * S),
                ("done", 5 * S),
                ("working", 2 * S),
                ("waiting", 4 * S),
                ("idle", 1 * S),
                ("unknown", 0),
            ],
        )

    def test_rank_and_icon_tables(self):
        self.assertEqual(
            [s for s, _ in sorted(model.RANK.items(), key=lambda kv: kv[1])],
            ["blocked", "done", "working", "waiting", "idle", "unknown"],
        )
        self.assertEqual(set(model.ICON), set(model.RANK))
        self.assertEqual(len(set(model.ICON.values())), len(model.ICON))
        for state in model.STATES:
            tokens = model.tokens_for({"attn": state, "attn_ns": 0})
            self.assertEqual(tokens["attn_icon"], model.ICON[state])

    def test_row_text(self):
        self.assertEqual(model.row_text("done", "data-pipeline"), model.ICON["done"] + "  data-pipeline")
        self.assertEqual(model.row_text("waiting", None), model.ICON["waiting"])
        self.assertEqual(model.row_text("bogus", "x"), model.ICON["unknown"] + "  x")

    def test_view_sort_never_uses_view_or_per_server_fields(self):
        fields = [s["field"] for s in model.VIEW_SORT]
        for banned in ("seen", "attention", "status", "state_change_seq"):
            self.assertNotIn(banned, fields)


if __name__ == "__main__":
    unittest.main()
