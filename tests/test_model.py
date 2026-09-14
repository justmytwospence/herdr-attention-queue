import unittest

from attention_queue import model
from attention_queue.model import Truth

S = 10**9
NOW = 1_800_000_000 * S


def T(status, seq, terminal="t1", pane="w1:p1", agent="claude", session="claude:id:s1"):
    return Truth(terminal, pane, agent, session, status, seq, {})


def run(seq_of_steps, restoring=False, durable=None, settle_ns=0, rec=None, start=NOW):
    """Apply (status, seq[, hints]) steps one second apart; return the final record."""
    now = start
    for item in seq_of_steps:
        status, seq = item[0], item[1]
        hints = item[2] if len(item) > 2 else ()
        rec = model.step(rec, T(status, seq), hints, now, restoring, durable, settle_ns)
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
        rec = model.apply_ack(rec, "idle", NOW + 9 * S)
        self.assertEqual(rec["attn"], "idle")
        self.assertFalse(rec["sticky"])
        self.assertEqual(rec["attn_ns"], NOW + 9 * S)

    def test_ack_on_working_keeps_working(self):
        rec = model.apply_ack(run([("working", 1)]), "working", NOW + 9 * S)
        self.assertEqual(rec["attn"], "working")

    def test_unread_marks_idle_done_and_survives_viewing(self):
        rec = model.apply_unread(run([("idle", 1)]), "idle", NOW + 9 * S)
        self.assertEqual(rec["attn"], "done")
        self.assertEqual(run([("idle", 1)], rec=rec)["attn"], "done")

    def test_unread_on_working_is_a_no_op(self):
        rec = run([("working", 1)])
        self.assertEqual(model.apply_unread(rec, "working", NOW + 9 * S), rec)


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
                ("idle", 1 * S),
                ("unknown", 0),
            ],
        )

    def test_view_sort_never_uses_view_or_per_server_fields(self):
        fields = [s["field"] for s in model.VIEW_SORT]
        for banned in ("seen", "attention", "status", "state_change_seq"):
            self.assertNotIn(banned, fields)


if __name__ == "__main__":
    unittest.main()
