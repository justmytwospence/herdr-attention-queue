"""In-process fake of the herdr socket API, covering the calls the plugin makes."""

import json
import os
import re
import shutil
import socket
import tempfile
import threading

TOKEN_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# done and idle are the same underlying lifecycle state (done = not yet seen).
LIFECYCLE = {"done": "idle"}


class FakeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class FakeHerdr:
    def __init__(self):
        # /tmp keeps the AF_UNIX path under macOS's ~104 byte limit.
        self.dir = tempfile.mkdtemp(prefix="aq-", dir="/tmp")
        self.path = os.path.join(self.dir, "herdr.sock")
        self.agents = {}
        self.order = []
        self.calls = []
        self.violations = []
        self.view = None
        self._seqs = {}
        self._next_seq = 0
        self._lock = threading.Lock()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(128)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- fixture helpers ---------------------------------------------------

    def add_agent(self, pane_id, status="idle", terminal_id=None, session="s1", agent="claude"):
        with self._lock:
            self._next_seq += 1
            self.agents[pane_id] = {
                "pane_id": pane_id,
                "workspace_id": pane_id.split(":")[0],
                "terminal_id": terminal_id or "term-" + pane_id.replace(":", "-"),
                "agent": agent,
                "agent_status": status,
                "state_change_seq": self._next_seq,
                "agent_session": (
                    {"source": "herdr:" + agent, "agent": agent, "kind": "id", "value": session}
                    if session
                    else None
                ),
                "tokens": {},
            }
            self.order.append(pane_id)

    def set_status(self, pane_id, status):
        """Like herdr: state_change_seq moves only on a lifecycle change, not a seen flip."""
        with self._lock:
            agent = self.agents[pane_id]
            old = LIFECYCLE.get(agent["agent_status"], agent["agent_status"])
            new = LIFECYCLE.get(status, status)
            if old != new:
                self._next_seq += 1
                agent["state_change_seq"] = self._next_seq
            agent["agent_status"] = status

    def remove_agent(self, pane_id):
        with self._lock:
            self.agents.pop(pane_id, None)
            self.order.remove(pane_id)
            for key in [k for k in self._seqs if k[0] == pane_id]:
                del self._seqs[key]

    def tokens(self, pane_id):
        with self._lock:
            return dict(self.agents[pane_id]["tokens"])

    def close(self):
        try:
            self._server.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)

    # -- server ------------------------------------------------------------

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            request = json.loads(buf.split(b"\n", 1)[0])
            try:
                result = self._dispatch(request.get("method"), request.get("params") or {})
                reply = {"id": request.get("id"), "result": result}
            except FakeError as e:
                reply = {"id": request.get("id"), "error": {"code": e.code, "message": e.message}}
            conn.sendall((json.dumps(reply) + "\n").encode())

    def _dispatch(self, method, params):
        with self._lock:
            self.calls.append((method, json.loads(json.dumps(params))))
            if method == "agent.list":
                agents = []
                for pane_id in self.order:
                    agent = dict(self.agents[pane_id])
                    if agent["tokens"]:
                        agent["tokens"] = dict(agent["tokens"])
                    else:
                        del agent["tokens"]
                    agents.append(agent)
                return {"type": "agents", "agents": agents}
            if method == "pane.report_metadata":
                return self._report_metadata(params)
            if method == "agent.view.set":
                if not str(params.get("source", "")).startswith("plugin:"):
                    self.violations.append("view source %r" % params.get("source"))
                self.view = params
                return {"type": "agent_view", "active": True, "source": params.get("source")}
            if method == "agent.view.clear":
                self.view = None
                return {"type": "agent_view", "active": False}
            raise FakeError("unknown_method", str(method))

    def _report_metadata(self, params):
        source = str(params.get("source", ""))
        if not source.startswith("plugin:"):
            self.violations.append("metadata source %r" % source)
        tokens = params.get("tokens") or {}
        if len(tokens) > 16:
            self.violations.append("%d token keys in one report" % len(tokens))
        for name in tokens:
            if not TOKEN_NAME.match(name):
                self.violations.append("bad token name %r" % name)
        pane_id = params.get("pane_id")
        if pane_id not in self.agents:
            raise FakeError("pane_not_found", str(pane_id))
        seq = params.get("seq")
        if seq is not None:
            key = (pane_id, source)
            last = self._seqs.get(key)
            if last is not None and seq <= last:
                # herdr accepts and ignores it; the plugin should never send one.
                self.violations.append("non-increasing seq %s <= %s for %s" % (seq, last, key))
                return {"type": "ok"}
            self._seqs[key] = seq
        agent = self.agents[pane_id]
        if params.get("title") and params.get("agent") in (None, agent["agent"]):
            agent["title"] = params["title"]
            agent["display_agent"] = params.get("display_agent")
        if params.get("clear_title"):
            agent.pop("title", None)
        if params.get("clear_display_agent"):
            agent.pop("display_agent", None)
        held = self.agents[pane_id]["tokens"]
        for name, value in tokens.items():
            if value is None or value == "":
                held.pop(name, None)
            else:
                held[name] = value
        return {"type": "ok"}
