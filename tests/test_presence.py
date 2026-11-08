import unittest

from vigo import commands
from vigo.app import App
from vigo.discord_api import Discord
from vigo import presence as showing
from vigo.peer import PeerServer
from vigo.presence import ONLINE_FOR, Presence, merge, status_for
from vigo.router import Router
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings

CHANNEL = "chan_1"


class StatusTest(unittest.TestCase):
    def test_no_server_that_answers_is_do_not_disturb(self):
        self.assertEqual(
            status_for(working=True, up=0, busy=0, last=10.0, now=10.0),
            "dnd",
            "nothing can be asked there, and a connected bot is not offline",
        )

    def test_work_is_online(self):
        self.assertEqual(
            status_for(working=True, up=1, busy=0, last=None, now=100.0), "online")

    def test_an_answer_keeps_it_online_for_five_minutes(self):
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

    def test_work_while_every_server_is_busy_is_online(self):
        self.assertEqual(
            status_for(working=True, up=1, busy=1, last=100.0, now=100.0), "online",
            "the account shows the work, and turns red only for work it is not doing",
        )

    def test_work_with_a_free_server_is_online(self):
        self.assertEqual(
            status_for(working=True, up=2, busy=1, last=None, now=100.0), "online",
            "the other server can take the next question",
        )


class MergeTest(unittest.TestCase):
    """What the same bot account shows when its daemons disagree."""

    def test_the_best_news_of_the_daemons_is_the_status(self):
        self.assertEqual(merge("dnd", "idle", "invisible"), "idle")
        self.assertEqual(merge("invisible", "online", "dnd"), "online")

    def test_one_daemon_at_work_is_green(self):
        self.assertEqual(merge("dnd", "online"), "online")

    def test_a_free_daemon_keeps_a_busy_one_from_looking_red(self):
        self.assertEqual(merge("dnd", "idle"), "idle",
                         "the free machine can take the next question")

    def test_every_daemon_at_work_is_do_not_disturb(self):
        self.assertEqual(merge("dnd", "dnd"), "dnd")

    def test_no_daemon_that_answers_is_do_not_disturb(self):
        self.assertEqual(merge("invisible", "invisible"), "dnd",
                         "a live connection is not offline")
        self.assertEqual(merge(), "dnd")

    def test_a_status_this_code_does_not_know_promises_nothing(self):
        self.assertEqual(merge("away", "dnd"), "dnd")


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

    def make(self, directory, devices: list[dict] | None = None) -> App:
        settings = load_settings(
            directory,
            devices=devices or [{"name": "macbook", "url": self.device.url,
                                 "directory": "/tmp/project"}],
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        self.app = App(settings, api=api, state=State(directory / "state.json"))
        self.app.gateway = _Gateway()
        self.app.router = Router(self.app.config, cache=0.0)
        return self.app

    def with_peer(self, status: str) -> tuple[PeerServer, dict]:
        """A second device whose daemon says it shows `status`.

        The device's OpenCode server is down, as it is for a machine that this
        one cannot reach: only the peer port answers, which is what the test
        is about.
        """
        peer = PeerServer("127.0.0.1", 0, "pc", "pw",
                          status=lambda: status).start()
        self.addCleanup(peer.stop)
        entry = {
            "name": "pc", "url": "http://127.0.0.1:1",
            "directory": "/tmp/other",
            "peer": f"http://127.0.0.1:{peer.server.server_address[1]}",
        }
        return peer, entry

    def test_a_server_at_work_is_do_not_disturb(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session()
            self.device.started_turn(session)
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "dnd")

    def test_a_stale_busy_session_is_not_do_not_disturb(self):
        """A restart leaves the stamp; the device's list says no turn runs."""
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session()
            self.device.started_turn(session)
            self.device.lost_turn(session)
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "idle",
                         "no turn runs there, so the light is not red")

    def test_thinking_on_the_only_server_is_green(self):
        """The account shows the work the daemon is doing, not a red light."""
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session()
            self.device.started_turn(session)
            app._begin_work()
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "online")

    def test_another_daemon_at_work_keeps_the_account_green(self):
        """This daemon is idle, and shows what the one at work says."""
        _peer, entry = self.with_peer("online")
        with TempDir() as directory:
            app = self.make(directory, devices=[
                {"name": "macbook", "url": self.device.url, "directory": "/tmp/project"},
                entry,
            ])
            app._refresh_presence()
            self.assertEqual(app.presence.own, "idle", "its own view is unchanged")
        self.assertEqual(app.gateway.statuses[-1], "online")

    def test_a_daemon_that_stops_answering_is_not_counted(self):
        """A status is only worth what the daemon that said it still answers."""
        peer, entry = self.with_peer("online")
        with TempDir() as directory:
            app = self.make(directory, devices=[
                {"name": "macbook", "url": self.device.url, "directory": "/tmp/project"},
                entry,
            ])
            now = [1000.0]
            app.presence.clock = lambda: now[0]
            app._refresh_presence()
            self.assertEqual(app.gateway.statuses[-1], "online")
            peer.stop()
            now[0] += showing.PEER_EVERY + 1.0
            app._refresh_presence()
        self.assertEqual(app.gateway.statuses[-1], "idle")

    def test_a_machine_that_sees_no_server_shows_what_the_other_one_sees(self):
        """One machine's servers being down is not the whole bot being offline."""
        _peer, entry = self.with_peer("online")
        with TempDir() as directory:
            app = self.make(directory, devices=[
                {"name": "macbook", "url": self.device.url, "directory": "/tmp/project"},
                entry,
            ])
            self.device.close()
            app._refresh_presence()
            self.assertEqual(app.presence.own, "dnd")
        self.assertEqual(app.gateway.statuses[-1], "online")

    def test_a_machine_that_sees_no_server_is_never_offline(self):
        """The daemon is connected, so its own view cannot make it invisible."""
        self.device.close()
        with TempDir() as directory:
            app = self.make(directory)
            app._refresh_presence()
            app._refresh_presence()
        self.assertNotIn("invisible", app.gateway.statuses)
        self.assertEqual(app.gateway.statuses[-1], "dnd",
                         "nothing can be asked there")

    def test_an_old_daemon_that_says_invisible_is_do_not_disturb(self):
        """A peer on the previous version cannot take the account offline."""
        _peer, entry = self.with_peer("invisible")
        with TempDir() as directory:
            app = self.make(directory, devices=[
                {"name": "macbook", "url": self.device.url, "directory": "/tmp/project"},
                entry,
            ])
            self.device.close()
            app._refresh_presence()
        self.assertNotIn("invisible", app.gateway.statuses)
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

    def test_no_server_that_answers_is_never_offline(self):
        with TempDir() as directory:
            app = self.make(directory)
            app._refresh_presence()
            self.assertEqual(app.gateway.statuses[-1], "idle")
            self.device.close()
            app._refresh_presence()
        self.assertNotIn("invisible", app.gateway.statuses)
        self.assertEqual(app.gateway.statuses[-1], "dnd")


if __name__ == "__main__":
    unittest.main()
