import os
import unittest

import attention_queue
from attention_queue import hooks

try:
    import tomllib
except ImportError:  # Python < 3.11; the plugin itself never parses TOML.
    tomllib = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# herdr 0.9.0 PLUGIN_HOOK_EVENT_KINDS (src/api/schema/events.rs).
HOOKABLE = {
    "workspace.created", "workspace.updated", "workspace.closed", "workspace.renamed",
    "workspace.moved", "workspace.reordered", "workspace.focused",
    "worktree.created", "worktree.opened", "worktree.removed",
    "tab.created", "tab.closed", "tab.renamed", "tab.moved", "tab.focused",
    "pane.created", "pane.closed", "pane.focused", "pane.moved", "pane.exited",
    "pane.agent_detected", "pane.agent_status_changed",
}
CONTEXTS = {"global", "workspace", "tab", "pane", "selection"}


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class ManifestTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "herdr-plugin.toml"), "rb") as f:
            self.manifest = tomllib.load(f)

    def test_required_fields(self):
        for key in ("id", "name", "version", "min_herdr_version"):
            self.assertIn(key, self.manifest)
        self.assertEqual(self.manifest["id"], "attention-queue")
        self.assertEqual(self.manifest["version"], attention_queue.__version__)

    def test_actions_match_the_dispatcher(self):
        ids = [a["id"] for a in self.manifest["actions"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), set(hooks.ACTIONS))
        for action in self.manifest["actions"]:
            self.assertNotIn(".", action["id"])
            self.assertTrue(set(action.get("contexts", [])) <= CONTEXTS)
            self.assertEqual(action["command"][-2:], ["action", action["id"]])

    def test_events_are_hookable(self):
        for event in self.manifest["events"]:
            self.assertIn(event["on"], HOOKABLE)

    def test_commands_run_the_entrypoint_without_bytecode(self):
        commands = [s["command"] for s in self.manifest["startup"]]
        commands += [e["command"] for e in self.manifest["events"]]
        commands += [a["command"] for a in self.manifest["actions"]]
        for command in commands:
            self.assertEqual(command[:3], ["python3", "-B", "attention.py"])
        self.assertTrue(os.path.exists(os.path.join(ROOT, "attention.py")))


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class ExampleConfigTest(unittest.TestCase):
    def test_example_rules_cover_every_state_icon(self):
        from attention_queue import model

        with open(os.path.join(ROOT, "examples", "config.toml"), "rb") as f:
            example = tomllib.load(f)
        row = example["ui"]["sidebar"]["agents"]["rows"][0]
        icon = next(t for t in row if isinstance(t, dict) and t.get("token") == "$attn_row")
        self.assertEqual({r["starts_with"] for r in icon["rules"]}, set(model.ICON.values()))
        self.assertTrue(example["ui"]["window_title"].startswith("herdr "))


if __name__ == "__main__":
    unittest.main()
