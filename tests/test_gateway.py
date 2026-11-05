import threading
import unittest

from vigo.gateway import Gateway
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


if __name__ == "__main__":
    unittest.main()
