import threading
import unittest

from vigo import ask as asking
from vigo.app import App
from vigo.discord_api import Discord
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings

OWNER = "145512144145512144"
CHANNEL = "chan_9"

class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()
        self.device = FakeOpenCode("macbook")
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.shutdown()
        self.device.close()
        self.discord.close()

    def make(self, directory):
        settings = load_settings(
            directory,
            devices=[{"name": "macbook", "url": self.device.url, "directory": "/tmp/project"}],
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        self.app = App(settings, api=api, state=State(directory / "state.json"))
        return self.app

    def said(self) -> list[str]:
        """Everything the bot posted: a command edits, an answer edits its own."""
        return [entry["content"] for entry in self.discord.edits + self.discord.followups]

    def button(self, app, custom: str, who: str = OWNER):
        interaction = self.discord.next_id("int")
        app.handle_interaction({
            "id": interaction,
            "token": "tok_button",
            "type": 3,
            "channel_id": "dm",
            "member": {"user": {"id": who}},
            "data": {"custom_id": custom},
        })
        return interaction

    def card(self):
        for channel, messages in self.discord.messages.items():
            for message in messages:
                if "Permission needed" in message["content"]:
                    return channel, message
        return None, None

    def test_a_tool_waits_for_the_owner(self):
        self.device.script = [
            ("tool", "bash"),
            ("permission", "bash", ["git status"]),
            ("delta", "all clean"),
            ("idle", "all clean"),
        ]
        with TempDir() as directory:
            app = self.make(directory)
            worker = asking.Ask(app, token="tok", channel_id=CHANNEL, user_id=OWNER,
                                device_name="macbook", prompt="check the tree",
                                new=False, title="Discord #general")
            thread = threading.Thread(target=worker.run_it, daemon=True)
            thread.start()

            self.assertTrue(self.device.wait_for_prompt(10))
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            channel, message = self.card()
            self.assertEqual(channel, f"dm_{OWNER}")
            self.assertIn("git status", message["content"])
            self.assertEqual(self.device.permission_replies, [], "nothing runs before the owner says so")

            components = message.get("components") or []
            self.assertEqual(len(components), 1)
            buttons = components[0]["components"]
            self.assertEqual([button["label"] for button in buttons],
                             ["Allow once", "Always allow", "Deny"])
            custom = buttons[0]["custom_id"]

            self.button(app, custom)
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.permission_replies), 10))
            session, request, decision = self.device.permission_replies[0]
            self.assertEqual(decision, "once")
            self.assertTrue(self.discord.wait_for(
                lambda: any("all clean" in str(content) for content in self.said()), 10))
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_a_stranger_cannot_answer_the_card(self):
        self.device.script = [("permission", "bash", ["git status"]), ("idle", "x")]
        with TempDir() as directory:
            app = self.make(directory)
            worker = asking.Ask(app, token="tok", channel_id=CHANNEL, user_id=OWNER,
                                device_name="macbook", prompt="go", new=True, title="t")
            threading.Thread(target=worker.run_it, daemon=True).start()
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            _channel, message = self.card()
            custom = message["components"][0]["components"][0]["custom_id"]
            self.button(app, custom, who="888888888888888888")
            self.assertEqual(self.device.permission_replies, [])
            self.button(app, custom)
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.permission_replies), 10))

    def test_an_approved_person_gets_no_saved_allow(self):
        """One use of a tool, never a standing grant the next question inherits."""
        guest = "777777777777777777"
        self.device.script = [("permission", "bash", ["git status"]), ("idle", "done")]
        with TempDir() as directory:
            settings = load_settings(
                directory, users=[guest],
                devices=[{"name": "macbook", "url": self.device.url,
                          "directory": "/tmp/project"}],
            )
            api = Discord("token", settings.app_id, base=self.discord.url)
            self.app = App(settings, api=api, state=State(directory / "state.json"))
            worker = asking.Ask(self.app, token="tok", channel_id=CHANNEL, user_id=guest,
                                device_name="macbook", prompt="go", new=True, title="t",
                                hidden=True)
            threading.Thread(target=worker.run_it, daemon=True).start()
            self.assertTrue(self.device.wait_for_prompt(10))
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            _channel, message = self.card()
            buttons = message["components"][0]["components"]
            self.assertEqual([button["label"] for button in buttons], ["Allow once", "Deny"],
                             "an approved person's tool is never saved")
            allow = buttons[0]["custom_id"]
            ticket = allow.split(":")[1]

            self.button(self.app, f"pw:{ticket}:w")
            self.assertEqual(self.device.permission_replies, [],
                             "a forged always-allow changes nothing")

            self.button(self.app, allow)
            self.assertTrue(self.discord.wait_for(
                lambda: bool(self.device.permission_replies), 10))
            self.assertEqual(self.device.permission_replies[0][2], "once")

    def test_a_deny_goes_through(self):
        self.device.script = [("permission", "bash", ["rm -rf build"]), ("idle", "stopped")]
        with TempDir() as directory:
            app = self.make(directory)
            worker = asking.Ask(app, token="tok", channel_id=CHANNEL, user_id=OWNER,
                                device_name="macbook", prompt="clean", new=True, title="t")
            threading.Thread(target=worker.run_it, daemon=True).start()
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            _channel, message = self.card()
            deny = [button for button in message["components"][0]["components"]
                    if button["label"] == "Deny"][0]
            interaction = self.button(app, deny["custom_id"])
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.permission_replies), 10))
        self.assertEqual(self.device.permission_replies[0][2], "reject")
        answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["type"], 7)
        self.assertIn("Denied", answer["data"]["content"])


    def test_when_a_dm_is_impossible_the_channel_says_only_that_approval_is_needed(self):
        self.discord.no_dm.add(OWNER)
        self.device.script = [("permission", "bash", ["git status"]), ("idle", "x")]
        with TempDir() as directory:
            app = self.make(directory)
            worker = asking.Ask(app, token="tok", channel_id=CHANNEL, user_id=OWNER,
                                device_name="macbook", prompt="check", new=True, title="t")
            threading.Thread(target=worker.run_it, daemon=True).start()
            self.assertTrue(self.discord.wait_for(
                lambda: any("could not send you a DM" in str(content)
                            for content in self.said()), 10))
        for content in self.said():
            self.assertNotIn("git status", str(content),
                             "the action and its paths never reach the channel")
        self.assertFalse(any("Permission needed" in message["content"]
                             for messages in self.discord.messages.values()
                             for message in messages))


if __name__ == "__main__":
    unittest.main()
