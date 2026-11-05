import json
import unittest

from vigo.allowlist import MARKER, AllowError, AllowList
from vigo.discord_api import Discord
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.support import TempDir, load_settings

OWNER = "145512144145512144"
CHANNEL = "222222222222222222"


class AllowListTest(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()

    def tearDown(self):
        self.discord.close()

    def lists(self, directory, channel=CHANNEL):
        settings = load_settings(directory, stateChannel=channel)
        api = Discord("token", settings.app_id, base=self.discord.url)
        first = State(directory / "one" / "state.json")
        second = State(directory / "two" / "state.json")
        return AllowList(settings, api, first), AllowList(settings, api, second)

    def test_nobody_is_allowed_at_first(self):
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            self.assertFalse(mac.may_ask("42"))
            self.assertEqual(mac.describe().count("nobody"), 1)

    def test_an_allow_reaches_the_other_device(self):
        with TempDir() as directory:
            mac, pc = self.lists(directory)
            said = mac.allow("42", "Alex", 3600)
            self.assertIn("allowed", said)
            self.assertTrue(pc.may_ask("42"), "the PC reads the same message")
            self.assertIn("Alex", pc.describe())

    def test_a_deny_reaches_the_other_device(self):
        with TempDir() as directory:
            mac, pc = self.lists(directory)
            mac.allow("42", "Alex", None)
            done, said = pc.deny("42")
            self.assertTrue(done)
            self.assertIn("no longer", said)
            self.assertFalse(mac.may_ask("42"))

    def test_an_allow_can_run_out(self):
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            mac.allow("42", "Alex", 0)
            self.assertFalse(mac.may_ask("42"))

    def test_the_owner_and_the_listed_need_no_allow(self):
        with TempDir() as directory:
            settings = load_settings(directory, users=["777777777777777777"])
            api = Discord("token", settings.app_id, base=self.discord.url)
            listing = AllowList(settings, api, State(directory / "s.json"))
            self.assertTrue(listing.may_ask(OWNER))
            self.assertTrue(listing.may_ask("777777777777777777"))
            self.assertFalse(listing.may_ask("9"))

    def test_the_owner_cannot_be_allowed(self):
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            with self.assertRaises(AllowError):
                mac.allow(OWNER, "me", 60)

    def test_a_listed_person_is_not_denied_here(self):
        with TempDir() as directory:
            settings = load_settings(directory, users=["777777777777777777"])
            api = Discord("token", settings.app_id, base=self.discord.url)
            listing = AllowList(settings, api, State(directory / "s.json"))
            done, said = listing.deny("777777777777777777")
            self.assertFalse(done)
            self.assertIn("file", said)

    def test_a_broken_message_stops_it_and_is_not_overwritten(self):
        with TempDir() as directory:
            broken = "333333333333333333"
            self.discord.add_message(broken, f"{MARKER}\nthis is not json")
            mac, _pc = self.lists(directory, channel=broken)
            with self.assertRaises(AllowError) as caught:
                mac.entries()
            self.assertIn("not JSON", str(caught.exception))
            self.assertFalse(mac.may_ask("42"))
            with self.assertRaises(AllowError):
                mac.allow("42", "Alex", 60)

    def test_a_channel_it_cannot_read_is_said_so(self):
        with TempDir() as directory:
            blocked = "444444444444444444"
            self.discord.forbidden.add(blocked)
            mac, _pc = self.lists(directory, channel=blocked)
            with self.assertRaises(AllowError) as caught:
                mac.allow("42", "Alex", 60)
            self.assertIn("cannot read", str(caught.exception))
            self.assertFalse(mac.may_ask("42"), "it refuses instead of allowing by accident")
            self.assertIn("cannot read", mac.describe())

    def test_a_newer_schema_is_refused_rather_than_guessed(self):
        with TempDir() as directory:
            newer = "555555555555555555"
            self.discord.add_message(newer, f"{MARKER}\n" + json.dumps({
                "v": 2, "allowed": [{"id": "42", "name": "Alex", "since": 1, "until": None}],
            }))
            mac, _pc = self.lists(directory, channel=newer)
            with self.assertRaises(AllowError) as caught:
                mac.entries()
            self.assertIn("newer vigo", str(caught.exception))
            self.assertFalse(mac.may_ask("42"), "it refuses rather than guessing")

    def test_two_allows_replace_one(self):
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            mac.allow("42", "Alex", 60)
            said = mac.allow("42", "Alex", None)
            self.assertIn("old allow", said)
            entries = mac.entries()
            self.assertEqual(len(entries), 1)
            self.assertIsNone(entries[0].until)


if __name__ == "__main__":
    unittest.main()
