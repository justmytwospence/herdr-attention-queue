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


class RuleTest(unittest.TestCase):
    ASKS = [
        "Tests pass.\n\nWant me to push the branch now?",
        "Two options:\n1. Ship it\n2. Wait\n\n**Which do you prefer?**",
        "Shall I go ahead with step 2? It takes about an hour.",
        "Nothing is committed. Say go and I'll start.",
        "If that trade-off is acceptable, say go and I'll start with the split.",
        "The re-baseline costs about $4 per persona, so I'd like your go-ahead before starting it.",
        "Orca's hook edits are still waiting on your decision.",
        "Tell me if you want this committed separately or together with those.",
        "- Confirm the account id before I re-run the import.",
        "I'll keep going unless you'd rather I stop here.",
    ]
    DOES_NOT = [
        "Done. The plugin is pushed and the NUC is synced.",
        "I can look for an existing package if you'd like to compare.",
        "If you want it run, say so, or use the notebook.",
        "Say if you want it.",
        "If it still fails after that, tell me and I'll dig further.",
        "The dialog asks \"Do you want to proceed?\" and the rule matches it.",
        "Run `git status?` to check.",
        "## Why did it fail?\n\nThe cache was stale. Fixed and pushed.",
        "Old question?\n\n" + "Then a long report. " * 60 + "\n\nAll done.",
        "```sh\nread -p 'Continue?' x\n```\n\nThe script is installed.",
    ]

    def test_questions_and_requests(self):
        for text in self.ASKS:
            with self.subTest(text=text):
                self.assertTrue(ask.rule_asks(text))

    def test_reports_offers_and_quoted_questions(self):
        for text in self.DOES_NOT:
            with self.subTest(text=text):
                self.assertFalse(ask.rule_asks(text))


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
        self.assertEqual(ask.check({"message": "x" * 5000 + "Want me to push?"}), {"asks": True, "by": "jev", "p": 0.95})
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
        # Without Jev the rule decides.
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            self.assertEqual(ask.check({"message": "Go?"}), {"asks": True, "by": "rule", "p": None, "reason": "Jev unavailable"})
            self.assertFalse(ask.check({"message": "Pushed."})["asks"])
        with patch.dict(os.environ, {"HERDR_ATTENTION_QUEUE_JEV_URL": "http://127.0.0.1:9/none"}):
            self.assertEqual(ask.check({"message": "Go?"})["by"], "rule")
            with open(os.path.join(self.config, "config.json"), "w") as f:
                json.dump({"ask_fallback": False}, f)
            self.assertEqual(ask.check({"message": "Go?"}), {"asks": False, "by": None, "p": None, "reason": "Jev unavailable"})
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
        self.assertEqual(json.loads(out), {"asks": True, "by": "jev", "p": 0.95})
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

    def test_without_a_key_the_rule_still_marks_questions(self):
        out = self.hook("ask-check", "--report", "--else", "clear", TYPESAFE_API_KEY="",
                        stdin=json.dumps({"message": "Tests pass. Want me to push?"}))
        self.assertEqual(json.loads(out)["by"], "rule")
        self.assertEqual(self.fake.tokens("w1:p1").get("activity"), "blocked")
