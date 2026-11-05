import json
import unittest

from vigo import __version__
from vigo.allowlist import AllowList
from vigo.discord_api import Discord
from vigo.presence import MARKER, Presence
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.support import TempDir, load_settings

CHANNEL = "222222222222222222"


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()

    def tearDown(self):
        self.discord.close()

    def daemon(self, directory, name):
        settings = load_settings(
            directory,
            device=name,
            stateChannel=CHANNEL,
            devices=[
                {"name": "macbook", "url": "http://127.0.0.1:1", "directory": "/a"},
                {"name": "pc", "url": "http://127.0.0.1:2", "directory": "/b"},
            ],
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        state = State(directory / name / "state.json")
        return Presence(name, api, state, AllowList(settings, api, state).home)

    def test_each_daemon_sees_the_other(self):
        with TempDir() as directory:
            mac = self.daemon(directory, "macbook")
            pc = self.daemon(directory, "pc")
            mac.announce()
            pc.announce()
            seen = mac.read()
        self.assertEqual(set(seen), {"macbook", "pc"})
        self.assertEqual(seen["macbook"]["version"], __version__)

    def test_an_older_build_is_named_but_not_my_own(self):
        with TempDir() as directory:
            mac = self.daemon(directory, "macbook")
            mac.announce()
            # Rewrite the message as if the Mac had seen the PC on an old build.
            message_id = self.discord.messages[CHANNEL][0]["id"]
            api = Discord("token", "111111111111111111", base=self.discord.url)
            api.edit(CHANNEL, message_id, {"content": f"{MARKER}\n" + json.dumps({
                "v": 1,
                "daemons": {
                    "macbook": {"version": __version__, "at": 1},
                    "pc": {"version": "0.9.0", "at": 1},
                },
            })})
            stale = mac.stale()
        self.assertEqual(len(stale), 1)
        self.assertIn("pc", stale[0])
        self.assertIn("0.9.0", stale[0])

    def test_nothing_is_said_when_both_match(self):
        with TempDir() as directory:
            mac = self.daemon(directory, "macbook")
            mac.announce()
            self.daemon(directory, "pc").announce()
            stale = mac.stale()
        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
