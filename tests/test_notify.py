"""Telling the owner something: the daemon's DM, and `vigo dm`."""

import io
import unittest

from vigo.__main__ import message_of
from vigo.discord_api import Discord
from vigo.notify import send_owner
from tests.fake_discord import FakeDiscord
from tests.support import TempDir, load_settings


class SendOwnerTest(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()
        self.addCleanup(self.discord.close)

    def test_it_dms_the_owner(self):
        with TempDir() as directory:
            settings = load_settings(directory)
            api = Discord("token", settings.app_id, base=self.discord.url)
            sent = send_owner(settings, "done", api=api)
        channel = self.discord.dm_channels[settings.owner]
        self.assertEqual(sent, 1)
        self.assertEqual(self.discord.messages[channel][0]["content"], "done")

    def test_a_long_message_arrives_in_pieces(self):
        with TempDir() as directory:
            settings = load_settings(directory)
            api = Discord("token", settings.app_id, base=self.discord.url)
            sent = send_owner(settings, "x" * 4000, api=api)
        channel = self.discord.dm_channels[settings.owner]
        self.assertGreater(sent, 1)
        self.assertEqual(len(self.discord.messages[channel]), sent)

    def test_nothing_to_say_is_refused_rather_than_sent(self):
        with TempDir() as directory:
            settings = load_settings(directory)
            api = Discord("token", settings.app_id, base=self.discord.url)
            with self.assertRaises(ValueError):
                send_owner(settings, "   ", api=api)
        self.assertEqual(self.discord.dm_channels, {})

    def test_it_only_mentions_nobody(self):
        with TempDir() as directory:
            settings = load_settings(directory)
            api = Discord("token", settings.app_id, base=self.discord.url)
            send_owner(settings, "hey <@everyone>", api=api)
        channel = self.discord.dm_channels[settings.owner]
        self.assertEqual(self.discord.messages[channel][0]["allowed_mentions"], {"parse": []})

    def test_components_ride_on_the_last_piece(self):
        with TempDir() as directory:
            settings = load_settings(directory)
            api = Discord("token", settings.app_id, base=self.discord.url)
            buttons = [{"type": 1, "components": []}]
            send_owner(settings, "x" * 4000, api=api, components=buttons)
        channel = self.discord.dm_channels[settings.owner]
        messages = self.discord.messages[channel]
        self.assertGreater(len(messages), 1)
        self.assertEqual(messages[0]["components"], buttons, "the newest is the last piece")
        self.assertNotIn("components", messages[-1])


class MessageOfTest(unittest.TestCase):
    def test_words_become_one_message(self):
        self.assertEqual(message_of(["all", "done"]), "all done")

    def test_piped_input_is_read_when_nothing_is_given(self):
        piped = io.StringIO("from a pipe\n")
        piped.isatty = lambda: False
        self.assertEqual(message_of([], stdin=piped), "from a pipe")

    def test_a_terminal_with_no_words_says_nothing(self):
        terminal = io.StringIO("")
        terminal.isatty = lambda: True
        self.assertEqual(message_of([], stdin=terminal), "")
