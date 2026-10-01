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


OK, WARN, CRIT = usage.GAUGE["ok"], usage.GAUGE["warn"], usage.GAUGE["critical"]


def iso(offset_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + offset_s))


def limit(kind, percent, resets_in, **extra):
    return dict({"kind": kind, "percent": percent, "resets_at": iso(resets_in)}, **extra)


H = 3600
D = 86400


class RenderTest(unittest.TestCase):
    def test_shows_only_the_window_on_the_worst_pace(self):
        # Neither has a usable pace yet (a day into the week; the block's reset
        # is further off than a block lasts), so the higher use shows.
        self.assertEqual(usage.render(SAMPLE, NOW), OK + " 5h 38% 7h55m")
        data = {"limits": [limit("session", 20, 4 * H), limit("weekly_all", 60, 3 * D)]}
        # 60% after 4 of 7 days projects to 105%: the week is the bottleneck.
        self.assertEqual(usage.render(data, NOW), WARN + " 7d 60% 3d")

    def test_levels(self):
        cases = [
            # 30% two hours into five: on pace for 75%, fine.
            ([limit("session", 30, 3 * H)], OK + " 5h 30% 3h0m"),
            # 45% halfway through: on pace for 90%.
            ([limit("session", 45, 150 * 60)], OK + " 5h 45% 2h30m"),
            # A fast start below 30% is not a problem yet.
            ([limit("session", 25, 4 * H)], OK + " 5h 25% 4h0m"),
            # 60% after 2h: hits the cap in 1h20m, before the reset.
            ([limit("session", 60, 3 * H)], WARN + " 5h 60% 3h0m"),
            # 70% after 1.5h: hits the cap in under 40 minutes.
            ([limit("session", 70, 210 * 60)], CRIT + " 5h 70% 3h30m"),
            ([limit("weekly_all", 80, 10 * 60)], WARN + " 7d 80% 10m"),
            ([limit("weekly_all", 92, 10 * 60)], CRIT + " 7d 92% 10m"),
            ([limit("weekly_all", 100, 2 * D)], CRIT + " 7d 100% 2d"),
            # The API's own severity is a floor.
            ([limit("weekly_all", 10, 2 * D, severity="warning")], WARN + " 7d 10% 2d"),
            ([limit("weekly_all", 10, 2 * D, severity="limit_reached")], CRIT + " 7d 10% 2d"),
        ]
        for limits, text in cases:
            with self.subTest(text=text):
                self.assertEqual(usage.render({"limits": limits}, NOW), text)

    def test_a_fresh_window_is_not_judged_by_its_pace(self):
        # 8% in the first five minutes "projects" to 480%; too early to say.
        data = {"limits": [limit("session", 8, 5 * H - 300), limit("weekly_all", 20, 5 * D)]}
        self.assertEqual(usage.render(data, NOW), OK + " 7d 20% 5d")

    def test_level_beats_projection_and_model_caps_count(self):
        data = {"limits": [
            limit("weekly_all", 40, 4 * D),
            limit("weekly_scoped", 91, 4 * D, scope={"model": {"display_name": "Fable"}}),
        ]}
        self.assertEqual(usage.render(data, NOW), CRIT + " Fable 91% 4d")

    def test_spend_shows_only_when_it_is_the_problem(self):
        spend = {"percent": 24, "used": {"amount_minor": 3583}, "limit": {"amount_minor": 15000}}
        data = {"limits": [limit("session", 5, 4 * H)], "spend": spend, "extra_usage": {"is_enabled": True}}
        self.assertEqual(usage.render(data, NOW), OK + " 5h 5% 4h0m")
        data["spend"] = dict(spend, percent=95, used={"amount_minor": 14250})
        self.assertEqual(usage.render(data, NOW), CRIT + " extra $142.50/$150")
        data["extra_usage"] = {"is_enabled": False}
        self.assertEqual(usage.render(data, NOW), OK + " 5h 5% 4h0m")
        self.assertEqual(usage.render({"spend": {"percent": 80}}, NOW), WARN + " extra 80%")

    def test_older_replies_without_limits(self):
        self.assertEqual(usage.render({"five_hour": {"utilization": 3}}, NOW), OK + " 5h 3%")
        self.assertEqual(usage.render({"seven_day": {"utilization": 77.4}}, NOW), WARN + " 7d 77%")
        self.assertIsNone(usage.render({}, NOW))
        self.assertIsNone(usage.render(None, NOW))
        self.assertIsNone(usage.render({"limits": [{"kind": "other", "percent": 99}]}, NOW))

    def test_until(self):
        self.assertEqual(usage.until("2026-09-29T12:30:00Z", NOW), "30m")
        self.assertEqual(usage.until("2026-09-29T15:05:00Z", NOW), "3h5m")
        self.assertEqual(usage.until("2026-10-02T12:00:00Z", NOW), "3d")
        self.assertEqual(usage.until("2026-10-02T12:00:00.5+00:00", NOW), "3d")
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
        self.assertEqual(usage.current(self.root, lambda: started.append(1)), usage.GAUGE["ok"] + " 5h 50%")
        self.assertEqual(usage.current(self.root, lambda: started.append(1)), usage.GAUGE["ok"] + " 5h 50%")
        self.assertEqual(started, [1], "the lock admits one refresh at a time")
        usage.release_refresh(self.root)
        usage.current(self.root, lambda: started.append(2))
        self.assertEqual(started, [1, 2])

    def test_a_failed_fetch_backs_off_for_a_full_ttl(self):
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "HERDR_ATTENTION_QUEUE_USAGE_URL": "file:///nonexistent/usage.json"}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            self.assertFalse(usage.refresh(self.root))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertIsNone(usage.current(self.root, lambda: self.fail("retried during backoff")))
        old = time.time() - usage.TTL_S - 1
        os.utime(usage.backoff_path(self.root), (old, old))
        started = []
        usage.current(self.root, lambda: started.append(1))
        self.assertEqual(started, [1])

    def test_a_fresh_cache_starts_nothing(self):
        with open(usage.cache_path(self.root), "w") as f:
            json.dump({"seven_day": {"utilization": 1}}, f)
        self.assertEqual(usage.current(self.root, lambda: self.fail("refreshed")), usage.GAUGE["ok"] + " 7d 1%")

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
