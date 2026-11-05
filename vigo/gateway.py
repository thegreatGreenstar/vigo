"""A WebSocket client for Discord's gateway, with heartbeats and resume.

Discord's gateway is a WebSocket of its own: the bot identifies once, beats
every interval, and resumes after a drop.  This file speaks just enough of
RFC 6455 to do that, and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
from typing import Callable

from . import log

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FATAL_CLOSE = {
    4004: "the token is not the bot's",
    4010: "invalid shard",
    4011: "sharding is required",
    4012: "the API version is not one Discord knows",
    4013: "invalid intents",
    4014: "the intents are not allowed for this bot",
}
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class GatewayClosed(Exception):
    pass


class FatalGatewayError(Exception):
    """Discord will not take a new try: a token that is not the bot's, intents off."""


class WebSocket:
    def __init__(self, url: str, timeout: float = 20.0):
        parsed = urllib.parse.urlsplit(url)
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.secure = parsed.scheme == "wss"
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.buffer = b""

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=self.host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        raw.sendall(request.encode())
        head = self._read_head()
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise GatewayClosed(f"handshake refused: {head.split(chr(13).encode())[0].decode(errors='replace')}")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"sec-websocket-accept" and value.strip().decode() != expected:
                raise GatewayClosed("handshake answer does not match the key")

    def _read_head(self) -> bytes:
        head = b""
        while b"\r\n\r\n" not in head:
            if self.sock is None:
                raise GatewayClosed("not connected")
            piece = self.sock.recv(4096)
            if not piece:
                raise GatewayClosed("closed during the handshake")
            head += piece
        head, _, rest = head.partition(b"\r\n\r\n")
        self.buffer += rest
        return head

    # -- frames -----------------------------------------------------------

    def _read(self, size: int, deadline: float) -> bytes:
        while len(self.buffer) < size:
            if self.sock is None:
                raise GatewayClosed("not connected")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayClosed("timed out")
            self.sock.settimeout(max(0.1, min(remaining, 5.0)))
            piece = self.sock.recv(65536)
            if not piece:
                raise GatewayClosed("closed by the server")
            self.buffer += piece
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def read_frame(self, idle_timeout: float) -> tuple[int, bytes] | None:
        """The next whole frame, or None when nothing arrived in idle_timeout."""
        try:
            head = self._read(2, time.monotonic() + idle_timeout)
        except socket.timeout:
            return None
        except ssl.SSLError as error:
            raise GatewayClosed(str(error)) from error
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        deadline = time.monotonic() + 30.0
        if length == 126:
            length = struct.unpack("!H", self._read(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8, deadline))[0]
        mask = self._read(4, deadline) if masked else b""
        payload = self._read(length, deadline) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def send(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None:
            raise GatewayClosed("not connected")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_json(self, value: object) -> None:
        self.send(OP_TEXT, json.dumps(value).encode())

    def close(self, code: int = 1000) -> None:
        if self.sock is None:
            return
        try:
            self.send(OP_CLOSE, struct.pack("!H", code))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None


class Gateway:
    """One Discord connection: identify, beat, resume, dispatch."""

    def __init__(
        self,
        url: str,
        token: str,
        intents: int,
        on_event: Callable[[str, dict], None],
        on_state: Callable[[str], None] | None = None,
    ):
        self.url = url
        self.token = token
        self.intents = intents
        self.on_event = on_event
        self.on_state = on_state or (lambda _text: None)
        self.session_id: str | None = None
        self.seq: int | None = None
        self.stopped = False
        self.ws: WebSocket | None = None

    def stop(self) -> None:
        self.stopped = True
        if self.ws:
            self.ws.close()

    def run(self) -> None:
        backoff = 1.0
        while not self.stopped:
            try:
                lived = self._session()
                if lived and not self.stopped:
                    log.info("gateway: session ended, resuming")
                backoff = 1.0
            except FatalGatewayError:
                raise
            except GatewayClosed as error:
                if not self.stopped:
                    log.info(f"gateway: {error}")
            except OSError as error:
                if not self.stopped:
                    log.info(f"gateway: {error}")
            except Exception as error:  # a frame the bot cannot read must not kill it
                log.error(f"gateway: {type(error).__name__}: {error}")
            finally:
                if self.ws:
                    self.ws.close()
                    self.ws = None
            if self.stopped:
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _session(self) -> bool:
        # A fresh connection each time: Discord keeps the old one open a moment.
        self.ws = WebSocket(self.url)
        self.ws.connect()
        self.on_state("connecting")

        interval = 41.25
        last_beat = 0.0
        acked_at = time.monotonic()
        sent_at = 0.0
        deadline = time.monotonic() + self.timeout_for_hello()
        identified = False

        while not self.stopped:
            if not identified and time.monotonic() > deadline:
                raise GatewayClosed("no hello")

            frame = self.ws.read_frame(idle_timeout=min(1.0, max(0.05, interval / 4)))
            now = time.monotonic()

            if frame is None:
                if identified and sent_at and now - max(sent_at, acked_at) > interval * 2.5:
                    raise GatewayClosed("no answer to a heartbeat")
                if identified and now - last_beat >= interval:
                    last_beat = now
                    sent_at = now
                    self._beat()
                continue

            opcode, payload = frame
            if opcode == OP_PING:
                self.ws.send(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 0
                raise self._closed(code, payload[2:].decode(errors="replace"))
            if opcode == OP_BINARY:
                continue
            if opcode != OP_TEXT:
                continue

            message = json.loads(payload.decode())
            op = message.get("op")
            data = message.get("d")
            if message.get("s") is not None:
                self.seq = message["s"]

            if op == 10:
                interval = float((data or {}).get("heartbeat_interval", 41250)) / 1000.0
                last_beat = now
                sent_at = now
                acked_at = now
                self._beat()
                if self.session_id:
                    self._resume(interval)
                else:
                    self._identify()
                identified = True
                self.on_state("up")
            elif op == 11:
                acked_at = now
                sent_at = 0.0
            elif op == 1:
                last_beat = now
                sent_at = now
                self._beat()
            elif op == 7:
                raise GatewayClosed("Discord asked for a reconnect")
            elif op == 9:
                self.session_id = None
                self.seq = None
                self.ws.send_json({"op": 2, "d": self._identify_data()})
                time.sleep(1.0)
            elif op == 0:
                event = message.get("t") or ""
                if event == "READY":
                    self.session_id = (data or {}).get("session_id")
                    self.on_state("ready")
                self.on_event(event, data or {})

        return True

    def timeout_for_hello(self) -> float:
        return 30.0

    def _identify_data(self) -> dict:
        import platform
        import sys

        system = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
        return {
            "token": self.token,
            "intents": self.intents,
            "properties": {
                "os": system,
                "browser": "vigo",
                "device": "vigo",
            },
        }

    def _identify(self) -> None:
        if self.ws:
            self.ws.send_json({"op": 2, "d": self._identify_data()})

    def _resume(self, interval: float) -> None:
        if not self.ws:
            return
        self.ws.send_json(
            {
                "op": 6,
                "d": {
                    "token": self.token,
                    "session_id": self.session_id,
                    "seq": self.seq,
                },
            }
        )
        log.info("gateway: resuming")

    def _beat(self) -> None:
        if self.ws:
            self.ws.send_json({"op": 1, "d": self.seq})

    def _closed(self, code: int, reason: str) -> Exception:
        if code in FATAL_CLOSE:
            return FatalGatewayError(f"closed with {code}: {FATAL_CLOSE[code]}")
        log.info(f"gateway: closed with {code} {reason}".rstrip())
        return GatewayClosed(f"closed with {code}")
