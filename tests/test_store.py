import json
import os
import shutil
import tempfile
import unittest

from attention_queue import store

S = 10**9


class SessionNameTest(unittest.TestCase):
    def test_names(self):
        cases = {
            "/Users/x/.config/herdr/herdr.sock": "default",
            "/home/s/.config/herdr/sessions/homelab/herdr.sock": "homelab",
            "/tmp/aq-123/herdr.sock": "default",
            "/home/s/.config/herdr/sessions/../herdr.sock": "default",
            "/home/s/.config/herdr/sessions/bad name/herdr.sock": "default",
        }
        for path, name in cases.items():
            with self.subTest(path=path):
                self.assertEqual(store.session_name(path), name)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aq-store-")
        self.store = store.Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_missing_state_is_empty(self):
        self.assertEqual(self.store.load(), store.empty_state())

    def test_round_trip(self):
        state = store.empty_state()
        state["live"]["t1"] = {"attn": "done"}
        state["report_seq"] = 42
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

    def test_corrupt_state_is_set_aside(self):
        with open(self.store.state_path, "w") as f:
            f.write("{nope")
        self.assertEqual(self.store.load(), store.empty_state())
        self.assertTrue(any(n.startswith("state.json.corrupt-") for n in os.listdir(self.dir)))

    def test_unknown_version_is_set_aside(self):
        with open(self.store.state_path, "w") as f:
            json.dump({"version": 99}, f)
        self.assertEqual(self.store.load(), store.empty_state())

    def test_save_if_changed(self):
        state = store.empty_state()
        before = self.store.dumps(state)
        self.assertFalse(self.store.save_if_changed(state, before))
        self.assertFalse(os.path.exists(self.store.state_path))
        state["report_seq"] = 1
        self.assertTrue(self.store.save_if_changed(state, before))
        self.assertTrue(os.path.exists(self.store.state_path))

    def test_lock_times_out_while_held(self):
        with self.store.locked():
            with self.assertRaises(store.LockTimeout):
                with self.store.locked(timeout=0.1):
                    pass
        with self.store.locked(timeout=0.1):
            pass

    def test_try_lock(self):
        path = os.path.join(self.dir, "reseed.lock")
        fd = store.try_lock(path)
        self.assertIsNotNone(fd)
        self.assertIsNone(store.try_lock(path))
        os.close(fd)
        fd = store.try_lock(path)
        self.assertIsNotNone(fd)
        os.close(fd)


class GcTest(unittest.TestCase):
    def test_old_sessions_dropped_unless_live(self):
        now = 10**18
        old = now - store.GC_MAX_AGE_NS - S
        state = store.empty_state()
        state["sessions"] = {"old": {"seen_ns": old}, "old-live": {"seen_ns": old}, "new": {"seen_ns": now}}
        state["live"] = {"t1": {"session": "old-live"}}
        store.gc_sessions(state, now)
        self.assertEqual(set(state["sessions"]), {"old-live", "new"})

    def test_cap_drops_oldest(self):
        now = 10**18
        state = store.empty_state()
        state["sessions"] = {"s%d" % i: {"seen_ns": now - i} for i in range(store.GC_MAX_SESSIONS + 3)}
        store.gc_sessions(state, now)
        self.assertEqual(len(state["sessions"]), store.GC_MAX_SESSIONS)
        self.assertIn("s0", state["sessions"])
        self.assertNotIn("s%d" % (store.GC_MAX_SESSIONS + 2), state["sessions"])


if __name__ == "__main__":
    unittest.main()
