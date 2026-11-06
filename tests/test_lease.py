"""One channel's run at a time, across daemons."""

import dataclasses
import threading
import unittest

from vigo import commands
from vigo.app import App
from vigo.discord_api import Discord
from vigo.lease import Lease
from vigo.opencode_api import OpenCode
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import question_of, TempDir, load_settings

CHANNEL = "chan_lease"
OWNER = "145512144145512144"


class Clock:
    """A clock the test moves, so a lease test never waits on the wall."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeOpenCode("macbook")
        self.addCleanup(self.device.close)
        self.session = self.device.new_session("Discord DM", {"vigoChannel": CHANNEL})
        self.client = OpenCode(self.device.url, "pw")

    def lease(self, owner: str = "macbook", **kwargs) -> Lease:
        return Lease(self.client, self.session, owner, **kwargs)

    def metadata(self) -> dict:
        with self.device.lock:
            return dict(self.device.sessions[self.session].get("metadata") or {})

    def test_a_run_takes_the_session_and_gives_it_back(self):
        lease = self.lease()
        self.assertEqual(lease.claim(), "mine")
        self.assertEqual(self.metadata()["vigoRun"]["owner"], "macbook")
        lease.release()
        self.assertNotIn("vigoRun", self.metadata(), "the claim is gone")
        self.assertEqual(self.metadata()["vigoChannel"], CHANNEL,
                         "and the channel mark it found is still there")

    def test_a_beating_claim_is_left_alone(self):
        clock = Clock()
        theirs = self.lease("pc", clock=clock)
        self.assertEqual(theirs.claim(), "mine")
        mine = self.lease("macbook", stale_after=10.0, clock=clock)
        self.assertEqual(mine.claim(), "theirs", "it just beat: somebody works")
        clock.advance(3.0)
        theirs.renew()
        self.assertEqual(mine.claim(), "theirs", "it beat again")
        clock.advance(3.0)
        theirs.renew()
        self.assertEqual(mine.claim(), "theirs")

    def test_a_claim_that_stopped_beating_is_taken(self):
        clock = Clock()
        theirs = self.lease("pc", stale_after=10.0, clock=clock)
        theirs.claim()
        mine = self.lease("macbook", stale_after=10.0, clock=clock)
        self.assertEqual(mine.claim(), "theirs")
        clock.advance(11.0)  # no beat in all that time
        self.assertEqual(mine.claim(), "mine", "a dead claim is not a claim")
        self.assertEqual(self.metadata()["vigoRun"]["owner"], "macbook")

    def test_a_restarted_daemon_takes_its_own_claim_back(self):
        first = self.lease("macbook")
        self.assertEqual(first.claim(), "mine")
        # the daemon goes away without releasing: the claim it leaves is its own
        again = self.lease("macbook", stale_after=600.0)
        self.assertEqual(again.claim(), "mine", "the owner may always take its own")
        self.assertEqual(self.metadata()["vigoRun"]["beat"], 2, "and the beat carries on")

    def test_a_device_that_will_not_keep_metadata_still_gets_the_question(self):
        gone = FakeOpenCode("macbook")
        self.addCleanup(gone.close)
        client = OpenCode(gone.url + "/nothing-here", "pw")
        lease = Lease(client, "ses_none", "macbook")
        self.assertEqual(lease.claim(), "unknown", "the run goes ahead and says why")


class TwoDaemonsTest(unittest.TestCase):
    """Two daemons, each with its own Discord and state, on one device."""

    def setUp(self):
        self.device = FakeOpenCode("macbook")
        self.addCleanup(self.device.close)
        self.mac = FakeDiscord()
        self.pc = FakeDiscord()
        self.addCleanup(self.mac.close)
        self.addCleanup(self.pc.close)
        self.apps: list[App] = []

    def app(self, name: str, discord: FakeDiscord, directory, **overrides) -> App:
        settings = load_settings(
            directory,
            device=name,
            devices=[{"name": name, "url": self.device.url, "directory": "/tmp/project"}],
            **overrides,
        )
        api = Discord("token", settings.app_id, base=discord.url)
        app = App(settings, api=api, state=State(directory / f"{name}.json"))
        self.apps.append(app)
        return app

    def tearDown(self) -> None:
        for app in self.apps:
            app.shutdown()

    def use(self, app: App, discord: FakeDiscord, options: list) -> str:
        interaction = discord.next_id("int")
        app.handle_interaction({
            "id": interaction,
            "token": "tok",
            "type": 2,
            "channel_id": CHANNEL,
            "channel": {"name": "general"},
            "member": {"user": {"id": OWNER}},
            "data": {"name": "vigo", "options": [{"name": commands.ASK, "type": 1,
                                                  "options": options}]},
        })
        return interaction

    def posted(self, discord: FakeDiscord) -> list[str]:
        edits = [entry["content"] for entry in discord.edits]
        followups = [entry["content"] for entry in discord.followups]
        return edits + followups

    def test_two_daemons_do_not_put_two_questions_into_one_turn(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.device.script = [("until", gate), ("idle", "the first answer")]
        with TempDir() as mac_dir, TempDir() as pc_dir:
            mac = self.app("macbook", self.mac, mac_dir)
            pc = self.app("pc", self.pc, pc_dir)
            self.use(mac, self.mac, [{"name": "prompt", "type": 3, "value": "why first?"}])
            self.assertTrue(self.device.wait_for_prompt())
            # The second daemon asks while the first one is working, so its run
            # reaches the lease with the device busy and no run of its own.
            self.use(pc, self.pc, [{"name": "prompt", "type": 3, "value": "why second?"}])
            self.assertTrue(self.pc.wait_for(lambda: pc.run_for(CHANNEL) is not None, 5))
            self.assertEqual(len(self.device.prompts), 1,
                             "the second question waits for the first turn")
            # A copied its script when it started; this one is for B.
            self.device.script = [("idle", "the second answer")]
            gate.set()
            self.assertTrue(self.pc.wait_for(
                lambda: any("the second answer" in content for content in self.posted(self.pc)),
                10))
        self.assertEqual([question_of(asked) for _session, asked in self.device.prompts],
                         ["why first?", "why second?"], "one prompt each, in order")

    def test_a_question_says_why_when_another_daemon_holds_the_channel(self):
        """The lease holds even when the device itself looks idle."""
        with TempDir() as mac_dir, TempDir() as pc_dir:
            mac = self.app("macbook", self.mac, mac_dir)
            pc = self.app("pc", self.pc, pc_dir)
            pc.config = dataclasses.replace(pc.config, timeout=0.2)
            session = self.device.new_session("Discord #general", {"vigoChannel": CHANNEL})
            mac.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            held = Lease(OpenCode(self.device.url, "pw"), session, "macbook")
            self.addCleanup(held.release)
            self.assertEqual(held.claim(), "mine")
            self.use(pc, self.pc, [{"name": "prompt", "type": 3, "value": "why second?"}])
            self.assertTrue(self.pc.wait_for(
                lambda: any("Another vigo" in content for content in self.posted(self.pc)), 10))
        self.assertEqual(self.device.prompts, [], "nothing was sent into the other turn")

    def test_the_goal_command_does_not_start_a_turn_somebody_holds(self):
        with TempDir() as mac_dir, TempDir() as pc_dir:
            mac = self.app("macbook", self.mac, mac_dir)
            pc = self.app("pc", self.pc, pc_dir)
            session = self.device.new_session("Discord #general", {"vigoChannel": CHANNEL})
            mac.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            held = Lease(OpenCode(self.device.url, "pw"), session, "macbook")
            self.addCleanup(held.release)
            self.assertEqual(held.claim(), "mine")
            pc.handle_interaction({
                "id": self.pc.next_id("int"), "token": "tok", "type": 2,
                "channel_id": CHANNEL, "channel": {"name": "general"},
                "member": {"user": {"id": OWNER}},
                "data": {"name": "vigo", "options": [
                    {"name": commands.GOAL, "type": 1, "options": [
                        {"name": "objective", "type": 3, "value": "keep it green"},
                    ]},
                ]},
            })
            self.assertTrue(self.pc.wait_for(
                lambda: any("Another vigo" in content for content in self.posted(self.pc)), 10))
        self.assertEqual(self.device.commands, [], "no goal was set on that session")
