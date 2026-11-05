"""A Discord gateway on 127.0.0.1, for tests: handshake, hello, identify, resume."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        piece = sock.recv(size - len(data))
        if not piece:
            raise ConnectionError("closed")
        data += piece
    return data


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    head = _read_exact(sock, 2)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if masked else b""
    payload = _read_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _send_frame(sock: socket.socket, payload: bytes, opcode: int = 1) -> None:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    sock.sendall(bytes(header) + payload)


class FakeGateway:
    def __init__(self, interval_ms: int = 120) -> None:
        self.interval_ms = interval_ms
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self.identifies: list[dict] = []
        self.resumes: list[dict] = []
        self.heartbeats: list[int | None] = []
        self.connection: socket.socket | None = None
        self.closed = False
        self.session_id = "sess_test"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        with self.lock:
            if self.connection:
                try:
                    self.connection.close()
                except OSError:
                    pass

    def dispatch(self, event: str, data: dict) -> None:
        self._send({"op": 0, "t": event, "s": 1, "d": data})

    def drop(self) -> None:
        """Close the connection as Discord does when it wants a resume."""
        with self.lock:
            connection = self.connection
        if connection:
            try:
                connection.close()
            except OSError:
                pass

    def _send(self, payload: dict) -> None:
        with self.lock:
            connection = self.connection
        if connection is None:
            raise ConnectionError("no connection")
        _send_frame(connection, json.dumps(payload).encode())

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _serve(self) -> None:
        while not self.closed:
            try:
                connection, _address = self.sock.accept()
            except OSError:
                return
            with self.lock:
                self.connection = connection
            thread = threading.Thread(target=self._speak, args=(connection,), daemon=True)
            thread.start()

    def _speak(self, connection: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            piece = connection.recv(4096)
            if not piece:
                return
            head += piece
        key = ""
        for line in head.decode(errors="replace").split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-key":
                key = value.strip()
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        _send_frame(connection, json.dumps(
            {"op": 10, "d": {"heartbeat_interval": self.interval_ms}}
        ).encode())
        try:
            while not self.closed:
                opcode, payload = _read_frame(connection)
                if opcode == 8:
                    return
                if opcode != 1:
                    continue
                message = json.loads(payload)
                op = message.get("op")
                if op == 2:
                    with self.lock:
                        self.identifies.append(message.get("d") or {})
                    _send_frame(connection, json.dumps({
                        "op": 0, "t": "READY", "s": 1,
                        "d": {"session_id": self.session_id,
                              "user": {"id": "999", "username": "vigo"},
                              "application": {"id": "999"}},
                    }).encode())
                elif op == 6:
                    with self.lock:
                        self.resumes.append(message.get("d") or {})
                    _send_frame(connection, json.dumps({"op": 0, "t": "RESUMED", "s": 2, "d": {}}).encode())
                elif op == 1:
                    with self.lock:
                        self.heartbeats.append(message.get("d"))
                    _send_frame(connection, json.dumps({"op": 11, "d": None}).encode())
        except (ConnectionError, OSError):
            return
