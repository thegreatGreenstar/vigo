import time
import unittest

from vigo.router import Router
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings


def older(fake: FakeOpenCode, session: str, seconds: float) -> None:
    for message in fake.messages[session]:
        if message["type"] == "user":
            message["time"]["created"] = (time.time() - seconds) * 1000


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.mac = FakeOpenCode("macbook")
        self.pc = FakeOpenCode("pc")

    def tearDown(self):
        self.mac.close()
        self.pc.close()

    def settings(self, directory, **overrides):
        return load_settings(
            directory,
            devices=[
                {"name": "macbook", "url": self.mac.url, "directory": "/mac"},
                {"name": "pc", "url": self.pc.url, "directory": "/pc"},
            ],
            **overrides,
        )

    def used(self, fake: FakeOpenCode, seconds_ago: float) -> None:
        session = fake.new_session()
        fake.add_message(session, "user", text="hello")
        if seconds_ago:
            older(fake, session, seconds_ago)

    def test_the_device_last_used_wins(self):
        with TempDir() as directory:
            router = Router(self.settings(directory))
            self.used(self.mac, 5)
            self.used(self.pc, 3600)
            pick = router.pick(fresh=True)
        self.assertEqual(pick.name, "macbook")
        self.assertIn("used it", pick.reason)

    def test_the_other_one_wins_when_it_is_the_one(self):
        with TempDir() as directory:
            router = Router(self.settings(directory))
            self.used(self.mac, 3600)
            self.used(self.pc, 5)
            pick = router.pick(fresh=True)
        self.assertEqual(pick.name, "pc")

    def test_stale_use_falls_back_to_the_first_device(self):
        with TempDir() as directory:
            router = Router(self.settings(directory, activityWindow=600))
            self.used(self.mac, 7200)
            self.used(self.pc, 7200)
            pick = router.pick(fresh=True)
        self.assertEqual(pick.name, "macbook")
        self.assertIn("first device", pick.reason)

    def test_a_named_device(self):
        with TempDir() as directory:
            router = Router(self.settings(directory))
            self.used(self.mac, 5)
            pick = router.pick("pc", fresh=True)
        self.assertEqual(pick.name, "pc")

    def test_a_name_that_is_not_one(self):
        with TempDir() as directory:
            router = Router(self.settings(directory))
            pick = router.pick("toaster", fresh=True)
        self.assertIsNone(pick.name)
        self.assertIn("toaster", pick.reason)

    def test_a_named_device_that_is_down(self):
        with TempDir() as directory:
            router = Router(self.settings(directory))
            self.pc.close()
            pick = router.pick("pc", fresh=True)
        self.assertIsNone(pick.name)
        self.assertIn("down", pick.reason)

    def test_no_device_at_all(self):
        with TempDir() as directory:
            router = Router(self.settings(directory))
            self.mac.close()
            self.pc.close()
            pick = router.pick(fresh=True)
        self.assertIsNone(pick.name)
        self.assertIn("no device", pick.reason)

    def test_the_description_names_both(self):
        with TempDir() as directory:
            router = Router(self.settings(directory))
            written = router.describe(router.states(fresh=True))
        self.assertIn("macbook", written)
        self.assertIn("pc", written)
        self.assertIn("up", written)

    def test_the_description_can_name_the_vigo_build(self):
        with TempDir() as directory:
            router = Router(self.settings(directory))
            written = router.describe(
                router.states(fresh=True), detailed=True,
                builds={"macbook": "vigo 1.0.0 (abc1234)"},
            )
        self.assertIn("vigo 1.0.0 (abc1234)", written)
        pc_line = next(line for line in written.splitlines() if line.startswith("`pc`"))
        self.assertNotIn("vigo", pc_line)


if __name__ == "__main__":
    unittest.main()
