"""OpenCode's HTTP API: sessions, prompts, permissions and the event stream.

Every device runs `opencode serve --hostname 0.0.0.0 --port 4096` with the same
OPENCODE_PASSWORD.  That password is Basic auth here, and it is the trust
boundary: whoever has it can run anything on that device.
"""

from __future__ import annotations

import base64
import json
import queue
import socket
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any
from collections.abc import Iterator

from . import httpc, log


class OpenCodeError(Exception):
    def __init__(self, status: int, message: str, where: str) -> None:
        super().__init__(f"{where}: {message}" + (f" ({status})" if status else ""))
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Health:
    version: str
    hostname: str
    pid: int


def _auth(password: str) -> str:
    return "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()


def busy_since(info: dict) -> float | None:
    """When the session's turn started, in milliseconds, while it still runs.

    The device stamps `busy` when a turn starts and `idle` when it ends, so a
    finished turn has an `idle` at least as new as its `busy`.  This is the
    only record that knows: the run may belong to another daemon, and it
    outlives this daemon's timeout and its restarts.
    """
    stamps = info.get("time") or {}
    busy, idle = stamps.get("busy"), stamps.get("idle")
    if not isinstance(busy, (int, float)):
        return None
    if isinstance(idle, (int, float)) and idle >= busy:
        return None
    return float(busy)


class OpenCode:
    def __init__(self, base: str, password: str, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.password = password
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: Any = None,
               timeout: float | None = None) -> Any:
        headers = {"Authorization": _auth(self.password)}
        body = None
        if payload is not None:
            body = httpc.json_body(payload)
            headers["Content-Type"] = "application/json"
        try:
            status, _, raw = httpc.call(
                f"{self.base}{path}", method, headers=headers, body=body,
                timeout=timeout or self.timeout,
            )
        except httpc.HttpError as error:
            raise OpenCodeError(0, str(error), f"{method} {path}") from error
        parsed = httpc.parse_json(raw)
        if not 200 <= status < 300:
            message = ""
            if isinstance(parsed, dict):
                message = str(parsed.get("message") or parsed.get("_tag") or "")
            raise OpenCodeError(status, message or "no message", f"{method} {path}")
        return parsed

    # -- what is there ----------------------------------------------------

    def info(self) -> Health:
        data = self._call("GET", "/api/info", timeout=5.0)
        if not isinstance(data, dict):
            raise OpenCodeError(0, "no answer", "GET /api/info")
        return Health(
            version=str(data.get("version", "?")),
            hostname=str(data.get("hostname", "?")),
            pid=int(data.get("pid", 0)),
        )

    def agent_ids(self) -> list[str]:
        data = self._call("GET", "/api/agent")
        return [entry.get("id", "") for entry in (data or {}).get("data", []) if entry.get("id")]

    # -- sessions ---------------------------------------------------------

    def sessions(self, limit: int = 10) -> list[dict]:
        data = self._call("GET", f"/api/session?limit={int(limit)}&order=desc")
        return (data or {}).get("data", []) if isinstance(data, dict) else []

    def session(self, session_id: str) -> dict:
        data = self._call("GET", f"/api/session/{session_id}")
        return data.get("data", {}) if isinstance(data, dict) else {}

    def messages(self, session_id: str, limit: int = 20, order: str = "desc",
                 kind: str | None = None) -> list[dict]:
        query = f"limit={int(limit)}&order={order}"
        if kind:
            query += f"&type={kind}"
        data = self._call("GET", f"/api/session/{session_id}/message?{query}")
        return data.get("data", []) if isinstance(data, dict) else []

    def create_session(self, directory: str, title: str, model: dict | None = None,
                       agent: str | None = None, metadata: dict | None = None) -> dict:
        payload: dict[str, Any] = {"title": title, "location": {"directory": directory}}
        if model:
            payload["model"] = model
        if agent:
            payload["agent"] = agent
        if metadata:
            payload["metadata"] = metadata
        data = self._call("POST", "/api/session", payload)
        return data.get("data", {}) if isinstance(data, dict) else {}

    def set_model(self, session_id: str, model: dict) -> None:
        self._call("POST", f"/api/session/{session_id}/model", {"model": model})

    def prompt(self, session_id: str, text: str, delivery: str = "steer") -> dict:
        data = self._call(
            "POST",
            f"/api/session/{session_id}/prompt",
            {"text": text, "delivery": delivery},
        )
        return data.get("data", {}) if isinstance(data, dict) else {}

    def command(self, session_id: str, name: str, text: str) -> dict:
        """Run one of the device's slash commands, on a session.

        `/goal <objective>` is the one this bot uses: it admits the objective
        and starts the agent on it, and the device keeps it across turns.
        """
        data = self._call(
            "POST",
            f"/api/session/{session_id}/command",
            {"name": name, "text": text},
        )
        return data.get("data", {}) if isinstance(data, dict) else {}

    def set_goal(self, session_id: str, objective: str) -> dict:
        return self.command(session_id, "goal", objective)

    def goal(self, session_id: str) -> dict | None:
        """The session's goal, or None when it has none.

        `status` is `active`, `paused` or `complete`; a paused goal names its
        reason, which is how a device that restarted under an active goal says
        so.  None means no goal, and an empty dict a device that does not
        answer: the caller must not read one as the other.
        """
        data = self._call(
            "POST", "/api/rpc/opencode.goal/get", {"input": {"sessionID": session_id}},
        )
        goal = (data or {}).get("output") if isinstance(data, dict) else None
        if goal is None:
            return None
        return goal if isinstance(goal, dict) else {}

    def interrupt(self, session_id: str) -> None:
        self._call("POST", f"/api/session/{session_id}/interrupt")

    def generate_text(self, prompt: str, model: dict | None = None,
                      timeout: float = 300.0) -> str:
        """One stateless answer: no session, no tools, no memory of it after."""
        payload: dict[str, Any] = {"prompt": prompt}
        if model:
            payload["model"] = model
        data = self._call("POST", "/api/experimental/generate/text", payload, timeout=timeout)
        return ((data or {}).get("data") or {}).get("text", "")

    def reply_permission(self, session_id: str, request_id: str, decision: str,
                         message: str = "") -> None:
        self._call(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            {"decision": decision, "message": message or None},
        )

    def forms(self, session_id: str) -> list[dict]:
        """The forms this session still waits on."""
        data = self._call("GET", f"/api/session/{session_id}/form")
        return data.get("data", []) if isinstance(data, dict) else []

    def reply_form(self, session_id: str, form_id: str, answer: dict) -> None:
        """Answer a form.  One reply carries every field, so collect them first."""
        self._call(
            "POST",
            f"/api/session/{session_id}/form/{form_id}/reply",
            {"answer": answer},
        )

    def user_activity(self, session_id: str) -> float:
        """The newest human message, in milliseconds, or 0."""
        found = self.messages(session_id, limit=1, order="desc", kind="user")
        newest = 0.0
        for message in found:
            created = (message.get("time") or {}).get("created")
            if isinstance(created, (int, float)):
                newest = max(newest, float(created))
        return newest


