"""Work that an upgrade's restart cuts off, carried over the start.

An upgrade ends the daemon, and a turn the device is running outlives it: the
device kept working while vigo was down.  Only that restart writes a record,
and the new process reads it once, so the answer still reaches the channel.
"""

import threading
import time
import unittest

from vigo import commands
from vigo import ask as asking
from vigo.app import App
from vigo.discord_api import Discord
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings, question_of

OWNER = "145512144145512144"
CHANNEL = "chan_r"


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()
        self.device = FakeOpenCode("macbook")
        self.apps: list[App] = []

    def tearDown(self):
        for app in self.apps:
            app.shutdown()
        self.device.close()
        self.discord.close()

    def make(self, directory, **overrides) -> App:
        settings = load_settings(
            directory,
            devices=[{"name": "macbook", "url": self.device.url, "directory": "/tmp/project"}],
            **overrides,
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        app = App(settings, api=api, state=State(directory / "state.json"))
        self.apps.append(app)
        return app

    def use(self, app: App, sub: str, options: list | None = None,
            token: str = "tok") -> None:
        app.handle_interaction({
            "id": self.discord.next_id("int"),
            "token": token,
            "type": 2,
            "channel_id": CHANNEL,
            "channel": {"name": "general"},
            "member": {"user": {"id": OWNER}},
            "data": {"name": "vigo",
                     "options": [{"name": sub, "type": 1, "options": options or []}]},
        })

    def asked(self) -> list[str]:
        return [question_of(question) for _session, question in self.device.prompts]

    def posted(self) -> list[str]:
        """Everything the bot said, wherever Discord put it."""
        return ([entry["content"] for entry in self.discord.edits + self.discord.followups]
                + [message["content"] for message in self.discord.messages.get(CHANNEL, [])])

    def in_channel(self, said: str) -> list[str]:
        return [message["content"] for message in self.discord.messages.get(CHANNEL, [])
                if said in message["content"]]

    def kill(self, app: App) -> None:
        """Take the process down the way a restart does.

        Its runs stop, and they post nothing: a real kill reaches no line of
        the old process, so the answer of a turn it watched is the new one's
        to post, and no test may let the old one post it first.
        """
        for channel in list(app.runs):
            ask = app.runs[channel].ask
            if ask is not None:
                ask._final = lambda *args, **kwargs: None
        app.shutdown()

    # -- the record -------------------------------------------------------

    def test_only_an_upgrade_writes_the_record_down(self):
        with TempDir() as directory:
            self.device.script = [("idle", "an answer")]
            app = self.make(directory)
            self.use(app, commands.ASK, options=[{"name": "prompt", "type": 3,
                                                  "value": "one question"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("an answer" in said for said in self.posted()), 10))
            self.assertTrue(self.discord.wait_for(lambda: app.run_for(CHANNEL) is None, 5))
            self.assertEqual(app.state.data["suspended"], {},
                             "a run that ended is not work to carry over")

    def test_the_record_holds_neither_the_token_nor_the_question(self):
        """A token is the one secret of a run, and the file keeps no message text."""
        holding = threading.Event()
        self.addCleanup(holding.set)
        self.device.script = [("until", holding), ("idle", "an answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK, token="sekrit-interaction",
                     options=[{"name": "prompt", "type": 3,
                               "value": "why is the secret question red?"}])
            self.assertTrue(self.device.wait_for_prompt())
            app._suspend_runs()
            written = (directory / "state.json").read_text(encoding="utf-8")
            self.assertIn("chan_r", written, "the turn is carried")
            self.assertNotIn("sekrit-interaction", written)
            self.assertNotIn("the secret question", written)

    # -- the resume -------------------------------------------------------

    def test_a_turn_in_flight_carries_over_the_restart(self):
        running = threading.Event()
        self.addCleanup(running.set)
        self.device.script = [("until", running), ("idle", "the carried answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK, options=[{"name": "prompt", "type": 3,
                                                  "value": "first"}])
            self.assertTrue(self.device.wait_for_prompt())
            session = app.run_for(CHANNEL).session
            # A question that waits its turn never ran, so it is not carried:
            # the state file keeps no message text, and there is none to ask.
            self.use(app, commands.ASK, options=[{"name": "prompt", "type": 3,
                                                  "value": "second"}])
            self.assertTrue(any("Queued" in entry["content"]
                                for entry in self.discord.edits + self.discord.followups))
            app._suspend_runs()
            self.kill(app)

            second = self.make(directory)
            second._resume_suspended()
            resumed = second.run_for(CHANNEL)
            self.assertIsNotNone(resumed, "the turn is watching again")
            self.assertEqual(resumed.session, session)
            self.assertEqual(self.asked(), ["first"], "the prompt was not sent twice")
            self.assertEqual(second.state.data["suspended"], {},
                             "the record is given up to the new process")

            running.set()
            self.assertTrue(self.discord.wait_for(
                lambda: len(self.in_channel("the carried answer")) == 1, 10))
            self.assertTrue(self.discord.wait_for(lambda: second.run_for(CHANNEL) is None, 5))
        self.assertEqual(self.asked(), ["first"],
                         "a question that never reached the device is not carried")
        self.assertEqual(self.in_channel("the carried answer")[0].count("the carried answer"), 1,
                         "the answer is posted once")
        self.assertEqual(self.discord.followups, [],
                         "the interaction is gone, so the channel carries the answer")

    def test_a_turn_that_ended_while_the_daemon_was_down_is_answered(self):
        finishing = threading.Event()
        self.addCleanup(finishing.set)
        self.device.script = [("until", finishing), ("idle", "the finished answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK, options=[{"name": "prompt", "type": 3,
                                                  "value": "long work"}])
            self.assertTrue(self.device.wait_for_prompt())
            session = app.run_for(CHANNEL).session
            app._suspend_runs()
            self.kill(app)
            finishing.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any(message.get("type") == "idle"
                            for message in self.device.messages.get(session, [])), 10))

            second = self.make(directory)
            second._resume_suspended()
            self.assertTrue(self.discord.wait_for(
                lambda: len(self.in_channel("the finished answer")) == 1, 10))
            self.assertTrue(self.discord.wait_for(lambda: second.run_for(CHANNEL) is None, 5))
        self.assertEqual(self.asked(), ["long work"], "the turn was read, not asked again")

    def test_the_answer_holds_no_turn_from_before_the_prompt(self):
        """The answer is the turn's own text, not everything the session said.

        The time filter cannot tell a message the person asked a moment ago
        from one this turn wrote, and a restart makes the two meet.
        """
        with TempDir() as directory:
            self.device.script = [("idle", "the new answer")]
            app = self.make(directory)
            session = self.device.new_session("Discord #general")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            self.device.add_message(session, "assistant", content=[
                {"type": "text", "text": "the old answer"},
            ])
            self.use(app, commands.ASK, options=[{"name": "prompt", "type": 3,
                                                  "value": "why is it red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the new answer" in said for said in self.posted()), 10))
        said = "\n".join(self.posted())
        self.assertIn("the new answer", said)
        self.assertNotIn("the old answer", said)

    def test_an_idle_from_the_turn_before_does_not_end_the_run(self):
        """A session holds the idle of every turn, and only this one's is the end."""
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            before = self.device.add_message(session, "idle")
            self.device.add_message(session, "user", text="the next question")
            self.device.add_message(session, "assistant", content=[
                {"type": "text", "text": "working"},
            ])
            worker = asking.Ask(app, token="tok", channel_id=CHANNEL, user_id=OWNER,
                                device_name="macbook", prompt="q", new=False, title="t")
            worker.before = str(before["id"])
            self.assertFalse(worker._is_idle(session), "the earlier idle is not this end")
            self.device.add_message(session, "idle")
            self.assertTrue(worker._is_idle(session))

    def test_a_carried_turn_for_a_device_that_left_the_file_is_dropped(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.suspend_runs({"chan_elsewhere": {
                "at": time.time(),
                "run": {"device": "gone", "user_id": OWNER, "session": "ses_x",
                        "prompted_at": 1.0, "before": "msg_1"},
            }})
            app = self.make(directory)
            app._resume_suspended()
            self.assertIsNone(app.run_for("chan_elsewhere"))
            self.assertEqual(app.state.data["suspended"], {})
        self.assertEqual(self.discord.messages, {}, "nothing is posted for it")

    def test_a_record_that_waited_past_its_restart_is_not_resumed(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.suspend_runs({"chan_elsewhere": {
                "at": time.time() - 7200,
                "run": {"device": "macbook", "user_id": OWNER, "session": "ses_x",
                        "prompted_at": 1.0, "before": "msg_1"},
            }})
            app = self.make(directory)
            app._resume_suspended()
            self.assertIsNone(app.run_for("chan_elsewhere"))
        self.assertEqual(self.asked(), [])
        self.assertEqual(self.discord.messages, {})

    def test_a_record_with_no_prompt_on_the_device_is_not_picked_up(self):
        """Only a turn the device already has can be watched after a restart."""
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.suspend_runs({"chan_elsewhere": {
                "at": time.time(),
                "run": {"device": "macbook", "user_id": OWNER, "session": "",
                        "prompted_at": 0.0, "before": ""},
            }})
            app = self.make(directory)
            app._resume_suspended()
            self.assertIsNone(app.run_for("chan_elsewhere"))
        self.assertEqual(self.asked(), [])
        self.assertEqual(self.discord.messages, {})


if __name__ == "__main__":
    unittest.main()
