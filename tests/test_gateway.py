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


if __name__ == "__main__":
    unittest.main()
