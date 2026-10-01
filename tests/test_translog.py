import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from attention_queue import translog
from tests.test_hooks_e2e import ENTRY, HookTestCase


def lines(directory):
    try:
        with open(translog.path(directory)) as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


class AppendTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aq-log-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_append_and_rotate(self):
        translog.append(self.dir, [{"ts_ms": 1, "n": 1}], max_bytes=40)
        translog.append(self.dir, [{"ts_ms": 2, "n": 2}, {"ts_ms": 3, "n": 3}], max_bytes=40)
        # Over the limit now: the next append rotates first.
        translog.append(self.dir, [{"ts_ms": 4, "n": 4}], max_bytes=40)
        with open(translog.path(self.dir) + ".1") as f:
            self.assertEqual([json.loads(x)["n"] for x in f], [1, 2, 3])
        self.assertEqual([x["n"] for x in lines(self.dir)], [4])

    def test_empty_append_creates_nothing(self):
        translog.append(self.dir, [])
        self.assertFalse(os.path.exists(translog.path(self.dir)))


class FollowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aq-log-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def start(self, since_ms, heartbeat_s=0):
        out = io.StringIO()
        done = threading.Event()
        thread = threading.Thread(
            target=translog.follow,
            args=(self.dir, since_ms),
            kwargs={"out": out, "poll_s": 0.01, "stop": done.is_set, "heartbeat_s": heartbeat_s},
            daemon=True,
        )
        thread.start()
        self.addCleanup(done.set)
        return out, done

    def seen(self, out):
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.01)
        return predicate()

    def test_since_reads_the_rotated_file_then_follows_across_rotation(self):
        translog.append(self.dir, [{"ts_ms": 1}, {"ts_ms": 2}], max_bytes=10**6)
        os.replace(translog.path(self.dir), translog.path(self.dir) + ".1")
        translog.append(self.dir, [{"ts_ms": 3}])
        out, _ = self.start(since_ms=1)
        self.assertTrue(self.wait(lambda: len(self.seen(out)) == 2))
        self.assertEqual([x["ts_ms"] for x in self.seen(out)], [2, 3])

        translog.append(self.dir, [{"ts_ms": 4}])
        # Rotation while following: the tail of the old file and the new file.
        os.replace(translog.path(self.dir), translog.path(self.dir) + ".1")
        translog.append(self.dir, [{"ts_ms": 5}])
        self.assertTrue(self.wait(lambda: len(self.seen(out)) == 4))
        self.assertEqual([x["ts_ms"] for x in self.seen(out)], [2, 3, 4, 5])

    def test_waits_for_a_log_that_does_not_exist_yet(self):
        out, _ = self.start(since_ms=0)
        time.sleep(0.05)
        translog.append(self.dir, [{"ts_ms": 7}])
        self.assertTrue(self.wait(lambda: [x["ts_ms"] for x in self.seen(out)] == [7]))

    def test_commands_on_input_are_answered_between_log_lines(self):
        r, w = os.pipe()
        out = io.StringIO()
        done = threading.Event()
        self.addCleanup(done.set)
        seen = []

        def on_command(command):
            seen.append(command)
            return None if command.get("op") == "quiet" else {"kind": "pong", "req": command.get("req")}

        threading.Thread(
            target=translog.follow, args=(self.dir, 0),
            kwargs={"out": out, "poll_s": 0.01, "stop": done.is_set, "heartbeat_s": 0,
                    "commands": os.fdopen(r, "rb"), "on_command": on_command},
            daemon=True,
        ).start()
        os.write(w, b'{"op":"ping","req":"r1"}\nnot json\n{"op":"quiet"}\n{"op":"pi')
        os.write(w, b'ng","req":"r2"}\n')
        self.assertTrue(self.wait(lambda: [x.get("req") for x in self.seen(out)] == ["r1", "r2"]))
        self.assertEqual(len(seen), 3)
        translog.append(self.dir, [{"ts_ms": 9}])
        self.assertTrue(self.wait(lambda: any(x.get("ts_ms") == 9 for x in self.seen(out))))
        os.close(w)  # input closed: keep following the log
        translog.append(self.dir, [{"ts_ms": 10}])
        self.assertTrue(self.wait(lambda: any(x.get("ts_ms") == 10 for x in self.seen(out))))

    def test_follow_marks_the_log_as_read(self):
        self.assertFalse(translog.follower_alive(self.dir))
        self.start(since_ms=0)
        self.assertTrue(self.wait(lambda: translog.follower_alive(self.dir)))
        old = time.time() - translog.ALIVE_MAX_AGE_S - 1
        os.utime(os.path.join(self.dir, translog.ALIVE), (old, old))
        self.assertFalse(translog.follower_alive(self.dir))

    def test_heartbeat(self):
        out, _ = self.start(since_ms=0, heartbeat_s=0.05)
        self.assertTrue(self.wait(lambda: any(x["kind"] == "ping" for x in self.seen(out))))


