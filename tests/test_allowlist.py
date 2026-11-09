import json
import unittest

from vigo.allowlist import MARKER, PAGE, PAGES, AllowError, AllowList
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

    def test_a_list_shaped_message_from_somebody_else_grants_nothing(self):
        """The list is a message, so only the bot's own message may be read.

        A channel where somebody else can post would otherwise let them write
        an allow for themselves and have the daemon believe it.
        """
        with TempDir() as directory:
            mac, pc = self.lists(directory)
            mac.allow("42", "Alex", None)
            forged = {"v": 1, "allowed": [{"id": "999", "name": "Nobody",
                                           "since": 1, "until": None}]}
            self.discord.add_message(CHANNEL, f"{MARKER}\n{json.dumps(forged)}",
                                     author="999", bot=False)
            self.assertFalse(pc.may_ask("999"), "the other device's state is empty")
            self.assertTrue(pc.may_ask("42"), "the bot's own message is still the list")
            self.assertNotIn("Nobody", pc.describe())

    def test_a_forged_message_alone_leaves_nobody_allowed(self):
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            forged = {"v": 1, "allowed": [{"id": "999", "name": "Nobody",
                                           "since": 1, "until": None}]}
            self.discord.add_message(CHANNEL, f"{MARKER}\n{json.dumps(forged)}",
                                     author="999", bot=False)
            self.assertFalse(mac.may_ask("999"))
            self.assertIn("nobody", mac.describe())

    def test_a_forged_message_is_not_overwritten(self):
        """The bot posts a new list rather than editing somebody else's message."""
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            forged = {"v": 1, "allowed": [{"id": "999", "name": "Nobody",
                                           "since": 1, "until": None}]}
            message = self.discord.add_message(
                CHANNEL, f"{MARKER}\n{json.dumps(forged)}", author="999", bot=False)
            mac.allow("42", "Alex", None)
            self.assertEqual(message["content"], f"{MARKER}\n{json.dumps(forged)}",
                             "the stranger's message is left alone")
            self.assertTrue(mac.may_ask("42"))

    def test_a_flood_does_not_hide_the_list(self):
        """A busy state channel must not turn the list into a new, empty one."""
        with TempDir() as directory:
            mac, pc = self.lists(directory)
            mac.allow("42", "Alex", None)
            for _ in range(120):
                self.discord.add_message(CHANNEL, "a stranger's line", author="999")
            self.assertTrue(pc.may_ask("42"), "the list is found past the newest page")
            self.assertEqual(pc.describe().count("Alex"), 1)

    def test_a_list_older_than_the_walk_is_not_replaced(self):
        """A channel too long to read to its start keeps its list, if it has one."""
        with TempDir() as directory:
            mac, _pc = self.lists(directory)  # a state channel: PAGES * PAGE
            for _ in range(PAGES * PAGE + 1):
                self.discord.add_message(CHANNEL, "not the list", bot=True)
            with self.assertRaises(AllowError) as caught:
                mac.allow("42", "Alex", None)
            self.assertIn("may be older", str(caught.exception))
            self.assertEqual(
                [message for message in self.discord.messages[CHANNEL]
                 if message["content"].startswith(MARKER)],
                [], "nothing is written")

    def test_a_bot_line_does_not_hide_the_absence_of_a_list(self):
        """The walk reaches the first message, so no list is a fact, not a guess."""
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            self.discord.add_message(CHANNEL, "the bot's first line", bot=True)
            said = mac.allow("42", "Alex", None)
            self.assertIn("allowed", said)
            self.assertTrue(mac.may_ask("42"))

    def test_a_dm_the_bot_wrote_in_still_gets_its_first_list(self):
        """The bot answers and sends cards in the owner's DM, and none of those
        is the list.  The first allow there must still create one."""
        with TempDir() as directory:
            settings = load_settings(directory)
            api = Discord("token", settings.app_id, base=self.discord.url)
            listing = AllowList(settings, api, State(directory / "state.json"))
            channel = api.open_dm(listing.owner)
            self.discord.add_message(channel, "an answer the bot posted", bot=True)
            said = listing.allow("42", "Baja Blast", 30 * 86400)
            self.assertIn("allowed", said)
            self.assertTrue(listing.may_ask("42"))

    def test_a_dm_is_read_past_the_walk_of_a_state_channel(self):
        """The bot's DM holds a card and an answer for every turn, so its list
        must be found deeper than a state channel's, not called missing."""
        with TempDir() as directory:
            settings = load_settings(directory)
            api = Discord("token", settings.app_id, base=self.discord.url)
            channel = api.open_dm(settings.owner)
            stored = {"v": 1, "allowed": [
                {"id": "42", "name": "Alex", "since": 1, "until": None}]}
            self.discord.add_message(channel, f"{MARKER}\n{json.dumps(stored)}", bot=True)
            for _ in range(PAGES * PAGE + 5):
                self.discord.add_message(channel, "an answer the bot posted", bot=True)
            fresh = AllowList(settings, api, State(directory / "fresh.json"))
            self.assertTrue(fresh.may_ask("42"),
                            "the DM is walked past a state channel's bound")

    def test_one_decision_reads_the_list_once(self):
        """Every command and every DM reads this list: one GET, not two."""
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            mac.allow("42", "Alex", None)
            with self.discord.lock:
                self.discord.gets.clear()
            self.assertTrue(mac.may_ask("42"))
            with self.discord.lock:
                reads = [path for path in self.discord.gets if "/messages/" in path]
            self.assertEqual(len(reads), 1, reads)


        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            mac.allow("42", "Alex", 60)
            said = mac.allow("42", "Alex", None)
            self.assertIn("old allow", said)
            entries = mac.entries()
            self.assertEqual(len(entries), 1)
            self.assertIsNone(entries[0].until)

    def test_without_a_state_channel_the_list_lives_in_the_bot_dm(self):
        with TempDir() as directory:
            settings = load_settings(directory)
            api = Discord("token", settings.app_id, base=self.discord.url)
            listing = AllowList(settings, api, State(directory / "state.json"))
            listing.allow("42", "Alex", None)
            channel = self.discord.dm_channels[listing.owner]
            stored = self.discord.messages.get(channel, [])
        self.assertTrue(any(message["content"].startswith(MARKER) for message in stored),
                        "the list is a message the bot owns in its DM with the owner")
        self.assertEqual(settings.state_channel, "", "the file names none")

    def test_the_stored_message_holds_the_list_and_no_free_text(self):
        """The state message is one marker and one JSON object, so a reader
        cannot mistake a chat line for an entry."""
        with TempDir() as directory:
            mac, _pc = self.lists(directory)
            mac.allow("42", "Alex", 60)
            stored = self.discord.messages[CHANNEL]
        content = next(message["content"] for message in stored
                       if message["content"].startswith(MARKER))
        head, raw = content.split("\n", 1)
        self.assertEqual(head, MARKER)
        data = json.loads(raw)
        self.assertEqual(sorted(data), ["allowed", "v"])
        self.assertEqual(data["v"], 1)
        self.assertEqual(sorted(data["allowed"][0]), ["id", "name", "since", "until"])



if __name__ == "__main__":
    unittest.main()
