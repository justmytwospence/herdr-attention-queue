"""End-to-end: run the real entrypoint as subprocesses against a fake herdr."""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from attention_queue import model, store, usage
from tests.fakeherdr import FakeHerdr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "attention.py")


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHerdr()
        self.state = tempfile.mkdtemp(prefix="aq-state-", dir="/tmp")
        # Usage and names read the network, the Keychain, and ~/.claude: off
        # unless a test turns them on with fixtures.
        self.config = tempfile.mkdtemp(prefix="aq-config-", dir="/tmp")
        self.write_config(usage=False, claude_names=False)

    def write_config(self, **values):
        with open(os.path.join(self.config, "config.json"), "w") as f:
            json.dump(values, f)

    def tearDown(self):
        # Close the server first: background processes exit once it is gone.
        self.fake.close()
        self.wait_for_reseed()
        self.wait_for_ticker()
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.config, ignore_errors=True)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env.update(
            {
                "HERDR_SOCKET_PATH": self.fake.path,
                "HERDR_PLUGIN_STATE_DIR": self.state,
                "HERDR_PLUGIN_CONFIG_DIR": self.config,
                "HERDR_PLUGIN_ID": "attention-queue",
                "HERDR_PLUGIN_ROOT": ROOT,
                "PYTHONDONTWRITEBYTECODE": "1",
                "HERDR_ATTENTION_QUEUE_SETTLE_S": "0",
                "HERDR_ATTENTION_QUEUE_RESTORE_S": "0",
                "HERDR_ATTENTION_QUEUE_RESEED_SCHEDULE": "0.05,0.05",
                "HERDR_ATTENTION_QUEUE_TICK_S": "0.05",
            }
        )
        env.update(extra)
        return env

    def argv(self, *args):
        return [sys.executable, "-B", ENTRY] + list(args)

    def run_hook(self, *args, event=None, pane=None, **extra):
        env = self.env(**extra)
        if event is not None:
            env["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(event)
        if pane is not None:
            env["HERDR_PANE_ID"] = pane
        proc = subprocess.run(
            self.argv(*args), env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def status_event(self, pane, status=None):
        agent = self.fake.agents[pane]
        return {
            "event": "pane_agent_status_changed",
            "data": {
                "type": "pane_agent_status_changed",
                "pane_id": pane,
                "workspace_id": agent["workspace_id"],
                "agent_status": status or agent["agent_status"],
                "agent": agent["agent"],
            },
        }

    def event(self, pane, status=None):
        return self.run_hook("event", event=self.status_event(pane, status))

    def attn(self, pane):
        return self.fake.tokens(pane).get("attn")

    def wait_for(self, predicate, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def wait_for_reseed(self):
        return self.wait_for_lock("reseed.lock")

    def wait_for_ticker(self):
        return self.wait_for_lock("ticker.lock")

    def ticker_running(self):
        lock = os.path.join(self.state, "sessions", "default", "ticker.lock")
        if not os.path.exists(lock):
            return False
        fd = store.try_lock(lock)
        if fd is None:
            return True
        os.close(fd)
        return False

    def wait_for_lock(self, name):
        lock = os.path.join(self.state, "sessions", "default", name)
        if not os.path.exists(lock):
            return

        def released():
            fd = store.try_lock(lock)
            if fd is None:
                return False
            os.close(fd)
            return True

        self.wait_for(released, timeout=30)


class StickyDoneTest(HookTestCase):
    def test_done_holds_until_the_agent_works_again(self):
        f = self.fake
        f.add_agent("w1:p1", "working", session="one")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "working")

        # Finished while focused: herdr reports idle, not done.
        f.set_status("w1:p1", "idle")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "done")

        # Viewing emits no event; another pane's event reconciles this one too.
        f.add_agent("w1:p2", "idle", session="two")
        self.event("w1:p2")
        self.assertEqual(self.attn("w1:p1"), "done")
        self.assertEqual(self.attn("w1:p2"), "idle")

        f.set_status("w1:p1", "working")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "working")

        f.set_status("w1:p1", "done")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "done")
        self.assertEqual(self.fake.tokens("w1:p1")["attn_rank"], "1")

        self.run_hook("action", "mark-reviewed", pane="w1:p1")
        self.assertEqual(self.attn("w1:p1"), "idle")
        self.run_hook("action", "mark-unread", pane="w1:p1")
        self.assertEqual(self.attn("w1:p1"), "done")
        self.run_hook("action", "mark-all-reviewed")
        self.assertEqual(self.attn("w1:p1"), "idle")
        self.assertEqual(f.violations, [])

    def test_answered_prompt_is_not_done(self):
        self.fake.add_agent("w1:p1", "blocked")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "blocked")
        self.fake.set_status("w1:p1", "idle")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "idle")

    def test_action_on_pane_without_agent(self):
        proc = self.run_hook("action", "mark-reviewed", pane="w9:p9")
        self.assertIn("has no agent", proc.stdout)

    def test_unknown_action_is_rejected(self):
        proc = subprocess.run(
            self.argv("action", "nope"), env=self.env(), capture_output=True, text=True, timeout=30
        )
        self.assertEqual(proc.returncode, 2)