class EventStream:
    """SSE from /api/event, read on its own thread into a queue."""

    def __init__(self, base: str, password: str, stop: threading.Event) -> None:
        self.base = base.rstrip("/")
        self.password = password
        self.stop = stop
        self.events: queue.Queue[dict | None] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.answer = None   # the live response, which `close` shuts

    def start(self) -> EventStream:
        self.thread = threading.Thread(target=self._run, name="opencode-events", daemon=True)
        self.thread.start()
        return self

    def close(self) -> None:
        """Stop reading and hand the connection back.

        Setting the flag alone left the thread blocked in a read for as long as
        the device had nothing to say, so every run left a connection and a
        thread behind it.  Closing the response is not the way: `HTTPResponse`
        is not safe to close from another thread, and it waits on the buffer
        lock the reader holds, which hangs the run for good.  Shutting the
        socket down wakes that read, and then the thread closes everything it
        owns itself.
        """
        self.stop.set()
        answer = self.answer
        sock = getattr(getattr(getattr(answer, "fp", None), "raw", None), "_sock", None)
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _run(self) -> None:
        request = urllib.request.Request(
            f"{self.base}/api/event",
            headers={
                "Authorization": _auth(self.password),
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            },
        )
        data: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=60) as answer:
                self.answer = answer
                self.ready.set()
                for raw in answer:
                    if self.stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith(":"):
                        continue
                    if not line:
                        if data:
                            payload = "\n".join(data)
                            data = []
                            try:
                                self.events.put(json.loads(payload))
                            except json.JSONDecodeError:
                                continue
                        continue
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
        except Exception as error:  # the caller reads the queue's end
            if not self.stop.is_set():
                log.info(f"events: stream ended ({error})")
        finally:
            self.answer = None
            self.ready.set()
            self.events.put(None)

    def __iter__(self) -> Iterator[dict]:
        return self.iter_events()

    def iter_events(self) -> Iterator[dict]:
        while True:
            item = self.events.get()
            if item is None:
                return
            yield item
