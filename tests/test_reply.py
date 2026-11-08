"""A direct message that replies to one of vigo's messages is an ask."""

import dataclasses
import threading
import unittest

from vigo import commands
from vigo.app import INTENTS, App
from vigo.discord_api import Discord
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings, question_of

OWNER = "145512144145512144"
STRANGER = "888888888888888888"
DM = "dm_1"


class ReplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.discord = FakeDiscord()
        self.device = FakeOpenCode("macbook")
        self.directory = TempDir()
        self.app: App | None = None

    def tearDown(self) -> None:
        if self.app is not None:
            self.discord.wait_for(lambda: not self.app.runs, 5)
            self.app.shutdown()
        self.directory.__exit__(None, None, None)
        self.device.close()
        self.discord.close()

    def make(self, **overrides) -> App:
        settings = load_settings(
            self.directory.path,
            devices=[{"name": "macbook", "url": self.device.url, "directory": "/tmp/project"}],
            **overrides,
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        self.app = App(settings, api=api, state=State(self.directory.path / "state.json"))
        return self.app

    def said(self) -> list[str]:
        posted = [message["content"] for message in self.discord.messages.get(DM, [])]
        posted += [entry["content"] or "" for entry in self.discord.followups]
        return posted

    def ask(self, app: App, text: str = "why is CI red?") -> None:
        app.handle_interaction({
            "id": self.discord.next_id("int"),
            "token": "tok",
            "type": 2,
            "channel_id": DM,
            "member": {"user": {"id": OWNER}},
            "data": {"name": "vigo", "options": [
                {"name": commands.ASK, "type": 1, "options": [
                    {"name": "prompt", "type": 3, "value": text},
                ]},
            ]},
        })

    def reply(self, app: App, to: str, *, who: str = OWNER, text: str = "and then?",
              attachments: list | None = None) -> None:
        app.handle_message({
            "id": self.discord.next_id("msg"),
            "channel_id": DM,
            "content": text,
            "author": {"id": who, "bot": False},
            "message_reference": {"message_id": to},
            **({"attachments": attachments} if attachments else {}),
        })

    def last_answer(self, app: App) -> str:
        return next(reversed(app.state.data["answers"]))

    def settle(self, app: App) -> None:
        """Wait for the command's run to close before replying to its answer."""
        self.assertTrue(self.discord.wait_for(lambda: not app.runs, 10),
                        "the first run ended")

    def wait_for(self, text: str) -> bool:
        return self.discord.wait_for(
            lambda: any(text in said for said in self.said()), 10)

    def test_the_intent_is_direct_messages_only(self):
        self.assertEqual(INTENTS, 1 << 12)

    def test_a_reply_to_an_answer_asks_and_answers_in_the_dm(self):
        self.device.script = [("idle", "the first answer")]
        app = self.make()
        self.ask(app)
        self.assertTrue(self.wait_for("the first answer"))
        self.settle(app)
        self.device.script = [("idle", "the second answer")]
        self.reply(app, self.last_answer(app))
        self.assertTrue(self.wait_for("the second answer"))
        self.assertEqual([question_of(asked) for _session, asked in self.device.prompts],
                         ["why is CI red?", "and then?"])
        posted = [message["content"] for message in self.discord.messages[DM]]
        self.assertTrue(any("the second answer" in content for content in posted),
                        "the answer is a message in the DM, not an edit")
        acks = sum(said.startswith("Got it") or ("Queued behind" in said)
                   for said in self.said())
        self.assertEqual(acks, 1)

    def test_a_queued_reply_says_it_still_runs_in_its_own_line(self):
        """Past `timeout` the note ends the queued message, and no second message arrives."""
        first, second = threading.Event(), threading.Event()
        self.addCleanup(first.set)
        self.addCleanup(second.set)
        self.device.script = [("idle", "the first answer")]
        app = self.make()
        app.config = dataclasses.replace(app.config, timeout=0.2)
        self.ask(app)
        self.assertTrue(self.wait_for("the first answer"))
        self.settle(app)
        self.device.script = [("until", first), ("idle", "the second answer")]
        self.reply(app, self.last_answer(app), text="the second question")
        self.assertTrue(self.discord.wait_for(lambda: len(self.device.prompts) == 2, 10))
        self.device.script = [("until", second), ("idle", "the third answer")]
        self.reply(app, self.last_answer(app), text="the third question")
        queued = next((message["content"] for message in self.discord.messages[DM]
                       if message["content"].startswith("Queued behind 1 question")), "")
        self.assertTrue(queued, "the second question waits in the line")
        note = "Still running on the device. `/vigo stop` ends it."
        first.set()
        self.assertTrue(self.discord.wait_for(
            lambda: any(content == f"{queued}\n\n{note}" for content in self.said()), 10),
            "the queued message grows the note at its end")
        self.assertFalse([said for said in self.said() if said.strip() == note],
                         "the note is not a message of its own")
        second.set()
        self.assertTrue(self.wait_for("the third answer"))

    def test_a_reply_that_is_only_a_file_asks_about_it(self):
        """A log dropped without words is the question: read this."""
        self.device.script = [("idle", "the first answer")]
        app = self.make()
        self.ask(app)
        self.assertTrue(self.wait_for("the first answer"))
        self.settle(app)
        self.device.script = [("idle", "the log shows it")]
        self.reply(app, self.last_answer(app), text="", attachments=[{
            "id": "att_1", "filename": "010015100B514000.log", "size": 136458,
            "url": "https://cdn.discordapp.com/attachments/1/2/log?ex=signed",
        }])
        self.assertTrue(self.wait_for("the log shows it"))
        told = self.device.prompts[-1][1]
        self.assertIn("“010015100B514000.log”, 133 KB", told)
        self.assertIn("https://cdn.discordapp.com/attachments/1/2/log?ex=signed", told)
        self.assertIn("curl -L -o /tmp/vigo-attached-att_1", told)

    def test_a_stranger_gets_no_answer_about_a_file(self):
        self.device.script = [("idle", "the first answer")]
        app = self.make()
        self.ask(app)
        self.assertTrue(self.wait_for("the first answer"))
        self.settle(app)
        asked = len(self.device.prompts)
        self.reply(app, self.last_answer(app), who=STRANGER, text="", attachments=[{
            "id": "att_1", "filename": "x.log", "size": 1,
            "url": "https://cdn.discordapp.com/attachments/1/2/x.log",
        }])
        self.assertEqual(len(self.device.prompts), asked, "no device took it")
        self.assertTrue(any("approved" in said for said in self.said()))

    def test_a_long_answer_to_a_reply_arrives_whole(self):
        """The answer the owner reads in a DM is not cut mid-sentence."""
        self.device.script = [("idle", "the first answer")]
        app = self.make()
        self.ask(app)
        self.assertTrue(self.wait_for("the first answer"))
        self.settle(app)
        self.device.script = [("idle", "y" * 4000)]
        self.reply(app, self.last_answer(app))
        self.assertTrue(self.discord.wait_for(
            lambda: sum(len(message["content"]) for message in self.discord.messages[DM]
                        if set(message["content"]) == {"y"}) >= 4000, 10))
        self.settle(app)
        posted = [message["content"] for message in self.discord.messages[DM]
                  if set(message["content"]) == {"y"}]
        self.assertGreater(len(posted), 1, "the answer takes more than one message")
        self.assertEqual("".join(posted), "y" * 4000, "the DM holds every word of it")

    def test_a_group_dm_answer_is_scrubbed_like_a_channel(self):
        """A group DM has several readers, so it is not the asker's alone."""
        group = "group_1"
        self.discord.channel_types[group] = 3
        app = self.make()
        app.state.remember_answer("old", group)
        token = "M" * 25 + ".GaBcDe." + "a" * 27
        self.device.script = [("idle", f"the token is {token}")]
        app.handle_message({
            "id": self.discord.next_id("msg"),
            "channel_id": group,
            "content": "and then?",
            "author": {"id": OWNER, "bot": False},
            "message_reference": {"message_id": "old"},
        })
        self.assertTrue(self.discord.wait_for(
            lambda: any("redacted: Discord token" in message["content"]
                        for message in self.discord.messages.get(group, [])), 10))
        self.assertIn("everyone in this conversation sees the answer",
                      self.device.prompts[-1][1])

    def test_a_one_to_one_dm_answer_keeps_what_the_asker_asked_about(self):
        self.discord.channel_types[DM] = 1
        app = self.make()
        app.state.remember_answer("old", DM)
        token = "M" * 25 + ".GaBcDe." + "a" * 27
        self.device.script = [("idle", f"the token is {token}")]
        app.handle_message({
            "id": self.discord.next_id("msg"),
            "channel_id": DM,
            "content": "and then?",
            "author": {"id": OWNER, "bot": False},
            "message_reference": {"message_id": "old"},
        })
        self.assertTrue(self.discord.wait_for(
            lambda: any(token in message["content"]
                        for message in self.discord.messages.get(DM, [])), 10))
        self.assertIn("only the asker sees the answer", self.device.prompts[-1][1])

    def test_a_reply_meets_the_guest_share_too(self):
        """A reply is a question, so it is not a way around the cap."""
        guest = "777777777777777777"
        app = self.make(users=[guest], guests={"share": 0})
        app.state.remember_answer("old", DM)
        self.reply(app, "old", who=guest)
        self.assertTrue(self.discord.wait_for(
            lambda: any("paused" in said for said in self.said()), 5))
        self.assertEqual(self.device.prompts, [])

    def test_a_plain_dm_is_ignored(self):
        app = self.make()
        app.handle_message({
            "id": self.discord.next_id("msg"),
            "channel_id": DM,
            "content": "hello",
            "author": {"id": OWNER, "bot": False},
        })
        self.assertEqual(app.waiting, {})
        self.assertEqual(self.device.prompts, [])

    def test_a_reply_to_a_non_vigo_message_is_ignored(self):
        app = self.make()
        theirs = self.discord.add_message(DM, "their own words", bot=False)
        self.reply(app, theirs["id"])
        self.assertEqual(app.waiting, {})
        self.assertEqual(self.device.prompts, [])

    def test_a_stranger_is_refused(self):
        self.device.script = [("idle", "the answer")]
        app = self.make()
        self.ask(app)
        self.assertTrue(self.wait_for("the answer"))
        self.settle(app)
        self.reply(app, self.last_answer(app), who=STRANGER)
        self.assertTrue(self.discord.wait_for(
            lambda: any("may use it" in said for said in self.said()), 5))
        self.assertEqual(len(self.device.prompts), 1, "the stranger's question did not run")


class TwoDaemonsReplyTest(unittest.TestCase):
    """Both daemons see the DM, but only the one that posted the message acts."""

    def setUp(self) -> None:
        self.device = FakeOpenCode("macbook")
        self.mac = FakeDiscord()
        self.pc = FakeDiscord()
        self.directory = TempDir()
        self.apps: list[tuple[App, FakeDiscord]] = []

    def tearDown(self) -> None:
        for app, discord in self.apps:
            discord.wait_for(lambda: not app.runs, 5)
            app.shutdown()
        self.directory.__exit__(None, None, None)
        self.device.close()
        self.mac.close()
        self.pc.close()

    def app(self, name: str, discord: FakeDiscord) -> App:
        settings = load_settings(
            self.directory.path,
            device=name,
            devices=[{"name": name, "url": self.device.url, "directory": "/tmp/project"}],
        )
        api = Discord("token", settings.app_id, base=discord.url)
        app = App(settings, api=api, state=State(self.directory.path / f"{name}.json"))
        self.apps.append((app, discord))
        return app

    def test_only_the_daemon_that_posted_the_answer_answers_the_reply(self):
        self.device.script = [("idle", "the first answer")]
        mac = self.app("macbook", self.mac)
        pc = self.app("pc", self.pc)
        mac.handle_interaction({
            "id": self.mac.next_id("int"),
            "token": "tok",
            "type": 2,
            "channel_id": DM,
            "member": {"user": {"id": OWNER}},
            "data": {"name": "vigo", "options": [
                {"name": commands.ASK, "type": 1, "options": [
                    {"name": "prompt", "type": 3, "value": "why is CI red?"},
                ]},
            ]},
        })
        self.assertTrue(self.mac.wait_for(
            lambda: any("the first answer" in (entry.get("content") or "")
                        for entry in self.mac.followups), 10))
        answer = next(reversed(mac.state.data["answers"]))
        self.assertTrue(self.mac.wait_for(lambda: not mac.runs, 10), "the first run ended")
        self.device.script = [("idle", "the second answer")]
        payload = {
            "id": self.mac.next_id("msg"),
            "channel_id": DM,
            "content": "and then?",
            "author": {"id": OWNER, "bot": False},
            "message_reference": {"message_id": answer},
        }
        pc.handle_message(payload)
        mac.handle_message(payload)
        self.assertTrue(self.mac.wait_for(
            lambda: any("the second answer" in message["content"]
                        for message in self.mac.messages.get(DM, [])), 10))
        self.assertEqual(len(self.device.prompts), 2, "one device turn for the reply")
        self.assertEqual([], self.pc.messages.get(DM, []), "the other daemon says nothing")
