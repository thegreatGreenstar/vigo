"""An OpenCode API on 127.0.0.1, for tests: sessions, prompts, events."""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from vigo.opencode_api import busy_since


class FakeOpenCode:
    def __init__(self, name: str = "macbook", password: str = "pw",
                 directory: str = "/tmp/project") -> None:
        self.name = name
        self.password = password
        self.directory = directory
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}
        self.subscribers: list[queue.Queue] = []
        self.prompts: list[tuple[str, str]] = []
        self.commands: list[tuple[str, str, str]] = []
        self.goals: dict[str, dict] = {}
        self.hold_next = 0          # swallow this many admissions, as a stuck drain did
        self.hold_busy = False      # ... and stamp the session busy while doing it
        self.stay_busy = False      # a turn that ends leaves the session busy
        self.generated: list[str] = []
        self.permission_replies: list[tuple[str, str, str]] = []
        self.form_replies: list[tuple[str, str, dict]] = []
        self.forms: dict[str, dict] = {}
        self.agents = [
            {"id": "build", "mode": "primary", "name": "Build"},
            {"id": "general", "mode": "subagent", "name": "General"},
            {"id": "explore", "mode": "subagent", "name": "Explore"},
        ]
        self.interrupts: list[str] = []
        self.counter = 0
        self.script: list[tuple] = []
        self.activity_ms = 0.0
        self.description = "fixing the thing"
        self.answer = "one-off answer"
        # The plan's meters and the totals of a window: the usage cap reads
        # both, and a test sets them to the numbers it needs.
        self.metered = True         # answer the usage endpoints at all
        self.limits_error = 0       # an HTTP status instead of the meters
        self.meters = {"rolling": 0.0, "weekly": 0.0, "monthly": 0.0}
        self.limited_meters: tuple[str, ...] = ()
        self.reset_at = int(time.time() * 1000) + 86400000
        self.limits_reads = 0
        self.stats_reads: list[tuple[float, float]] = []
        self.hang_events = threading.Event()
        self.permission_waiters: dict[str, threading.Event] = {}
        self.form_waiters: dict[str, threading.Event] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        # A short poll: `shutdown()` waits for the loop to notice, and half a
        # second per server is half a second per test.
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.hang_events.set()
        for waiter in list(self.permission_waiters.values()) + list(self.form_waiters.values()):
            waiter.set()
        self.server.shutdown()
        self.server.server_close()

    # -- helpers ----------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self.counter += 1
            return f"{prefix}_{self.counter}"

    def emit(self, kind: str, data: dict) -> None:
        event = {"id": self.next_id("evt"), "type": kind, "data": data}
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def add_message(self, session: str, kind: str, **fields) -> dict:
        message = {"id": self.next_id("msg"), "type": kind,
                   "time": {"created": time.time() * 1000}, **fields}
        with self.lock:
            self.messages.setdefault(session, []).append(message)
        return message

    def new_session(self, title: str = "test", metadata: dict | None = None,
                    agent: str = "", parent: str = "") -> str:
        session = self.next_id("ses")
        with self.lock:
            self.sessions[session] = {
                "id": session, "projectID": "proj", "cost": 0, "tokens": {},
                "time": {"created": time.time() * 1000, "updated": time.time() * 1000},
                "title": title, "location": {"directory": self.directory},
                "metadata": dict(metadata or {}), "agent": agent or "build",
            }
            if parent:
                self.sessions[session]["parentID"] = parent
            self.messages.setdefault(session, [])
        return session

    def spent_between(self, start_ms: float, end_ms: float) -> dict:
        """What every session on this fake spent between two times.

        The real device answers this from its own record; the fake sums the
        assistant messages it holds, so a test sets a session's cost with the
        messages it adds.
        """
        totals = {"input": 0, "output": 0, "reasoning": 0,
                  "cache": {"read": 0, "write": 0}}
        with self.lock:
            every = list(self.messages.values())
        for messages in every:
            for message in messages:
                if message.get("type") != "assistant":
                    continue
                created = float((message.get("time") or {}).get("created") or 0)
                if not start_ms <= created <= end_ms:
                    continue
                tokens = message.get("tokens") or {}
                for key in ("input", "output", "reasoning"):
                    totals[key] += int(tokens.get(key) or 0)
                totals["cache"]["read"] += int((tokens.get("cache") or {}).get("read") or 0)
        return totals

    def running(self) -> dict[str, dict]:
        """What `/api/session/active` answers: the sessions a turn runs in."""
        with self.lock:
            return {session: {"type": "running"} for session, info in self.sessions.items()
                    if busy_since(info) is not None}

    def started_turn(self, session: str) -> None:
        """Mark the session busy, as the device does while an agent works."""
        with self.lock:
            stamps = self.sessions[session]["time"]
            stamps["busy"] = time.time() * 1000
            stamps.pop("idle", None)

    def finished_turn(self, session: str) -> None:
        """Mark the session idle, as the device does when the agent stops."""
        with self.lock:
            stamps = self.sessions[session]["time"]
            stamps["busy"] = time.time() * 1000
            stamps["idle"] = stamps["busy"] + 1
            stamps["updated"] = stamps["idle"]

    def wait_for_prompt(self, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.prompts:
                return True
            time.sleep(0.02)
        return False

    def start(self, session: str, text: str, kind: str = "user") -> None:
        """Admit a question the way the device does: record it, then start on it.

        With `hold_next` the device swallows that many admissions, as it did
        when its session drain hung: the question is recorded and the agent
        never starts, so nothing on the session is newer than the question.
        """
        with self.lock:
            self.prompts.append((session, text))
            held = self.hold_next > 0
            if held:
                self.hold_next -= 1
        if held:
            if self.hold_busy:
                # The device did this on the real machine: a turn it never
                # actually ran, stamped busy, with the question still undelivered.
                self.started_turn(session)
            return
        if kind == "synthetic":
            self.add_message(session, "synthetic", text=text,
                             metadata={"source": "goal"})
        else:
            self.add_message(session, "user", text=text)
        self.started_turn(session)
        threading.Thread(target=self.run_script, args=(session,), daemon=True).start()

    def run_command(self, session: str, name: str, text: str) -> None:
        """A slash command on the device; `/goal` is the one this bot sends."""
        with self.lock:
            self.commands.append((session, name, text))
        if name != "goal":
            return
        word = text.strip().lower()
        goal_id = self.next_id("goal")
        with self.lock:
            if word == "clear":
                self.goals.pop(session, None)
                return
            if word in ("pause", "resume"):
                held = self.goals.get(session)
                if held is not None:
                    held["status"] = "paused" if word == "pause" else "active"
                return
            self.goals[session] = {
                "id": goal_id, "objective": text, "status": "active",
                "rounds": 0, "maxRounds": 50,
            }
        self.start(session, text, kind="synthetic")

    # -- the script -------------------------------------------------------

    def run_script(self, session: str) -> None:
        steps = list(self.script)
        for step in steps:
            if self.hang_events.is_set():
                return
            action = step[0]
            if action == "delta":
                self.emit("session.text.delta",
                          {"sessionID": session, "assistantMessageID": "msg_a",
                           "ordinal": 0, "delta": step[1]})
                time.sleep(0.01)
            elif action == "tool":
                self.emit("session.tool.called",
                          {"sessionID": session, "assistantMessageID": "msg_a",
                           "id": self.next_id("tool"), "input": {"name": step[1]}})
            elif action == "permission":
                request = self.next_id("perm")
                waiter = threading.Event()
                self.permission_waiters[request] = waiter
                self.emit("permission.asked", {
                    "id": request, "sessionID": session,
                    "action": step[1], "resources": step[2] if len(step) > 2 else [],
                })
                waiter.wait(10.0)
                self.emit("permission.replied", {"sessionID": session, "requestID": request})
            elif action == "until":
                # A turn that runs until the test says it may finish: no
                # sleeping, and the test decides when the work is over.
                step[1].wait(10.0)
            elif action == "form":
                form = dict(step[1])
                form["sessionID"] = session
                form.setdefault("id", self.next_id("frm"))
                waiter = threading.Event()
                with self.lock:
                    self.forms[form["id"]] = form
                    self.form_waiters[form["id"]] = waiter
                self.emit("form.created", {"form": form})
                waiter.wait(10.0)
                self.emit("form.replied", {"id": form["id"], "sessionID": session,
                                           "answer": {}})
            elif action == "fail":
                # The device's turn failed: the reason is what the person
                # needs, and the event carries it the way the device writes it.
                self.emit("session.execution.failed", {
                    "sessionID": session,
                    "message": step[1] if len(step) > 1 else "the turn failed",
                })
                return
            elif action == "idle":
                self.add_message(session, "assistant",
                                 content=[{"type": "text", "text": step[1] if len(step) > 1 else "done"}],
                                 cost=0.002, tokens={"input": 120, "output": 8})
                # The device ends a turn with an idle message on the session,
                # which is what a run reads when the event that said so was
                # missed: a restart in between leaves nothing else.
                self.add_message(session, "idle")
                with self.lock:
                    if session in self.sessions:
                        self.sessions[session]["cost"] = 0.002
                        self.sessions[session]["tokens"] = {
                            "input": 120, "output": 8, "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        }
                # The device is idle before it says so: the next question in
                # the channel may start the moment it hears the event, and it
                # must not find the turn before this one still busy.
                if not self.stay_busy:
                    # With `stay_busy` the turn ends but the device still looks
                    # busy: the other daemon's turn began under it.
                    self.finished_turn(session)
                self.emit("session.execution.succeeded", {"sessionID": session})
                self.emit("session.idle", {"sessionID": session})
                return

    # -- the handler ------------------------------------------------------

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status: int, body: object = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def _authed(self) -> bool:
                header = self.headers.get("Authorization") or ""
                expected = "Basic " + base64.b64encode(
                    f"opencode:{fake.password}".encode()).decode()
                if header != expected:
                    self._send(401, {"_tag": "UnauthorizedError", "message": "Authentication required"})
                    return False
                return True

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    return {}

            def do_GET(self) -> None:
                if not self._authed():
                    return
                parts = urlsplit(self.path)
                path = parts.path
                query = parse_qs(parts.query)
                if path == "/api/info":
                    self._send(200, {"version": "2.2.5", "pid": 1, "hostname": fake.name,
                                     "urls": [fake.url], "paths": {}, "capabilities": {}})
                    return
                if path == "/api/experimental/usage/limits":
                    fake.limits_reads += 1
                    if fake.limits_error:
                        self._send(fake.limits_error, {"_tag": "NotFound", "message": "no route"})
                        return
                    if not fake.metered:
                        self._send(404, {"_tag": "NotFound", "message": "no route"})
                        return
                    meters = {
                        name: {"percent": float(fake.meters.get(name, 0.0)),
                               "limited": name in fake.limited_meters,
                               "resetsAt": fake.reset_at}
                        for name in ("rolling", "weekly", "monthly")
                    }
                    self._send(200, {
                        "location": {"directory": fake.directory},
                        "data": {
                            "workspace": {"id": "org", "name": "Personal",
                                          "usageURL": "https://example.invalid/usage"},
                            "go": meters, "budgets": [],
                        },
                    })
                    return
                if path == "/api/experimental/session/stats":
                    start = float(query.get("from", ["0"])[0])
                    end = float(query.get("to", [str(time.time() * 1000)])[0])
                    with fake.lock:
                        fake.stats_reads.append((start, end))
                    self._send(200, {"data": {
                        "tokens": fake.spent_between(start, end), "cost": 0,
                        "sessions": len(fake.sessions), "subagents": 0,
                        "prompts": 0, "steps": 0,
                    }})
                    return
                if path == "/api/event":
                    # Subscribe before answering: the client is free to emit as
                    # soon as it reads the headers, and an event sent before
                    # this list holds the channel is an event nobody sees.
                    channel: queue.Queue = queue.Queue()
                    with fake.lock:
                        fake.subscribers.append(channel)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        self.wfile.write(b": ready\n\n")
                        self.wfile.flush()
                        while not fake.hang_events.is_set():
                            try:
                                event = channel.get(timeout=0.2)
                            except queue.Empty:
                                continue
                            raw = f"data: {json.dumps(event)}\n\n".encode()
                            self.wfile.write(raw)
                            self.wfile.flush()
                    except OSError:
                        # A reader that went away: BrokenPipe on POSIX,
                        # ConnectionAborted (10053) or Reset (10054) on Windows.
                        pass
                    finally:
                        with fake.lock:
                            fake.subscribers.remove(channel)
                    return
                if path == "/api/agent":
                    self._send(200, {
                        "location": {"directory": fake.directory},
                        "data": list(fake.agents),
                    })
                    return
                if path == "/api/session/active":
                    self._send(200, {"data": fake.running()})
                    return
                if path == "/api/session":
                    with fake.lock:
                        listed = list(fake.sessions.values())
                    self._send(200, {"data": listed, "cursor": {"previous": None, "next": None}})
                    return
                if path.startswith("/api/session/") and path.endswith("/message"):
                    session = path.split("/")[3]
                    limit = int(query.get("limit", ["20"])[0])
                    kind = query.get("type", [None])[0]
                    order = query.get("order", ["desc"])[0]
                    with fake.lock:
                        found = list(fake.messages.get(session, []))
                    if kind:
                        found = [message for message in found if message.get("type") == kind]
                    if order == "desc":
                        found = list(reversed(found))
                    # The cursor is an offset here: the real device's is
                    # opaque, and all a client may do with either is follow it.
                    offset = int((query.get("cursor") or ["0"])[0] or 0)
                    window = found[offset:offset + limit]
                    more = offset + limit < len(found)
                    self._send(200, {"data": window, "cursor": {
                        "previous": str(offset) if offset else None,
                        "next": str(offset + limit) if more else None,
                    }})
                    return
                if path.startswith("/api/session/") and path.endswith("/form"):
                    session = path.split("/")[3]
                    with fake.lock:
                        listed = [form for form in fake.forms.values()
                                  if form.get("sessionID") == session]
                    self._send(200, {"data": listed})
                    return
                if path.startswith("/api/session/"):
                    session = path.rsplit("/", 1)[1]
                    with fake.lock:
                        info = fake.sessions.get(session)
                    if not info:
                        self._send(404, {"_tag": "SessionNotFoundError", "message": "no session"})
                        return
                    self._send(200, {"data": info})
                    return
                self._send(404, {"_tag": "NotFound", "message": "no route"})

            def do_PATCH(self) -> None:
                if not self._authed():
                    return
                path = urlsplit(self.path).path
                body = self._body()
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "api" and parts[1] == "session":
                    with fake.lock:
                        info = fake.sessions.get(parts[2])
                        if info is not None and "metadata" in body:
                            # Replaced, not merged, as the real device does.
                            info["metadata"] = dict(body.get("metadata") or {})
                    self._send(204)
                    return
                self._send(404, {"_tag": "NotFound", "message": "no route"})

            def do_POST(self) -> None:
                if not self._authed():
                    return
                path = urlsplit(self.path).path
                body = self._body()
                if path == "/api/experimental/generate/text":
                    said = str(body.get("prompt") or "")
                    with fake.lock:
                        fake.generated.append(said)
                    # Vigo asks for a short phrase for the acknowledgement
                    # before the answer itself; this fake tells them apart by
                    # the instruction's first words.
                    text = fake.description if said.startswith("In one short phrase") else fake.answer
                    self._send(200, {"data": {"text": text}})
                    return
                if path == "/api/session":
                    title = body.get("title") or "test"
                    session = fake.new_session(title, body.get("metadata"),
                                               str(body.get("agent") or ""))
                    self._send(200, {"data": fake.sessions[session]})
                    return
                if path == "/api/rpc/opencode.goal/get":
                    session = str((body.get("input") or {}).get("sessionID") or "")
                    with fake.lock:
                        held = fake.goals.get(session)
                    self._send(200, {"output": dict(held) if held else None})
                    return
                parts = path.strip("/").split("/")
                if len(parts) >= 4 and parts[0] == "api" and parts[1] == "session":
                    session = parts[2]
                    tail = parts[3]
                    if tail == "prompt":
                        text = str(body.get("text") or "")
                        fake.start(session, text)
                        self._send(200, {"data": {"id": fake.next_id("msg"), "sessionID": session,
                                                  "type": "user", "payload": {"text": text}}})
                        return
                    if tail == "command":
                        fake.run_command(session, str(body.get("name") or ""),
                                         str(body.get("text") or ""))
                        self._send(200, {"data": {}})
                        return
                    if tail == "model":
                        self._send(204)
                        return
                    if tail == "interrupt":
                        with fake.lock:
                            fake.interrupts.append(session)
                        fake.emit("session.execution.interrupted", {"sessionID": session})
                        self._send(204)
                        return
                    if tail == "permission":
                        request = parts[4] if len(parts) > 4 else ""
                        decision = str(body.get("decision") or "")
                        with fake.lock:
                            fake.permission_replies.append((session, request, decision))
                        waiter = fake.permission_waiters.pop(request, None)
                        if waiter:
                            waiter.set()
                        self._send(204)
                        return
                    if tail == "form" and len(parts) >= 6 and parts[5] == "reply":
                        form_id = parts[4]
                        answer = body.get("answer") or {}
                        with fake.lock:
                            fake.form_replies.append((session, form_id, answer))
                            fake.forms.pop(form_id, None)
                        waiter = fake.form_waiters.pop(form_id, None)
                        if waiter:
                            waiter.set()
                        self._send(204)
                        return
                self._send(404, {"_tag": "NotFound", "message": "no route"})

        return Handler
