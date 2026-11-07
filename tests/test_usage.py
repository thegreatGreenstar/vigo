"""The cap on what approved people spend of the owner's plan."""

from __future__ import annotations

import time
import unittest

from vigo import usage
from vigo.config import Guests
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings

GUEST = "777777777777777777"
OTHER = "666666666666666666"
OWNER = "145512144145512144"
CAPACITY = 30_000_000
NO_CAPS = {"hourly": 0, "daily": 0, "perUserHourly": 0, "perUserDaily": 0}


def tokens(amount: int, **rest: int) -> dict:
    """One assistant message's usage, in the shape the device sends."""
    return {"input": amount, "output": 0, "reasoning": 0, **rest}


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeOpenCode("macbook")
        self.now = time.time() + 5

    def tearDown(self):
        self.device.close()

    def settings(self, directory, **overrides):
        return load_settings(
            directory,
            devices=[{"name": "macbook", "url": self.device.url, "directory": "/tmp/project"}],
            **overrides,
        )

    def spend(self, settings, who: str = GUEST) -> usage.Verdict:
        found = usage.collect(settings, "macbook", now=self.now)
        return usage.judge(found, who, settings.guests)

    # -- whose session is it ----------------------------------------------

    def test_a_guest_is_found_by_the_mark_or_by_the_channel_key(self):
        self.assertEqual(usage.guest_of({"metadata": {"vigoGuest": GUEST}}), GUEST)
        self.assertEqual(usage.guest_of({"metadata": {"vigoChannel": f"chan:{GUEST}"}}), GUEST)
        self.assertEqual(usage.guest_of({"metadata": {"vigoChannel": "chan"}}), "")
        self.assertEqual(usage.guest_of({}), "")

    def test_a_subagent_counts_as_the_session_that_delegated_it(self):
        listed = [
            {"id": "parent", "metadata": {"vigoGuest": GUEST}},
            {"id": "child", "parentID": "parent", "metadata": {}},
            {"id": "grandchild", "parentID": "child", "metadata": {}},
            {"id": "own", "metadata": {"vigoChannel": "chan"}},
        ]
        self.assertEqual(
            usage.guest_sessions(listed),
            {"parent": GUEST, "child": GUEST, "grandchild": GUEST},
            "a delegated session has no mark of its own, and still spends the plan",
        )

    def test_cache_reads_are_not_plan_tokens(self):
        self.assertEqual(
            usage.tokens_of({"input": 1, "output": 2, "reasoning": 3,
                             "cache": {"read": 10**9, "write": 0}}),
            6,
        )
        self.assertEqual(usage.tokens_of(None), 0)

    # -- the share of the plan ---------------------------------------------

    def test_a_third_of_the_plan_is_the_edge(self):
        with TempDir() as directory:
            settings = self.settings(directory, guests=NO_CAPS)
            guest = self.device.new_session(metadata={"vigoGuest": GUEST})
            self.device.add_message(guest, "assistant", tokens=tokens(1_000_000))
            self.device.meters = {"monthly": 33.0}
            self.assertTrue(self.spend(settings).allowed, "33 percent of 100 is under a third")
            self.device.meters = {"monthly": 34.0}
            verdict = self.spend(settings)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "share")

    def test_an_owner_who_spent_two_thirds_leaves_the_last_third(self):
        with TempDir() as directory:
            settings = self.settings(directory, guests=NO_CAPS)
            owner = self.device.new_session(metadata={"vigoChannel": "chan"})
            guest = self.device.new_session(metadata={"vigoGuest": GUEST})
            # 66 units of the plan are the owner's, and the guest has 17 more:
            # 66 * 17/83 is 13.5 percent of the plan, not the guest's third.
            self.device.add_message(owner, "assistant", tokens=tokens(660_000))
            self.device.add_message(guest, "assistant", tokens=tokens(170_000))
            self.device.meters = {"monthly": 66.0}
            self.assertTrue(self.spend(settings).allowed)
            # The guest passes the last third only when the owner's 66 units
            # and the guest's 69 make 66 * 69/135, which is over a third.
            self.device.add_message(guest, "assistant", tokens=tokens(520_000))
            verdict = self.spend(settings)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "share")

    def test_the_highest_meter_is_the_one_that_counts(self):
        with TempDir() as directory:
            settings = self.settings(directory, guests=NO_CAPS)
            guest = self.device.new_session(metadata={"vigoGuest": GUEST})
            self.device.add_message(guest, "assistant", tokens=tokens(1_000_000))
            self.device.meters = {"rolling": 80.0, "weekly": 10.0, "monthly": 10.0}
            verdict = self.spend(settings)
        self.assertFalse(verdict.allowed,
                         "the rolling window is the one that would stop the plan")

    def test_each_meter_is_measured_over_its_own_window(self):
        """A week's allowance spent in a day is caught by the week's meter.

        The whole month is 10M tokens, so the guest's 1M is a tenth of it.  The
        owner's 9M is older than the week, so the week holds the guest's 1M
        alone: at 66 percent the guest is two thirds of that window, and the
        month's ratio would have hidden it.
        """
        with TempDir() as directory:
            settings = self.settings(directory, guests=NO_CAPS)
            owner = self.device.new_session(metadata={"vigoChannel": "chan"})
            guest = self.device.new_session(metadata={"vigoGuest": GUEST})
            self.device.add_message(owner, "assistant", tokens=tokens(9_000_000),
                                    time={"created": (self.now - 20 * 86400) * 1000})
            self.device.add_message(guest, "assistant", tokens=tokens(1_000_000))
            self.device.meters = {"rolling": 0.0, "weekly": 66.0, "monthly": 10.0}
            verdict = self.spend(settings)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "share")

    def test_a_meter_that_is_already_limited_refuses(self):
        with TempDir() as directory:
            settings = self.settings(directory, guests=NO_CAPS)
            self.device.meters = {"monthly": 10.0}
            self.device.limited_meters = ("weekly",)
            verdict = self.spend(settings)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "limited")

    # -- the hourly and daily caps ----------------------------------------

    def test_the_hourly_cap_counts_only_this_hour(self):
        with TempDir() as directory:
            settings = self.settings(directory, guests={"hourly": 1_000, "daily": 0,
                                                        "perUserHourly": 0,
                                                        "perUserDaily": 0})
            guest = self.device.new_session(metadata={"vigoGuest": GUEST})
            self.device.add_message(
                guest, "assistant", tokens=tokens(900),
                time={"created": (self.now - 120) * 1000},
            )
            self.assertTrue(self.spend(settings).allowed)
            self.device.add_message(
                guest, "assistant", tokens=tokens(200),
                time={"created": (self.now - 60) * 1000},
            )
            verdict = self.spend(settings)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "hourly")

    def test_an_old_message_counts_on_the_day_and_not_on_the_hour(self):
        with TempDir() as directory:
            settings = self.settings(directory, guests={"hourly": 2_000, "daily": 5_000,
                                                        "perUserHourly": 0,
                                                        "perUserDaily": 0})
            guest = self.device.new_session(metadata={"vigoGuest": GUEST})
            self.device.add_message(
                guest, "assistant", tokens=tokens(4_000),
                time={"created": (self.now - 7200) * 1000},
            )
            self.assertTrue(self.spend(settings).allowed)
            self.device.add_message(
                guest, "assistant", tokens=tokens(1_500),
                time={"created": (self.now - 30) * 1000},
            )
            verdict = self.spend(settings)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "daily")

    def test_a_cap_is_each_person_and_not_a_pool(self):
        with TempDir() as directory:
            settings = self.settings(directory, guests={"hourly": 0, "daily": 0,
                                                        "perUserHourly": 0,
                                                        "perUserDaily": 2_000})
            mine = self.device.new_session(metadata={"vigoGuest": GUEST})
            theirs = self.device.new_session(metadata={"vigoGuest": OTHER})
            self.device.add_message(mine, "assistant", tokens=tokens(1_500))
            self.device.add_message(theirs, "assistant", tokens=tokens(1_900))
            self.assertTrue(self.spend(settings, GUEST).allowed)
            self.device.add_message(mine, "assistant", tokens=tokens(600))
            verdict = self.spend(settings, GUEST)
            theirs = self.spend(settings, OTHER)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "user-daily")
        self.assertTrue(theirs.allowed, "another person's spending is not this person's cap")

    # -- what cannot be read is refused, never guessed ---------------------

    def test_a_plan_that_cannot_be_read_refuses(self):
        self.device.metered = False
        with TempDir() as directory:
            settings = self.settings(directory, users=[GUEST])
            verdict = usage.screen(settings, GUEST, "macbook")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "unknown")

    def test_a_share_of_zero_pauses_every_guest(self):
        with TempDir() as directory:
            settings = self.settings(directory, guests={"share": 0})
            verdict = usage.screen(settings, GUEST, "macbook")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "paused")
        self.assertEqual(self.device.limits_reads, 0, "no device is asked when guests are paused")

    def test_a_long_guest_session_is_read_across_its_pages(self):
        """The device pages its messages; a session past one page still counts."""
        with TempDir() as directory:
            settings = self.settings(directory)
            guest = self.device.new_session(metadata={"vigoGuest": GUEST})
            for _ in range(250):
                self.device.add_message(guest, "assistant", tokens=tokens(10))
            found = usage.collect(settings, "macbook", now=self.now)
        self.assertEqual(found.guest_windows.get("monthly"), 2_500)

    def test_an_owner_session_never_counts_as_a_guest(self):
        with TempDir() as directory:
            settings = self.settings(directory)
            owner = self.device.new_session(metadata={"vigoChannel": "chan"})
            self.device.add_message(owner, "assistant", tokens=tokens(9_000_000))
            found = usage.collect(settings, "macbook", now=self.now)
        self.assertEqual(found.guest_windows.get("monthly"), 0)
        self.assertEqual(found.all_windows.get("monthly"), 9_000_000)

    # -- A/B: the same workload, with and without the cap -------------------

    def test_the_cap_holds_a_third_where_a_free_run_spends_it_all(self):
        """A scripted month: 30 questions, 500k tokens each, owner 3M.

        A plan of 30M tokens would read 33 percent at the tenth question, so
        the cap keeps the guests at the third the rule promises.  The same
        workload without the cap spends 15M, half the plan, and the test says
        both numbers rather than trusting one.
        """
        def run(guarded: bool) -> tuple[int, int, int]:
            guest = 0
            refused = 0
            owner = 3_000_000
            for _ in range(30):
                total = guest + owner
                percent = 100.0 * total / CAPACITY
                spend = usage.Spend(
                    percents={"monthly": percent},
                    all_windows={"monthly": total},
                    guest_windows={"monthly": guest},
                    guest_hour=guest, guest_day=guest,
                )
                allowed = True
                if guarded:
                    allowed = usage.judge(
                        spend, GUEST,
                        Guests(hourly=0, daily=0, per_user_hourly=0, per_user_daily=0),
                    ).allowed
                if allowed:
                    guest += 500_000
                else:
                    refused += 1
            return guest, guest + owner, refused

        free_guest, _free_total, free_refused = run(False)
        held_guest, _held_total, held_refused = run(True)
        self.assertEqual(free_refused, 0)
        self.assertGreaterEqual(free_guest, 15_000_000, "the control spends the whole workload")
        self.assertGreater(free_guest / CAPACITY, 1 / 3, "the control passes a third of the plan")
        self.assertLessEqual(held_guest, CAPACITY / 3 + 500_000,
                             "the cap holds the third of the 30M plan")
        self.assertGreater(held_refused, 0)
        self.assertGreater(free_guest, held_guest, "the cap spends less than no cap")


if __name__ == "__main__":
    unittest.main()
