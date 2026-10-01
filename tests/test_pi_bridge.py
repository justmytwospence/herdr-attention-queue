"""Installing the pi bridge into pi's extensions directory."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from attention_queue import pi_bridge
from tests.test_hooks_e2e import ROOT, HookTestCase


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aq-pi-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.agent = os.path.join(self.dir, "agent")
        self.config = os.path.join(self.dir, "config")
        os.makedirs(self.config)
        patcher = patch.dict(os.environ, {"PI_CODING_AGENT_DIR": self.agent, "HERDR_PLUGIN_CONFIG_DIR": self.config})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self.agent, "extensions", pi_bridge.NAME)

    def read(self):
        with open(self.path) as f:
            return f.read()

    def test_installs_only_where_pi_is_set_up(self):
        self.assertEqual(pi_bridge.ensure(ROOT), "no pi")
        os.makedirs(self.agent)
        self.assertEqual(pi_bridge.ensure(ROOT), "installed")
        content = self.read()
        self.assertTrue(content.startswith(pi_bridge.MARKER))
        self.assertIn('const PLUGIN_ROOT = "%s";' % ROOT, content)
        self.assertNotIn(pi_bridge.PLACEHOLDER, content)
        self.assertEqual(pi_bridge.ensure(ROOT), "current")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)

    def test_updates_its_own_file_and_never_touches_a_foreign_one(self):
        os.makedirs(os.path.join(self.agent, "extensions"))
        with open(self.path, "w") as f:
            f.write(pi_bridge.MARKER + " old version\n")
        self.assertEqual(pi_bridge.ensure(ROOT), "updated")
        with open(self.path, "w") as f:
            f.write("// my own extension\n")
        self.assertEqual(pi_bridge.ensure(ROOT), "foreign file")
        self.assertEqual(self.read(), "// my own extension\n")

    def test_config_switch_removes_it(self):
        os.makedirs(self.agent)
        pi_bridge.ensure(ROOT)
        with open(os.path.join(self.config, "config.json"), "w") as f:
            json.dump({"pi_bridge": False}, f)
        self.assertEqual(pi_bridge.ensure(ROOT), "removed")
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(pi_bridge.ensure(ROOT), "off")


class HookInstallTest(HookTestCase):
    def test_startup_and_events_install_the_bridge(self):
        agent = os.path.join(self.state, "pi-agent")
        os.makedirs(agent)
        self.fake.add_agent("w1:p1", "idle", agent="pi")
        self.run_hook("event", event=self.status_event("w1:p1"))
        installed = os.path.join(agent, "extensions", pi_bridge.NAME)
        self.assertTrue(os.path.exists(installed))
        os.unlink(installed)
        self.run_hook("startup")
        self.assertTrue(os.path.exists(installed))
