"""An OpenCode API on 127.0.0.1, for tests: sessions, prompts, events."""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


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
        self.generated: list[str] = []
        self.permission_replies: list[tuple[str, str, str]] = []
        self.interrupts: list[str] = []
        self.counter = 0
        self.script: list[tuple] = []
        self.activity_ms = 0.0
        self.hang_events = threading.Event()
        self.permission_waiters: dict[str, threading.Event] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.hang_events.set()
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

    def new_session(self, title: str = "test") -> str:
        session = self.next_id("ses")
        with self.lock:
            self.sessions[session] = {
                "id": session, "projectID": "proj", "cost": 0, "tokens": {},
                "time": {"created": time.time() * 1000, "updated": time.time() * 1000},
                "title": title, "location": {"directory": self.directory},
            }
            self.messages.setdefault(session, [])
        return session

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
            elif action == "sleep":
                time.sleep(step[1])
            elif action == "idle":
                self.add_message(session, "assistant",
                                 content=[{"type": "text", "text": step[1] if len(step) > 1 else "done"}],
                                 cost=0.002, tokens={"input": 120, "output": 8})
                self.emit("session.execution.succeeded", {"sessionID": session})
                self.emit("session.idle", {"sessionID": session})
                with self.lock:
                    if session in self.sessions:
                        self.sessions[session]["cost"] = 0.002
                        self.sessions[session]["tokens"] = {
                            "input": 120, "output": 8, "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        }
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
                if path == "/api/event":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    channel: queue.Queue = queue.Queue()
                    with fake.lock:
                        fake.subscribers.append(channel)
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
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        with fake.lock:
                            fake.subscribers.remove(channel)
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
                    self._send(200, {"data": found[:limit], "cursor": {"previous": None, "next": None}})
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

            def do_POST(self) -> None:
                if not self._authed():
                    return
                path = urlsplit(self.path).path
                body = self._body()
                if path == "/api/experimental/generate/text":
                    with fake.lock:
                        fake.generated.append(str(body.get("prompt") or ""))
                    self._send(200, {"data": {"text": "one-off answer"}})
                    return
                if path == "/api/session":
                    title = body.get("title") or "test"
                    session = fake.new_session(title)
                    self._send(200, {"data": fake.sessions[session]})
                    return
                parts = path.strip("/").split("/")
                if len(parts) >= 4 and parts[0] == "api" and parts[1] == "session":
                    session = parts[2]
                    tail = parts[3]
                    if tail == "prompt":
                        text = str(body.get("text") or "")
                        with fake.lock:
                            fake.prompts.append((session, text))
                        fake.add_message(session, "user", text=text)
                        threading.Thread(target=fake.run_script, args=(session,), daemon=True).start()
                        self._send(200, {"data": {"id": fake.next_id("msg"), "sessionID": session,
                                                  "type": "user", "payload": {"text": text}}})
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
                self._send(404, {"_tag": "NotFound", "message": "no route"})

        return Handler
