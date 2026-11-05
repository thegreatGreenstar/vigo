import threading
import time
import unittest

from vigo.app import App
from vigo.discord_api import Discord
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings

OWNER = "145512144145512144"
CHANNEL = "chan_7"


class ElectionTest(unittest.TestCase):
    """Two daemons, one bot: only the device's own daemon answers."""

    def setUp(self):
        self.discord = FakeDiscord()
        self.mac = FakeOpenCode("macbook")
        self.pc = FakeOpenCode("pc")
        self.mac.script = [("delta", "from the mac"), ("idle", "from the mac")]

    def tearDown(self):
        for app in getattr(self, "made", []):
            app.shutdown()
        self.pc.close()
        self.mac.close()
        self.discord.close()

    def apps(self, directory):
        devices = [
            {"name": "macbook", "url": self.mac.url, "directory": "/mac"},
            {"name": "pc", "url": self.pc.url, "directory": "/pc"},
        ]
        made = []
        for name in ("macbook", "pc"):
            settings = load_settings(directory / name, device=name, devices=devices)
            api = Discord("token", settings.app_id, base=self.discord.url)
            made.append(App(settings, api=api, state=State(directory / name / "state.json")))
        self.made = made
        return made

    def payload(self, interaction: str):
        return {
            "id": interaction,
            "token": "tok_election",
            "type": 2,
            "channel_id": CHANNEL,
            "channel": {"name": "general"},
            "member": {"user": {"id": OWNER}},
            "data": {"name": "vigo", "options": [
                {"name": "ask", "type": 1, "options": [
                    {"name": "prompt", "type": 3, "value": "which one"},
                ]},
            ]},
        }

    def test_only_one_daemon_answers(self):
        session = self.mac.new_session()
        self.mac.add_message(session, "user", text="recent")
        with TempDir() as directory:
            mac_app, pc_app = self.apps(directory)
            interaction = self.discord.next_id("int")
            payload = self.payload(interaction)
            threads = [
                threading.Thread(target=mac_app.handle_interaction, args=(payload,)),
                threading.Thread(target=pc_app.handle_interaction, args=(payload,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertTrue(self.discord.wait_for(
                lambda: any("from the mac" in str(entry["content"])
                            for entry in self.discord.edits + self.discord.followups), 10))
        acks = [reply for reply in self.discord.responses if reply["id"] == interaction]
        self.assertEqual(len(acks), 1, "Discord acknowledged the use once")
        self.assertEqual(len(self.mac.prompts), 1)
        self.assertEqual(len(self.pc.prompts), 0)

    def test_the_other_daemon_takes_over_without_it(self):
        session = self.mac.new_session()
        self.mac.add_message(session, "user", text="recent")
        with TempDir() as directory:
            _mac_app, pc_app = self.apps(directory)
            interaction = self.discord.next_id("int")
            started = time.monotonic()
            pc_app.handle_interaction(self.payload(interaction))
            waited = time.monotonic() - started
            self.assertTrue(self.discord.wait_for(lambda: bool(self.mac.prompts), 10))
        self.assertLess(waited, 1.0, "the answer is acknowledged before a device is probed")
        self.assertEqual(len(self.mac.prompts), 1)


if __name__ == "__main__":
    unittest.main()
