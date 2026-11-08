import socket
import struct
import threading
import unittest

from vigo.gateway import Gateway, GatewayClosed, WebSocket, gateway_url_ok
from tests.fake_gateway import FakeGateway


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGateway(interval_ms=120)
        self.events: list[tuple[str, dict]] = []
        self.states: list[str] = []

    def tearDown(self):
        self.gateway.stop()
        self.fake.close()

    def start(self) -> Gateway:
        self.gateway = Gateway(self.fake.url, "token", 1,
                               lambda event, data: self.events.append((event, data)),
                               self.states.append)
        threading.Thread(target=self.gateway.run, daemon=True).start()
        self.assertTrue(self.fake.wait_for(lambda: self.states.count("ready") >= 1, 5))
        return self.gateway

    def test_it_identifies_with_the_intents_it_was_given(self):
        self.start()
        self.assertTrue(self.fake.wait_for(lambda: bool(self.fake.identifies), 5))
        self.assertEqual(self.fake.identifies[0]["intents"], 1)
        self.assertEqual(self.fake.identifies[0]["token"], "token")

    def test_a_dispatch_reaches_the_handler(self):
        self.start()
        self.fake.dispatch("GUILD_CREATE", {"id": "guild_1"})
        self.assertTrue(self.fake.wait_for(
            lambda: ("GUILD_CREATE", {"id": "guild_1"}) in self.events, 5))

    def test_it_beats(self):
        self.start()
        self.assertTrue(self.fake.wait_for(lambda: len(self.fake.heartbeats) >= 2, 5))

    def test_a_drop_is_resumed_with_the_session(self):
        self.start()
        self.assertTrue(self.fake.wait_for(lambda: bool(self.fake.identifies), 5))
        self.fake.drop()
        self.assertTrue(self.fake.wait_for(lambda: bool(self.fake.resumes), 8))
        self.assertEqual(self.fake.resumes[0]["session_id"], "sess_test")

    def test_a_status_set_before_the_connection_goes_out_with_the_identify(self):
        self.gateway = Gateway(self.fake.url, "token", 1,
                               lambda event, data: None, self.states.append)
        self.gateway.set_presence("idle")
        threading.Thread(target=self.gateway.run, daemon=True).start()
        self.assertTrue(self.fake.wait_for(lambda: self.states.count("ready") >= 1, 5))
        self.assertTrue(self.fake.wait_for(lambda: bool(self.fake.presences), 5))
        self.assertEqual(self.fake.presences[0]["status"], "idle")
        self.assertEqual(self.fake.presences[0]["activities"], [])
        self.assertIsNone(self.fake.presences[0]["since"])

    def test_a_new_status_reaches_the_gateway(self):
        self.start()
        self.gateway.set_presence("dnd")
        self.assertTrue(self.fake.wait_for(lambda: bool(self.fake.presences), 5))
        self.assertEqual(self.fake.presences[-1]["status"], "dnd")

    def test_the_status_is_sent_again_after_a_drop(self):
        self.start()
        self.gateway.set_presence("online")
        self.assertTrue(self.fake.wait_for(lambda: len(self.fake.presences) >= 1, 5))
        self.fake.drop()
        self.assertTrue(self.fake.wait_for(lambda: len(self.fake.presences) >= 2, 8))
        self.assertEqual(self.fake.presences[-1]["status"], "online")


class FrameTest(unittest.TestCase):
    """A frame from the server: its length is eight bytes the server chooses."""

    def test_a_frame_past_the_limit_is_refused_before_it_is_read(self):
        ws = WebSocket("ws://127.0.0.1:1")
        near, far = socket.socketpair()
        ws.sock = near
        try:
            # A text frame that claims a terabyte, which no message holds.
            far.sendall(struct.pack("!BBQ", 0x81, 127, 1 << 40))
            with self.assertRaises(GatewayClosed) as caught:
                ws.read_frame(idle_timeout=1.0)
            self.assertIn("past the", str(caught.exception))
        finally:
            far.close()
            near.close()

    def test_a_frame_of_a_size_that_fits_is_read(self):
        ws = WebSocket("ws://127.0.0.1:1")
        near, far = socket.socketpair()
        ws.sock = near
        try:
            far.sendall(struct.pack("!BB", 0x81, 2) + b"hi")
            self.assertEqual(ws.read_frame(idle_timeout=1.0), (0x1, b"hi"))
        finally:
            far.close()
            near.close()


class HandshakeTest(unittest.TestCase):
    """The handshake answer has to prove it read this request's key."""

    def test_an_answer_without_the_accept_header_is_refused(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve() -> None:
            conn, _ = listener.accept()
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                             b"Upgrade: websocket\r\n\r\n")
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        try:
            with self.assertRaises(GatewayClosed) as caught:
                WebSocket(f"ws://127.0.0.1:{port}", timeout=2.0).connect()
            self.assertIn("does not match", str(caught.exception))
        finally:
            listener.close()

    def test_a_head_past_its_bound_is_refused(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve() -> None:
            conn, _ = listener.accept()
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n" + b"x" * (70 * 1024))
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()
        try:
            with self.assertRaises(GatewayClosed) as caught:
                WebSocket(f"ws://127.0.0.1:{port}", timeout=2.0).connect()
            self.assertIn("handshake head", str(caught.exception))
        finally:
            listener.close()


class GatewayAddressTest(unittest.TestCase):
    """The identify frame carries the token, so the address is checked first."""

    def test_discords_gateway_is_allowed(self):
        self.assertTrue(gateway_url_ok("wss://gateway.discord.gg/?v=10&encoding=json"))
        self.assertTrue(gateway_url_ok("wss://gateway-us-east1-b.discord.gg"))

    def test_a_loopback_gateway_is_allowed_for_a_local_server(self):
        self.assertTrue(gateway_url_ok("ws://127.0.0.1:4096"))
        self.assertTrue(gateway_url_ok("ws://localhost:4096"))

    def test_another_host_is_refused(self):
        for url in ("ws://attacker.example", "wss://attacker.example",
                    "wss://discord.gg.attacker.example", "wss://discord.com@attacker.example",
                    "file:///etc/passwd"):
            with self.subTest(url=url):
                self.assertFalse(gateway_url_ok(url))


if __name__ == "__main__":
    unittest.main()
