import unittest

from vigo import commands
from vigo.app import App
from vigo.discord_api import Discord
from vigo.presence import ONLINE_FOR, Presence, status_for
from vigo.router import Router
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings

CHANNEL = "chan_1"


class StatusTest(unittest.TestCase):
    def test_no_server_that_answers_is_invisible(self):
        self.assertEqual(
            status_for(working=True, up=0, busy=0, last=10.0, now=10.0),
            "invisible",
            "a light cannot be green when there is nothing to ask",
        )

    def test_work_is_online(self):
        self.assertEqual(
            status_for(working=True, up=1, busy=0, last=None, now=100.0), "online")

    def test_an_answer_keeps_it_online_for_two_minutes(self):
        self.assertEqual(
            status_for(working=False, up=1, busy=0, last=100.0, now=100.0 + ONLINE_FOR),
            "online",
        )
        self.assertEqual(
            status_for(working=False, up=1, busy=0, last=100.0,
                       now=100.0 + ONLINE_FOR + 1.0),
            "idle",
        )

    def test_every_server_at_work_is_do_not_disturb(self):
        self.assertEqual(
            status_for(working=False, up=2, busy=2, last=None, now=100.0), "dnd")

    def test_a_server_that_can_take_a_question_is_not_to_disturb(self):
        self.assertEqual(
            status_for(working=False, up=2, busy=1, last=None, now=100.0), "idle")

    def test_work_while_every_server_is_busy_is_do_not_disturb(self):
        self.assertEqual(
            status_for(working=True, up=1, busy=1, last=100.0, now=100.0), "dnd",
            "thinking is do not disturb, not a green light",
        )

    def test_work_with_a_free_server_is_online(self):
        self.assertEqual(
            status_for(working=True, up=2, busy=1, last=None, now=100.0), "online",
            "the other server can take the next question",
        )


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.shown: list[str] = []
        self.now = 1000.0
        self.presence = Presence(self.shown.append, clock=lambda: self.now)

    def test_it_shows_a_status_once(self):
        self.presence.show("idle")
        self.presence.show("idle")
        self.presence.show("online")
        self.assertEqual(self.shown, ["idle", "online"])

    def test_an_answer_sets_the_time_it_answered(self):
        self.assertIsNone(self.presence.last)
        self.presence.touch()
        self.assertEqual(self.presence.last, 1000.0)


class _Gateway:
    """Only what the app touches: the status it hands to the connection."""

    def __init__(self):
        self.statuses: list[str] = []

    def set_presence(self, status: str) -> None:
        self.statuses.append(status)


class AppPresenceTest(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()
        self.device = FakeOpenCode("macbook")
        self.app: App | None = None

    def tearDown(self):
        if self.app is not None:
            self.app.shutdown()
        self.device.close()
        self.discord.close()

    def make(self, directory) -> App:
        settings = load_settings(
            directory,
            devices=[{"name": "macbook", "url": self.device.url,
                      "directory": "/tmp/project"}],
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        self.app = App(settings, api=api, state=State(directory / "state.json"))
        self.app.gateway = _Gateway()
        self.app.router = Router(self.app.config, cache=0.0)
        return self.app

    def test_a_server_at_work_is_do_not_disturb(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session()
            self.device.started_turn(session)
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "dnd")

    def test_thinking_on_the_only_server_is_do_not_disturb(self):
        """The daemon's own turn makes the light red while it thinks."""
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session()
            self.device.started_turn(session)
            app._begin_work()
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "dnd")

    def test_a_server_that_is_free_keeps_it_idle(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session()
            self.device.started_turn(session)
            self.device.finished_turn(session)
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "idle")

    def test_a_finished_answer_keeps_it_online(self):
        with TempDir() as directory:
            app = self.make(directory)
            app._begin_work()
            self.assertEqual(app._working(), True)
            app._end_work()
            self.assertEqual(app._working(), False)
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "online")

    def test_an_answer_to_a_command_touches_the_status(self):
        with TempDir() as directory:
            app = self.make(directory)
            payload = {
                "id": "int_1", "token": "tok", "type": 2, "channel_id": CHANNEL,
                "channel": {"name": "general"},
                "member": {"user": {"id": app.config.owner, "username": "owner"}},
                "data": {"name": "vigo",
                         "options": [{"name": commands.HELP, "type": 1, "options": []}]},
            }
            app.handle_interaction(payload)
            self.assertIsNotNone(app.presence.last)
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "online")

    def test_no_server_that_answers_is_invisible(self):
        with TempDir() as directory:
            app = self.make(directory)
            app._refresh_presence()
            self.assertEqual(app.gateway.statuses[-1], "idle")
            self.device.close()
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "invisible")


if __name__ == "__main__":
    unittest.main()
