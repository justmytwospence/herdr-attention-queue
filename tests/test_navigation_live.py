"""Opt-in isolated Herdr smoke: HERDR_NAVIGATION_LIVE=1 python3 -m unittest tests.test_navigation_live -v."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("HERDR_NAVIGATION_LIVE") == "1" and shutil.which("herdr"),
                     "set HERDR_NAVIGATION_LIVE=1 for isolated live-server smoke")
class LiveNavigationTest(unittest.TestCase):
    def test_cross_space_tab_split_and_zoom_focus(self):
        session = "aq-nav-" + uuid.uuid4().hex[:8]
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env["HERDR_DISABLE_SOUND"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        def cli(*args):
            result = subprocess.run(["herdr", "--session", session, *args], env=env,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)["result"] if result.stdout.strip() else {}
        with tempfile.TemporaryDirectory(prefix="aq-nav-test-") as directory:
            log = open(os.path.join(directory, "server.log"), "w+")
            server = subprocess.Popen(["herdr", "--session", session, "server"],
                                      env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            socket = Path.home() / ".config/herdr/sessions" / session / "herdr.sock"
            try:
                deadline = time.monotonic() + 15
                while not socket.exists() and server.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not socket.exists():
                    log.seek(0)
                    self.fail("test server failed: " + log.read())
                spaces = [cli("workspace", "create", "--label", name, "--cwd", directory, "--no-focus")
                          for name in ("nav-shell", "nav-blocked", "nav-done")]
                shell = spaces[0]["root_pane"]["pane_id"]
                blocked = spaces[1]["root_pane"]["pane_id"]
                tab = cli("tab", "create", "--workspace", spaces[2]["workspace"]["workspace_id"],
                          "--cwd", directory, "--no-focus")
                base = tab["root_pane"]["pane_id"]
                split = cli("pane", "split", base, "--direction", "right", "--no-focus")
                done = split["pane"]["pane_id"]
                cli("pane", "zoom", base, "--on")
                cli("pane", "report-agent", blocked, "--source", "user:nav-test", "--agent", "pi",
                    "--state", "blocked", "--agent-session-id", "blocked-test")
                cli("pane", "report-agent", done, "--source", "user:nav-test", "--agent", "pi",
                    "--state", "working", "--agent-session-id", "done-test")
                config = Path(directory) / "config"
                config.mkdir()
                (config / "config.json").write_text('{"usage":false,"claude_names":false}')
                action_env = dict(env, HERDR_SOCKET_PATH=str(socket), HERDR_PLUGIN_STATE_DIR=directory,
                                  HERDR_PLUGIN_CONFIG_DIR=str(config), HERDR_PLUGIN_ROOT=str(ROOT),
                                  HERDR_ATTENTION_QUEUE_SETTLE_S="0", HERDR_PANE_ID=shell)
                def action(name, anchor=shell):
                    action_env["HERDR_PANE_ID"] = anchor
                    p = subprocess.run([sys.executable, "-B", str(ROOT / "attention.py"), "action", name],
                                       env=action_env, text=True, capture_output=True, timeout=25)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                action("reapply")
                cli("pane", "report-agent", done, "--source", "user:nav-test", "--agent", "pi", "--state", "idle")
                action("next-attention")
                self.assertEqual(cli("pane", "layout", "--pane", blocked)["layout"]["focused_pane_id"], blocked)
                action("next-attention", blocked)
                self.assertEqual(cli("pane", "layout", "--pane", done)["layout"]["focused_pane_id"], done)
                action("previous-attention", done)
                self.assertEqual(cli("pane", "layout", "--pane", blocked)["layout"]["focused_pane_id"], blocked)
                agents = cli("agent", "list")["agents"]
                self.assertEqual(next(a for a in agents if a["pane_id"] == done)["tokens"]["attn"], "done")
                action("mark-reviewed", done)
                action("next-attention", blocked)
                agents = cli("agent", "list")["agents"]
                self.assertEqual(next(a for a in agents if a["pane_id"] == done)["tokens"]["attn"], "idle")
            finally:
                # Only this uniquely named test session and its state are touched.
                subprocess.run(["herdr", "session", "stop", session], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.terminate()
                    server.wait(timeout=10)
                subprocess.run(["herdr", "session", "delete", session], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                state = Path.home() / ".local/state/herdr/plugins/attention-queue/sessions" / session
                shutil.rmtree(state, ignore_errors=True)
                log.close()
