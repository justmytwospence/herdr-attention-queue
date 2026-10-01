"""The turn-ending question judge and the harness commands."""

import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from attention_queue import ask
from tests.test_hooks_e2e import HookTestCase


class FakeJev:
    """A local stand-in for the TypeSafe API: answers `asks` with a set probability."""

    def __init__(self):
        self.p = 0.95
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.headers.get("Authorization"), body))
                reply = json.dumps({"answers": {"asks": {"type": "noul", "noul": outer.p}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1/systemone" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FinalMessageTest(unittest.TestCase):
    def test_payload_shapes(self):
        self.assertEqual(ask.final_message({"message": "Shall I?"}), "Shall I?")
        self.assertEqual(ask.final_message({"last_assistant_message": " Go? "}), "Go?")
        self.assertEqual(ask.final_message({"message": [{"type": "text", "text": "A"}, {"type": "image"}]}), "A")
        self.assertEqual(ask.final_message("plain text"), "plain text")
        self.assertIsNone(ask.final_message({}))
        self.assertIsNone(ask.final_message(None))

    def test_claude_transcript_last_assistant_text(self):
        d = tempfile.mkdtemp(prefix="aq-tr-", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "t.jsonl")
        lines = [
            {"type": "user", "message": {"content": "do it"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it."}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            "not json",
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done. Want me to push?"}]}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "x"}]}},
        ]
        with open(path, "w") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        self.assertEqual(ask.final_message({"transcript_path": path}), "Done. Want me to push?")
        self.assertIsNone(ask.final_message({"transcript_path": path + ".missing"}))


class JudgeTest(unittest.TestCase):
    def setUp(self):
        self.jev = FakeJev()
        self.addCleanup(self.jev.close)
        self.config = tempfile.mkdtemp(prefix="aq-askcfg-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.config, True)
        env = {"TYPESAFE_API_KEY": "k", "HERDR_ATTENTION_QUEUE_JEV_URL": self.jev.url,
               "HERDR_PLUGIN_CONFIG_DIR": self.config}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_threshold_and_request(self):
        self.assertEqual(ask.check({"message": "x" * 5000 + "Want me to push?"}), {"asks": True, "p": 0.95})
        auth, body = self.jev.requests[-1]
        self.assertEqual(auth, "Bearer k")
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(len(body["state"]["message"]), ask.MAX_CHARS)
        self.assertTrue(body["state"]["message"].endswith("Want me to push?"))
        self.jev.p = 0.5
        self.assertEqual(ask.check({"message": "Pushed."})["asks"], False)

    def test_config_and_failures_answer_does_not_ask(self):
        with open(os.path.join(self.config, "config.json"), "w") as f:
            json.dump({"ask_threshold": 0.4}, f)
        self.jev.p = 0.5
        self.assertTrue(ask.check({"message": "Go?"})["asks"])
        with open(os.path.join(self.config, "config.json"), "w") as f:
            json.dump({"ask_judge": False}, f)
        self.assertEqual(ask.check({"message": "Go?"})["reason"], "ask_judge is off")
        os.remove(os.path.join(self.config, "config.json"))
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            self.assertEqual(ask.check({"message": "Go?"})["reason"], "Jev unavailable")
        with patch.dict(os.environ, {"HERDR_ATTENTION_QUEUE_JEV_URL": "http://127.0.0.1:9/none"}):
            self.assertEqual(ask.check({"message": "Go?"})["reason"], "Jev unavailable")
        self.assertEqual(ask.check({})["reason"], "no final message")


class HarnessCommandTest(HookTestCase):
    def setUp(self):
        super().setUp()
        self.jev = FakeJev()
        self.addCleanup(self.jev.close)
        self.fake.add_agent("w1:p1", "idle", agent="claude", session="c")

    def hook(self, *args, stdin="{}", **extra):
        import subprocess

        settings = dict(HERDR_ENV="1", HERDR_PANE_ID="w1:p1", TYPESAFE_API_KEY="k",
                        HERDR_ATTENTION_QUEUE_JEV_URL=self.jev.url)
        settings.update(extra)
        env = self.env(**settings)
        proc = subprocess.run(self.argv(*args), env=env, input=stdin, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_activity_sets_and_clears_the_token(self):
        self.hook("activity", "blocked")
        self.assertEqual(self.fake.tokens("w1:p1").get("activity"), "blocked")
        self.hook("activity", "clear")
        self.assertNotIn("activity", self.fake.tokens("w1:p1"))
        self.hook("activity", "bogus")
        self.hook("activity", "working", HERDR_ENV="")  # outside herdr: nothing
        self.assertNotIn("activity", self.fake.tokens("w1:p1"))
        self.assertEqual(self.fake.violations, [])

    def test_ask_check_reports_a_question_or_the_else_state(self):
        out = self.hook("ask-check", "--report", "--else", "idle", stdin=json.dumps({"last_assistant_message": "Go?"}))
        self.assertEqual(json.loads(out), {"asks": True, "p": 0.95})
        self.assertEqual(self.fake.tokens("w1:p1").get("activity"), "blocked")
        self.jev.p = 0.1
        self.hook("ask-check", "--report", "--else", "idle", stdin=json.dumps({"message": "Pushed."}))
        self.assertEqual(self.fake.tokens("w1:p1").get("activity"), "idle")
        self.hook("ask-check", "--report", stdin=json.dumps({"message": "Pushed."}))
        self.assertEqual(self.fake.tokens("w1:p1").get("activity"), "idle", "no --else: unchanged")
        self.assertEqual(self.fake.violations, [])

    def test_a_question_turns_a_finished_turn_into_blocked(self):
        self.fake.set_status("w1:p1", "working")
        self.event("w1:p1")
        self.fake.set_status("w1:p1", "idle")
        self.hook("ask-check", "--report", "--else", "clear", stdin=json.dumps({"message": "Shall I push?"}))
        self.run_hook("action", "refresh")
        self.assertEqual(self.attn("w1:p1"), "blocked")
        self.hook("activity", "clear")  # the user answered (UserPromptSubmit)
        self.fake.set_status("w1:p1", "working")
        self.run_hook("action", "refresh")
        self.assertEqual(self.attn("w1:p1"), "working")
