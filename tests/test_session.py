"""Which session a channel gets on a device, and when it gets a new one."""

import types
import unittest

from vigo import ask as asking
from vigo.config import Device, Model
from vigo.opencode_api import OpenCode
from vigo.state import State
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir

MODEL = Model(provider_id="opencode-go", id="deepseek-v4.1-flash")


def holder(state: State):
    """What `open_session` needs from the app: the state, and a model."""
    return types.SimpleNamespace(
        state=state, config=types.SimpleNamespace(model=MODEL),
    )


class OpenSessionTest(unittest.TestCase):
    def setUp(self):
        self.mac = FakeOpenCode("macbook")
        self.pc = FakeOpenCode("pc")
        self.addCleanup(self.mac.close)
        self.addCleanup(self.pc.close)
        self.directory = TempDir()
        self.directory.__enter__()
        self.addCleanup(self.directory.__exit__, None, None, None)

    def device(self, fake: FakeOpenCode) -> Device:
        return Device(name=fake.name, url=fake.url, directory=f"/tmp/{fake.name}")

    def open(self, state, fake, channel="chan", new=False, title="Discord DM"):
        client = OpenCode(fake.url, "pw")
        return asking.open_session(holder(state), client, self.device(fake),
                                   channel, new, title)

    def test_the_same_channel_reuses_its_session(self):
        state = State(self.directory.path / "state.json")
        first = self.open(state, self.mac)
        second = self.open(state, self.mac)
        self.assertEqual(first, second)
        self.assertEqual(len(self.mac.sessions), 1, "one session, not one per ask")

    def test_a_channel_has_one_session_on_each_device(self):
        state = State(self.directory.path / "state.json")
        on_mac = self.open(state, self.mac)
        on_pc = self.open(state, self.pc)
        self.assertIn(on_mac, self.mac.sessions)
        self.assertIn(on_pc, self.pc.sessions)
        self.assertEqual(self.open(state, self.mac), on_mac, "back on the Mac, same session")
        self.assertEqual(self.open(state, self.pc), on_pc)
        self.assertEqual(len(self.mac.sessions), 1)
        self.assertEqual(len(self.pc.sessions), 1)

    def test_a_second_daemon_adopts_the_session_the_device_holds(self):
        """Two daemons answer every channel; each has its own state file."""
        mine = State(self.directory.path / "mine.json")
        theirs = State(self.directory.path / "theirs.json")
        made = self.open(mine, self.mac)
        adopted = self.open(theirs, self.mac)
        self.assertEqual(made, adopted, "the device's own record is the one they share")
        self.assertEqual(len(self.mac.sessions), 1)

    def test_the_session_names_its_channel(self):
        state = State(self.directory.path / "state.json")
        session = self.open(state, self.mac, channel="1567785699418316850")
        self.assertEqual(self.mac.sessions[session]["metadata"][asking.CHANNEL_KEY],
                         "1567785699418316850")

    def test_an_explicit_new_opens_a_fresh_session(self):
        state = State(self.directory.path / "state.json")
        first = self.open(state, self.mac)
        second = self.open(state, self.mac, new=True)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.mac.sessions), 2)
        self.assertEqual(self.open(state, self.mac), second, "the fresh one is the channel's")

    def test_a_session_the_device_dropped_is_replaced(self):
        state = State(self.directory.path / "state.json")
        first = self.open(state, self.mac)
        with self.mac.lock:
            self.mac.sessions.pop(first)
            self.mac.messages.pop(first, None)
        second = self.open(state, self.mac)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.mac.sessions), 1)