class ConcurrencyTest(HookTestCase):
    def test_concurrent_shuffled_hooks_converge(self):
        f = self.fake
        panes = ["w1:p%d" % i for i in range(1, 6)]
        for i, pane in enumerate(panes):
            f.add_agent(pane, "working", session="s%d" % i)
        f.add_agent("w2:p1", "idle", session="gap")
        self.event(panes[0])

        for pane in panes:
            f.set_status(pane, "idle")
        # A short turn that ran and ended before any of its hooks reconciled.
        f.set_status("w2:p1", "working")
        f.set_status("w2:p1", "idle")

        events = []
        for pane in panes:
            events += [self.status_event(pane)] * 3
        events += [self.status_event("w2:p1")] * 3
        events.append(self.status_event("w2:p1", status="working"))
        random.shuffle(events)

        procs = []
        for event in events:
            env = self.env(HERDR_PLUGIN_EVENT_JSON=json.dumps(event))
            procs.append(
                subprocess.Popen(
                    self.argv("event"),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, (out + err).decode())

        for pane in panes + ["w2:p1"]:
            self.assertEqual(self.attn(pane), "done", pane)
        self.assertEqual(f.violations, [])


class RestartTest(HookTestCase):
    def test_restart_restores_done_and_ignores_false_done(self):
        f = self.fake
        f.add_agent("w1:p1", "working", session="keep")
        f.add_agent("w1:p2", "idle", session="plain")
        self.event("w1:p1")
        f.set_status("w1:p1", "idle")
        self.event("w1:p1")
        self.assertEqual(self.attn("w1:p1"), "done")
        self.assertEqual(self.attn("w1:p2"), "idle")

        # Cold restart: a new server with no tokens or view, agents not back yet.
        self.fake.close()
        self.fake = FakeHerdr()
        knobs = {
            "HERDR_ATTENTION_QUEUE_RESTORE_S": "30",
            "HERDR_ATTENTION_QUEUE_SETTLE_S": "30",
            "HERDR_ATTENTION_QUEUE_RESEED_SCHEDULE": ",".join(["0.2"] * 25),
        }
        self.run_hook("startup", **knobs)
        self.assertIsNotNone(self.fake.view)
        self.assertEqual(self.fake.view["sort"], model.VIEW_SORT)
        self.assertEqual(self.fake.view["source"], "plugin:attention-queue")

        # Agents relaunch after the startup hook, with new terminal ids and no
        # events; herdr falsely reports one of them done (herdr issue #3990).
        self.fake.add_agent("w1:p1", "idle", terminal_id="new-1", session="keep")
        self.fake.add_agent("w1:p2", "done", terminal_id="new-2", session="plain")

        self.assertTrue(
            self.wait_for(lambda: self.attn("w1:p1") and self.attn("w1:p2")),
            "reseed never wrote tokens",
        )
        self.assertEqual(self.attn("w1:p1"), "done")
        self.assertEqual(self.attn("w1:p2"), "idle")
        self.assertEqual(self.fake.violations, [])


class LifecycleTest(HookTestCase):
    def test_reapply_and_clear(self):
        self.fake.add_agent("w1:p1", "working")
        self.run_hook("action", "reapply")
        self.assertEqual(self.fake.view["label"], model.VIEW_LABEL)
        self.assertEqual(self.attn("w1:p1"), "working")

        self.run_hook("action", "clear")
        self.assertEqual(self.fake.tokens("w1:p1"), {})
        self.assertIsNone(self.fake.view)

    def test_missing_socket_exits_cleanly(self):
        proc = subprocess.run(
            self.argv("event"),
            env=self.env(HERDR_SOCKET_PATH="/tmp/aq-no-such-dir/herdr.sock"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_closed_agent_is_forgotten(self):
        f = self.fake
        f.add_agent("w1:p1", "working", session="gone")
        self.event("w1:p1")
        f.set_status("w1:p1", "idle")
        self.event("w1:p1")
        f.remove_agent("w1:p1")
        self.run_hook("event", event={"event": "pane_closed", "data": {"pane_id": "w1:p1"}})
        with open(os.path.join(self.state, "sessions", "default", "state.json")) as fh:
            state = json.load(fh)
        self.assertEqual(state["live"], {})
        self.assertIsNotNone(state["sessions"]["claude:id:gone"]["closed_ns"])


if __name__ == "__main__":
    unittest.main()


class UsageAndNamesTest(HookTestCase):
    def test_usage_reaches_every_agent_row_and_claude_rows_get_session_names(self):
        claude = tempfile.mkdtemp(prefix="aq-claude-", dir="/tmp")
        self.addCleanup(shutil.rmtree, claude, True)
        os.makedirs(os.path.join(claude, "jobs", "abcdef12"))
        with open(os.path.join(claude, "jobs", "abcdef12", "state.json"), "w") as f:
            json.dump({"name": "cache layer"}, f)
        with open(os.path.join(self.state, "usage.json"), "w") as f:
            json.dump({"five_hour": {"utilization": 40}}, f)
        self.write_config(usage=True, claude_names=True)
        self.fake.add_agent("w1:p1", "working", session="abcdef12-0000-4000-8000-000000000000")
        self.fake.add_agent("w1:p2", "idle", session="/tmp/pi.jsonl", agent="pi")
        self.run_hook("event", event=self.status_event("w1:p1"), CLAUDE_CONFIG_DIR=claude)
        self.assertEqual(self.fake.tokens("w1:p1").get("usage"), usage.GAUGE["ok"] + " 5h 40%")
        self.assertEqual(self.fake.tokens("w1:p2").get("usage"), usage.GAUGE["ok"] + " 5h 40%")
        self.assertEqual(self.fake.agents["w1:p1"].get("title"), "cache layer")
        self.assertEqual(self.fake.agents["w1:p1"].get("display_agent"), "Claude: cache layer")
        self.assertNotIn("title", self.fake.agents["w1:p2"])
        # Unchanged text and names are not reported again.
        reports = len([c for c in self.fake.calls if c[0] == "pane.report_metadata"])
        self.run_hook("event", event=self.status_event("w1:p2"), CLAUDE_CONFIG_DIR=claude)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "pane.report_metadata"]), reports)
        # clear removes the usage token and the names.
        self.run_hook("action", "clear", CLAUDE_CONFIG_DIR=claude)
        self.assertNotIn("usage", self.fake.tokens("w1:p1"))
        self.assertNotIn("title", self.fake.agents["w1:p1"])
        self.assertEqual(self.fake.violations, [])

    def test_a_stale_cache_refreshes_in_the_background_and_pushes_the_new_text(self):
        reply = os.path.join(self.state, "reply.json")
        with open(reply, "w") as f:
            json.dump({"seven_day": {"utilization": 77}}, f)
        self.write_config(usage=True, claude_names=False)
        self.fake.add_agent("w1:p1", "idle")
        extra = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "HERDR_ATTENTION_QUEUE_USAGE_URL": "file://" + reply}
        self.run_hook("event", event=self.status_event("w1:p1"), **extra)
        deadline = time.time() + 10
        while time.time() < deadline and self.fake.tokens("w1:p1").get("usage") != usage.GAUGE["warn"] + " 7d 77%":
            time.sleep(0.05)
        self.assertEqual(self.fake.tokens("w1:p1").get("usage"), usage.GAUGE["warn"] + " 7d 77%")
        self.assertFalse(os.path.exists(os.path.join(self.state, "usage.lock")))
