"""A Discord REST API on 127.0.0.1, for tests."""

from __future__ import annotations

import datetime
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeDiscord:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.acked: set[str] = set()
        self.responses: list[dict] = []
        self.originals: dict[str, dict] = {}  # interaction token -> its message
        self.edits: list[dict] = []
        self.followups: list[dict] = []
        self.deleted: list[str] = []
        self.messages: dict[str, list[dict]] = {}
        self.commands: dict[str, list[dict]] = {}
        self.global_commands: list[dict] = []
        self.global_command_puts = 0
        self.dm_channels: dict[str, str] = {}
        self.counter = 0
        self.gateway = ""
        self.forbidden: set[str] = set()  # channel ids that answer 403
        self.no_dm: set[str] = set()  # user ids whose DM cannot be opened
        self.uneditable: set[str] = set()  # interaction tokens whose message is gone
        self.guild_commands_puts = 0
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
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/v10"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- helpers ----------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self.counter += 1
            return f"{prefix}_{self.counter}"

    def answer_for(self, interaction_id: str) -> dict | None:
        for entry in self.responses:
            if entry["id"] == interaction_id:
                return entry
        return None

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def add_message(self, channel: str, content: str, author: str = "1",
                    bot: bool = False) -> dict:
        message = {
            "id": self.next_id("msg"),
            "channel_id": channel,
            "content": content,
            "author": {"id": author, "bot": bot},
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with self.lock:
            self.messages.setdefault(channel, []).insert(0, message)
        return message

    # -- the handler ------------------------------------------------------

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep the tests quiet
                pass

            def _send(self, status: int, body: object = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    return {}

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                path = parts.path
                query = parse_qs(parts.query)
                if path.endswith("/gateway/bot"):
                    self._send(200, {"url": fake.gateway, "shards": 1})
                    return
                if "/guilds/" in path and path.endswith("/commands"):
                    guild = path.split("/guilds/")[1].split("/")[0]
                    self._send(200, fake.commands.get(guild, []))
                    return
                if path.endswith("/commands"):
                    self._send(200, fake.global_commands)
                    return
                if "/channels/" in path and path.endswith("/messages"):
                    channel = path.split("/channels/")[1].split("/")[0]
                    if channel in fake.forbidden:
                        self._send(403, {"code": 50001, "message": "Missing Access"})
                        return
                    limit = int(query.get("limit", ["50"])[0])
                    self._send(200, fake.messages.get(channel, [])[:limit])
                    return
                if "/channels/" in path and "/messages/" in path:
                    channel = path.split("/channels/")[1].split("/")[0]
                    message_id = path.rsplit("/", 1)[1]
                    for message in fake.messages.get(channel, []):
                        if message["id"] == message_id:
                            self._send(200, message)
                            return
                    self._send(404, {"code": 10008, "message": "Unknown Message"})
                    return
                if "/channels/" in path:
                    self._send(200, {"id": path.rsplit("/", 1)[1], "name": "general"})
                    return
                self._send(404, {"code": 0, "message": "no route"})

            def do_PUT(self) -> None:
                path = urlsplit(self.path).path
                body = self._body()
                if "/guilds/" in path and path.endswith("/commands"):
                    guild = path.split("/guilds/")[1].split("/")[0]
                    with fake.lock:
                        fake.guild_commands_puts += 1
                        fake.commands[guild] = [
                            dict(command, id=f"cmd_{index}")
                            for index, command in enumerate(body if isinstance(body, list) else [])
                        ]
                    self._send(200, fake.commands[guild])
                    return
                if path.endswith("/commands"):
                    with fake.lock:
                        fake.global_command_puts += 1
                        fake.global_commands = [
                            dict(command, id=f"cmd_{index}")
                            for index, command in enumerate(body if isinstance(body, list) else [])
                        ]
                    self._send(200, fake.global_commands)
                    return
                self._send(404, {"code": 0, "message": "no route"})

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                body = self._body()
                if "/interactions/" in path and path.endswith("/callback"):
                    interaction_id = path.split("/interactions/")[1].split("/")[0]
                    token = path.split("/interactions/")[1].split("/")[1]
                    kind = body.get("type")
                    data = body.get("data") or {}
                    with fake.lock:
                        if interaction_id in fake.acked:
                            self._send(400, {"code": 40060,
                                             "message": "Interaction has already been acknowledged."})
                            return
                        fake.acked.add(interaction_id)
                        fake.responses.append({"id": interaction_id, "type": kind,
                                               "data": data})
                        # A callback with content sends the message at once; a
                        # deferral is a message with no content yet, which the
                        # "is thinking" state marks.
                        if kind in (4, 5):
                            fake.originals[token] = {
                                "content": str(data.get("content") or ""),
                                "loading": kind == 5,
                                "ephemeral": bool(data.get("flags")),
                                "deleted": False,
                            }
                    self._send(204 if kind == 5 else 200)
                    return
                if "/users/@me/channels" in path:
                    user = str(body.get("recipient_id"))
                    if user in fake.no_dm:
                        self._send(403, {"code": 50007, "message": "Cannot send messages to this user"})
                        return
                    with fake.lock:
                        channel = fake.dm_channels.setdefault(user, f"dm_{user}")
                    self._send(200, {"id": channel, "type": 1})
                    return
                if "/webhooks/" in path:
                    token = path.split("/webhooks/")[1].split("/")[1]
                    if token in fake.uneditable:
                        self._send(404, {"code": 10015, "message": "Unknown Webhook"})
                        return
                    original = fake.originals.get(token)
                    if original is not None and original["loading"]:
                        # Discord's documented compatibility rule: the first
                        # followup after a deferral edits the loading message
                        # instead of creating a new one.
                        with fake.lock:
                            original["content"] = str(body.get("content") or "")
                            original["loading"] = False
                            fake.edits.append({"token": token,
                                               "content": original["content"],
                                               "components": body.get("components")})
                        self._send(200, {"id": "original", "content": original["content"]})
                        return
                    with fake.lock:
                        fake.followups.append({"token": token,
                                               "content": body.get("content"),
                                               "flags": body.get("flags")})
                    self._send(200, {"id": fake.next_id("msg"), "content": body.get("content")})
                    return
                if "/channels/" in path and path.endswith("/messages"):
                    channel = path.split("/channels/")[1].split("/")[0]
                    if channel in fake.forbidden:
                        self._send(403, {"code": 50001, "message": "Missing Access"})
                        return
                    message = fake.add_message(channel, str(body.get("content") or ""),
                                               bot=True)
                    if body.get("components"):
                        message["components"] = body["components"]
                    if body.get("allowed_mentions") is not None:
                        message["allowed_mentions"] = body["allowed_mentions"]
                    self._send(200, message)
                    return
                self._send(404, {"code": 0, "message": "no route"})

            def do_DELETE(self) -> None:
                path = urlsplit(self.path).path
                if path.endswith("/messages/@original"):
                    token = path.split("/webhooks/")[1].split("/")[1]
                    with fake.lock:
                        original = fake.originals.get(token)
                        if original is not None:
                            original["content"] = ""
                            original["loading"] = False
                            original["deleted"] = True
                        fake.deleted.append(token)
                    self._send(204)
                    return
                self._send(404, {"code": 0, "message": "no route"})

            def do_PATCH(self) -> None:
                path = urlsplit(self.path).path
                body = self._body()
                if path.endswith("/messages/@original"):
                    token = path.split("/webhooks/")[1].split("/")[1]
                    if token in fake.uneditable:
                        self._send(404, {"code": 10015, "message": "Unknown Webhook"})
                        return
                    with fake.lock:
                        original = fake.originals.setdefault(token, {
                            "content": "", "loading": True, "ephemeral": False,
                            "deleted": False,
                        })
                        original["content"] = str(body.get("content") or "")
                        original["loading"] = False
                        fake.edits.append({"token": token, "content": original["content"],
                                           "components": body.get("components")})
                    self._send(200, {"id": "original", "content": original["content"]})
                    return
                if "/channels/" in path and "/messages/" in path:
                    channel = path.split("/channels/")[1].split("/")[0]
                    message_id = path.rsplit("/", 1)[1]
                    if channel in fake.forbidden:
                        self._send(403, {"code": 50001, "message": "Missing Access"})
                        return
                    with fake.lock:
                        for message in fake.messages.get(channel, []):
                            if message["id"] == message_id:
                                message["content"] = body.get("content", message["content"])
                                self._send(200, message)
                                return
                    self._send(404, {"code": 10008, "message": "Unknown Message"})
                    return
                self._send(404, {"code": 0, "message": "no route"})

        return Handler