class HookLogTest(HookTestCase):
    def log(self):
        return lines(os.path.join(self.state, "sessions", "default"))

    def test_hooks_log_every_attn_change(self):
        f = self.fake
        f.add_agent("w1:p1", "working")
        self.event("w1:p1")
        f.set_status("w1:p1", "blocked")
        self.event("w1:p1")
        f.set_status("w1:p1", "working")
        self.event("w1:p1")
        f.set_status("w1:p1", "idle")
        self.event("w1:p1")
        self.run_hook("action", "mark-reviewed", pane="w1:p1")
        log = [x for x in self.log() if x["kind"] == "attn"]
        self.assertEqual(
            [(x["prev"], x["attn"]) for x in log],
            [(None, "working"), ("working", "blocked"), ("blocked", "working"), ("working", "done"), ("done", "idle")],
        )
        first = log[0]
        for key in ("v", "ts_ms", "pane_id", "terminal_id", "workspace_id", "tab_id", "agent", "restoring"):
            self.assertIn(key, first)
        self.assertEqual((first["pane_id"], first["workspace_id"], first["tab_id"]), ("w1:p1", "w1", "w1:t1"))
        self.assertFalse(first["restoring"])
        # The workspace label is looked up only for blocked and done.
        self.assertEqual([x["workspace"] for x in log], [None, "ws w1", None, "ws w1", None])

    def test_unchanged_state_logs_nothing(self):
        self.fake.add_agent("w1:p1", "idle")
        self.event("w1:p1")
        self.event("w1:p1")
        self.assertEqual(len(self.log()), 1)

    def test_focus_hook_logs_a_focus_line_and_nothing_else(self):
        self.fake.add_agent("w1:p1", "idle")
        self.event("w1:p1")
        reports = self.fake.count("pane.report_metadata")
        self.run_hook("focus", event={"event": "pane_focused", "data": {"pane_id": "w1:p1"}})
        self.assertEqual(self.log()[-1]["kind"], "focus")
        self.assertEqual(self.log()[-1]["pane_id"], "w1:p1")
        self.assertEqual(self.fake.count("pane.report_metadata"), reports)

    def test_follow_command(self):
        self.fake.add_agent("w1:p1", "working")
        self.event("w1:p1")
        since = self.log()[0]["ts_ms"] - 1
        proc = subprocess.Popen(
            self.argv("follow", "--session", "default", "--since-ms", str(since)),
            env=self.env(),
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: (proc.kill(), proc.communicate()))
        first = json.loads(proc.stdout.readline())
        self.assertEqual((first["pane_id"], first["attn"]), ("w1:p1", "working"))

    def test_follow_rejects_bad_arguments(self):
        for args in (["follow"], ["follow", "--session", "../x"], ["follow", "--session"], ["follow", "--since-ms", "1"]):
            with self.subTest(args=args):
                proc = subprocess.run(self.argv(*args), env=self.env(), capture_output=True, timeout=30)
                self.assertEqual(proc.returncode, 2)
        self.assertTrue(os.path.exists(ENTRY))


if __name__ == "__main__":
    unittest.main()


class FollowEntrypointTest(HookTestCase):
    def test_follow_serves_list_focus_and_notice_over_its_pipe(self):
        import subprocess

        self.fake.add_agent("w1:p1", "done", session="a")
        self.fake.add_agent("w1:p2", "idle", session="b")
        self.run_hook("event", event=self.status_event("w1:p1"))
        proc = subprocess.Popen(
            self.argv("follow", "--session", "default", "--since-ms", "9999999999999"),
            env=self.env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )
        self.addCleanup(lambda: (proc.kill(), proc.communicate()))
        replies = {}
        for command in ({"op": "list", "req": "r1"}, {"op": "focus", "req": "r2", "pane_id": "w1:p2"},
                        {"op": "notice", "req": "r3", "text": "hi"}, {"op": "focus", "req": "r4", "pane_id": "nope"}):
            proc.stdin.write(json.dumps(command) + "\n")
            proc.stdin.flush()
            reply = json.loads(proc.stdout.readline())
            replies[reply["req"]] = reply
        listed = {a["pane_id"]: a["tokens"]["attn"] for a in replies["r1"]["agents"]}
        self.assertEqual(listed, {"w1:p1": "done", "w1:p2": "idle"})
        self.assertEqual(replies["r2"]["ok"], True)
        self.assertEqual(self.fake.focus[-1], "w1:p2")
        self.assertEqual(replies["r3"]["ok"], True)
        self.assertEqual(replies["r4"]["kind"], "error")
