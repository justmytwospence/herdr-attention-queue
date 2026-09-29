"""Usage gauges and Claude session names."""

import json
import os
import shutil
import tempfile
import time
import unittest
import calendar

from attention_queue import labels, usage

NOW = calendar.timegm((2026, 9, 29, 12, 0, 0))

SAMPLE = {
    "five_hour": {"utilization": 38.6, "resets_at": "2026-09-29T19:55:00.123Z"},
    "seven_day": {"utilization": 15.2, "resets_at": "2026-10-05T13:00:00Z"},
    "limits": [
        {"kind": "weekly_scoped", "percent": 8, "scope": {"model": {"display_name": "Fable"}}},
        {"kind": "other", "percent": 99},
    ],
    "spend": {"percent": 103, "used": {"amount_minor": 15421}, "limit": {"amount_minor": 15000}},
    "extra_usage": {"is_enabled": False},
}


class RenderTest(unittest.TestCase):
    def test_render_matches_the_status_line_segments(self):
        self.assertEqual(
            usage.render(SAMPLE, NOW),
            "\U000F0954 38% 7h55m \U000F00ED 15% 6d \U000F0068 8% \U000F0114 $154.21/$150 off",
        )

    def test_render_skips_missing_segments(self):
        self.assertEqual(usage.render({"five_hour": {"utilization": 3}}, NOW), "\U000F0954 3%")
        self.assertIsNone(usage.render({}, NOW))
        self.assertIsNone(usage.render(None, NOW))
        spend = {"spend": {"percent": 12}, "extra_usage": {"is_enabled": True}}
        self.assertEqual(usage.render(spend, NOW), "\U000F0114 12%")

    def test_until(self):
        self.assertEqual(usage.until("2026-09-29T12:30:00Z", NOW), "30m")
        self.assertEqual(usage.until("2026-09-29T15:05:00Z", NOW), "3h5m")
        self.assertEqual(usage.until("2026-10-02T12:00:00Z", NOW), "3d")
        self.assertEqual(usage.until("2026-09-29T11:00:00Z", NOW), "")
        self.assertEqual(usage.until("garbage", NOW), "")
        self.assertEqual(usage.until(None, NOW), "")


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aq-usage-", dir="/tmp")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_stale_cache_starts_one_refresh_and_still_renders(self):
        with open(usage.cache_path(self.root), "w") as f:
            json.dump({"five_hour": {"utilization": 50}}, f)
        old = time.time() - usage.TTL_S - 10
        os.utime(usage.cache_path(self.root), (old, old))
        started = []
        self.assertEqual(usage.current(self.root, lambda: started.append(1)), "\U000F0954 50%")
        self.assertEqual(usage.current(self.root, lambda: started.append(1)), "\U000F0954 50%")
        self.assertEqual(started, [1], "the lock admits one refresh at a time")
        usage.release_refresh(self.root)
        usage.current(self.root, lambda: started.append(2))
        self.assertEqual(started, [1, 2])

    def test_a_fresh_cache_starts_nothing(self):
        with open(usage.cache_path(self.root), "w") as f:
            json.dump({"seven_day": {"utilization": 1}}, f)
        self.assertEqual(usage.current(self.root, lambda: self.fail("refreshed")), "\U000F00ED 1%")

    def test_refresh_fetches_with_the_claude_token_and_caches(self):
        reply = os.path.join(self.root, "reply.json")
        with open(reply, "w") as f:
            json.dump(SAMPLE, f)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "HERDR_ATTENTION_QUEUE_USAGE_URL": "file://" + reply}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            self.assertTrue(usage.refresh(self.root))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        data, age = usage.read_cache(self.root)
        self.assertEqual(data, SAMPLE)
        self.assertLess(age, 5)

    def test_no_token_means_no_fetch(self):
        env = {"CLAUDE_CONFIG_DIR": self.root}
        saved = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")}
        os.environ.update(env)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        try:
            if os.uname().sysname != "Darwin":
                self.assertIsNone(usage.access_token())
                self.assertFalse(usage.refresh(self.root))
            with open(os.path.join(self.root, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {"accessToken": "from-file"}}, f)
            if os.uname().sysname != "Darwin":
                self.assertEqual(usage.access_token(), "from-file")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class ClaudeNameTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aq-claude-", dir="/tmp")
        os.makedirs(os.path.join(self.dir, "jobs", "e65109c0"))
        with open(os.path.join(self.dir, "jobs", "e65109c0", "state.json"), "w") as f:
            json.dump({"name": "  matchmaker\nsession recovery ", "state": "working"}, f)
        self.saved = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.dir

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_names_come_from_claude_jobs(self):
        key = "claude:id:e65109c0-5928-48c8-91f9-46848b2a9cd8"
        self.assertEqual(labels.claude_name(key), "matchmaker session recovery")
        self.assertIsNone(labels.claude_name("claude:id:00000000-0000"))
        self.assertIsNone(labels.claude_name("pi:path:/x"))
        self.assertIsNone(labels.claude_name("claude:id:../../etc"))
        self.assertIsNone(labels.claude_name(None))


if __name__ == "__main__":
    unittest.main()
