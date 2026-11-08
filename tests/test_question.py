import threading
import time
import unittest

from vigo import ask as asking
from vigo.app import App
from vigo.discord_api import Discord
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings

OWNER = "145512144145512144"
STRANGER = "888888888888888888"
CHANNEL = "chan_q"


def field(key: str, title: str, description: str = "Which one?") -> dict:
    return {
        "key": key,
        "title": title,
        "description": description,
        "type": "string",
        "custom": True,
        "options": [
            {"value": "Yes", "label": "Yes"},
            {"value": "No", "label": "No"},
        ],
    }


class QuestionTest(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()
        self.device = FakeOpenCode("macbook")
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.shutdown()
        self.device.close()
        self.discord.close()

    def make(self, directory, **overrides):
        settings = load_settings(
            directory,
            devices=[{"name": "macbook", "url": self.device.url, "directory": "/tmp/project"}],
            **overrides,
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        self.app = App(settings, api=api, state=State(directory / "state.json"))
        return self.app

    def worker(self, app: App, user_id: str = OWNER, **extra) -> asking.Ask:
        return asking.Ask(app, token="tok", channel_id=CHANNEL, user_id=user_id,
                          device_name="macbook", prompt="go", new=False, title="t",
                          **extra)

    def said(self) -> list[str]:
        return [entry["content"] for entry in self.discord.edits + self.discord.followups]

    def card(self):
        for channel, messages in self.discord.messages.items():
            for message in messages:
                if "Question from the agent" in message["content"]:
                    return channel, message
        return None, None

    def click(self, app: App, custom: str, message: dict,
              who: str = OWNER, values: list[str] | None = None):
        interaction = self.discord.next_id("int")
        app.handle_interaction({
            "id": interaction,
            "token": "tok_button",
            "type": 3,
            "channel_id": f"dm_{OWNER}",
            "member": {"user": {"id": who}},
            "message": message,
            "data": {"custom_id": custom, "values": values or []},
        })
        return interaction

    def submit(self, app: App, custom: str, value: str, message: dict,
               who: str = OWNER):
        interaction = self.discord.next_id("int")
        app.handle_interaction({
            "id": interaction,
            "token": "tok_modal",
            "type": 5,
            "channel_id": f"dm_{OWNER}",
            "member": {"user": {"id": who}},
            "message": message,
            "data": {
                "custom_id": custom,
                "components": [{"type": 1, "components": [
                    {"type": 4, "custom_id": "answer", "value": value},
                ]}],
            },
        })
        return interaction

    def start(self, app: App, **extra):
        thread = threading.Thread(target=self.worker(app, **extra).run_it, daemon=True)
        thread.start()
        return thread

    def test_a_question_past_the_token_posts_its_answer_in_the_channel(self):
        """The clock runs from the interaction, so a run the queue held is late."""
        self.device.script = [("idle", "the answer after the wait")]
        with TempDir() as directory:
            app = self.make(directory)
            self.start(app, asked_at=time.monotonic() - asking.LIFE - 60)
            self.assertTrue(self.discord.wait_for(
                lambda: any("the answer after the wait" in message["content"]
                            for message in self.discord.messages.get(CHANNEL, [])), 10))
        self.assertEqual(self.discord.followups, [], "a dead token is not tried")
        self.assertEqual(self.discord.dm_channels, {},
                         "the channel takes a public answer, so no DM is opened")

    def test_a_hidden_question_past_the_token_is_dmed_without_trying_it(self):
        self.device.script = [("idle", "still private")]
        with TempDir() as directory:
            app = self.make(directory)
            self.start(app, hidden=True,
                       asked_at=time.monotonic() - asking.LIFE - 60)
            self.assertTrue(self.discord.wait_for(
                lambda: bool(self.discord.dm_channels), 10), "the owner's DM was opened")
            self.assertTrue(self.discord.wait_for(
                lambda: bool(self.discord.messages.get(
                    self.discord.dm_channels.get(OWNER, ""), [])), 10))
            dm = self.discord.messages.get(self.discord.dm_channels[OWNER], [])
        self.assertTrue(any("still private" in message["content"] for message in dm))
        self.assertTrue(any("the answer to “go”" in message["content"] for message in dm),
                        "the DM names the question it answers")
        self.assertEqual(self.discord.followups, [], "a dead token is not tried")
        self.assertEqual(self.discord.messages.get(CHANNEL, []), [],
                         "a hidden answer is never posted in the channel")

    def test_an_approved_askers_late_answer_goes_to_them_not_the_owner(self):
        """The owner's DM would show them what an approved person asked."""
        guest = "777777777777777777"
        self.device.script = [("idle", "for you only")]
        with TempDir() as directory:
            app = self.make(directory, users=[guest])
            self.start(app, user_id=guest, hidden=True,
                       asked_at=time.monotonic() - asking.LIFE - 60)
            self.assertTrue(self.discord.wait_for(
                lambda: bool(self.discord.messages.get(
                    self.discord.dm_channels.get(guest, ""), [])), 10))
            mine = self.discord.messages.get(self.discord.dm_channels[guest], [])
        self.assertTrue(any("for you only" in message["content"] for message in mine))
        self.assertNotIn(OWNER, self.discord.dm_channels,
                         "the owner's DM is not opened")
        self.assertEqual(self.discord.messages.get(CHANNEL, []), [],
                         "the answer never lands in the channel")

    def test_a_state_file_that_cannot_be_written_does_not_fail_the_answer(self):
        """Remembering where the answer landed is not worth a second post."""
        with TempDir() as directory:
            app = self.make(directory)
            app.state = State(directory / "gone" / "state.json")
            worker = self.worker(app)
            worker._record({"id": "msg_1"})  # must not raise

    def test_a_question_goes_to_the_owners_dm_and_answers_the_form(self):
        self.device.script = [("form", {"title": "Questions", "fields": [field("q0", "Pick one")]}),
                              ("idle", "all done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.start(app)
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            channel, message = self.card()
            self.assertEqual(channel, f"dm_{OWNER}")
            self.assertIn("Pick one", message["content"])
            self.assertIn("Which one?", message["content"])
            buttons = message["components"][0]["components"]
            self.assertEqual([button["label"] for button in buttons], ["Yes", "No", "Other"])
            self.click(app, buttons[0]["custom_id"], message)
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.form_replies), 10))
            self.assertEqual(self.device.form_replies[0][2], {"q0": "Yes"})
            self.assertTrue(self.discord.wait_for(
                lambda: any("all done" in str(entry.get("content"))
                            for entry in self.discord.followups), 10))
        self.assertEqual(self.discord.messages.get(CHANNEL, []), [],
                         "the question never reaches the channel")

    def test_other_opens_a_modal_and_the_typed_answer_is_sent(self):
        self.device.script = [("form", {"title": "Questions", "fields": [field("q0", "Pick one")]}),
                              ("idle", "all done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.start(app)
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            _channel, message = self.card()
            other = message["components"][0]["components"][-1]
            interaction = self.click(app, other["custom_id"], message)
            answer = self.discord.answer_for(interaction)
            self.assertEqual(answer["type"], 9)
            self.assertEqual(answer["data"]["title"], "Pick one")
            modal = answer["data"]["custom_id"]
            self.assertTrue(modal.startswith("qm:"))
            self.submit(app, modal, "Maybe", message)
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.form_replies), 10))
            self.assertEqual(self.device.form_replies[0][2], {"q0": "Maybe"})

    def test_only_the_owner_answers_the_question(self):
        self.device.script = [("form", {"title": "Questions", "fields": [field("q0", "Pick one")]}),
                              ("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.start(app)
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            _channel, message = self.card()
            buttons = message["components"][0]["components"]
            interaction = self.click(app, buttons[0]["custom_id"], message, who=STRANGER)
            answer = self.discord.answer_for(interaction)
            self.assertEqual(answer["data"]["flags"], 64)
            self.assertIn("Only", answer["data"]["content"])
        self.assertEqual(self.device.form_replies, [])

    def test_a_multiple_choice_field_uses_a_select(self):
        form = {"title": "Questions", "fields": [{
            "key": "q0", "title": "Pick some", "description": "Which ones?",
            "type": "multiselect", "custom": True,
            "options": [{"value": "A", "label": "A"}, {"value": "B", "label": "B"}],
        }]}
        self.device.script = [("form", form), ("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.start(app)
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            _channel, message = self.card()
            select = message["components"][0]["components"][0]
            self.assertEqual(select["type"], 3)
            self.assertEqual(select["max_values"], 2)
            self.assertEqual([option["label"] for option in select["options"]], ["A", "B"])
            self.click(app, select["custom_id"], message, values=["0", "1"])
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.form_replies), 10))
        self.assertEqual(self.device.form_replies[0][2], {"q0": ["A", "B"]})

    def test_when_a_dm_is_impossible_the_question_stays_out_of_the_channel(self):
        self.discord.no_dm.add(OWNER)
        self.device.script = [("form", {"title": "Questions", "fields": [field("q0", "Pick one")]}),
                              ("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.start(app)
            self.assertTrue(self.discord.wait_for(
                lambda: any("could not send it to you" in str(content)
                            for content in self.said()), 10))
        for content in self.said():
            self.assertNotIn("Pick one", str(content),
                             "the question never reaches a channel")
        self.assertEqual(self.device.form_replies, [])

    def test_two_fields_are_sent_in_one_reply(self):
        form = {"title": "Questions", "fields": [field("q0", "First"), field("q1", "Second")]}
        self.device.script = [("form", form), ("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.start(app)
            self.assertTrue(self.discord.wait_for(lambda: self.card()[0] is not None, 10))
            _channel, message = self.card()
            rows = message["components"]
            self.assertEqual(len(rows), 2, "one row for each field")
            first = rows[0]["components"][0]["custom_id"]
            second = rows[1]["components"][1]["custom_id"]
            self.click(app, first, message)
            self.assertFalse(self.device.form_replies,
                             "the form waits for the second field")
            self.click(app, second, message)
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.form_replies), 10))
        self.assertEqual(self.device.form_replies[0][2], {"q0": "Yes", "q1": "No"})

if __name__ == "__main__":
    unittest.main()
