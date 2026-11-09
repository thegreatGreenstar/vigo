"""What the daemon remembers between runs: which session each channel has."""

import json
import os
import time
import unittest
from pathlib import Path

from vigo.state import State
from tests.support import TempDir


class StateTest(unittest.TestCase):
    def test_a_channel_keeps_a_session_per_device(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.remember_session("chan", "macbook", "ses_mac", "Discord DM")
            state.remember_session("chan", "pc", "ses_pc", "Discord DM")
            self.assertEqual(state.session_on("chan", "macbook")["session"], "ses_mac")
            self.assertEqual(state.session_on("chan", "pc")["session"], "ses_pc")
            self.assertIsNone(state.session_on("chan", "laptop"))
            newest = state.session_for("chan")
        self.assertEqual(newest["session"], "ses_pc", "the newest, which status shows")

    def test_forgetting_one_device_keeps_the_other(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.remember_session("chan", "macbook", "ses_mac", "Discord DM")
            state.remember_session("chan", "pc", "ses_pc", "Discord DM")
            state.forget_session("chan", "pc")
            self.assertIsNone(state.session_on("chan", "pc"))
            self.assertIsNotNone(state.session_on("chan", "macbook"))
            state.forget_session("chan")
            self.assertIsNone(state.session_for("chan"))
            self.assertNotIn("chan", state.data["sessions"])

    def test_the_old_shape_becomes_one_session_on_its_device(self):
        """A file written before a channel could hold one per device."""
        with TempDir() as directory:
            path = Path(directory / "state.json")
            path.write_text(json.dumps({
                "sessions": {"chan": {"device": "macbook", "session": "ses_old",
                                      "title": "Discord DM", "at": 7}},
                "tickets": {},
                "channels": {},
            }), encoding="utf-8")
            os.chmod(path, 0o600)
            state = State(path)
            self.assertEqual(state.session_on("chan", "macbook")["session"], "ses_old")
            self.assertEqual(state.session_for("chan")["session"], "ses_old")
            self.assertIsNone(state.session_on("chan", "pc"), "not made up on another device")

    def test_a_broken_sessions_table_is_not_fatal(self):
        with TempDir() as directory:
            path = Path(directory / "state.json")
            path.write_text(json.dumps({"sessions": []}), encoding="utf-8")
            os.chmod(path, 0o600)
            state = State(path)
            self.assertEqual(state.data["sessions"], {})
            self.assertIsNone(state.session_for("chan"))

    def test_an_answer_keeps_the_device_that_answered_it(self):
        """A reply continues there, so the device goes with the message."""
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.remember_answer("msg_1", "chan", "pc")
            entry = state.answer_for("msg_1")
            self.assertEqual(entry["channel"], "chan")
            self.assertEqual(entry["device"], "pc")
            state.remember_answer("msg_2", "chan")
            self.assertEqual(state.answer_for("msg_2")["device"], "",
                             "an answer with no device named still says its channel")

    def test_an_answer_from_before_the_device_was_kept_reads_as_none(self):
        """A state file written by an older daemon has no `device` key."""
        with TempDir() as directory:
            path = directory / "state.json"
            path.write_text(json.dumps({
                "answers": {"msg_1": {"channel": "chan", "at": time.time()}},
            }), encoding="utf-8")
            os.chmod(path, 0o600)
            state = State(path)
            self.assertEqual(state.answer_for("msg_1")["channel"], "chan")
            self.assertEqual(str(state.answer_for("msg_1").get("device") or ""), "")

    # -- work carried over an upgrade ------------------------------------


    def test_carried_over_work_is_given_up_once(self):
        with TempDir() as directory:
            path = directory / "state.json"
            state = State(path)
            state.suspend_runs({"chan": {"at": time.time(), "run": None,
                                         "waiting": [{"prompt": "hello"}]}})
            reopened = State(path)
            self.assertIn("chan", reopened.take_suspended())
            self.assertEqual(reopened.take_suspended(), {}, "it is read once")
            self.assertEqual(State(path).data["suspended"], {},
                             "the new process wrote the gap down")

    def test_carried_over_work_older_than_its_restart_is_dropped(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.suspend_runs({"chan": {"at": time.time() - 7200, "run": None,
                                         "waiting": [{"prompt": "old"}]}})
            self.assertEqual(state.take_suspended(), {})

    # -- access requests ---------------------------------------------------

    def test_a_request_waits_then_a_denial_cools_it_down(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            self.assertEqual(state.request_state("42"), "")
            state.remember_request("42", "Alex")
            self.assertEqual(state.request_state("42"), "pending")
            self.assertEqual(state.pending_requests(), 1)
            state.settle_request("42", allowed=False)
            self.assertEqual(state.request_state("42"), "cooling")
            self.assertEqual(state.request_state("42", now=time.time() + 3601), "")
            self.assertEqual(state.pending_requests(), 0)

    def test_a_pending_request_does_not_wait_for_ever(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.remember_request("42", "Alex")
            self.assertEqual(state.request_state("42", now=time.time() + 86401), "")
            self.assertEqual(state.pending_requests(now=time.time() + 86401), 0)

    def test_an_allow_leaves_no_request_behind(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.remember_request("42", "Alex")
            state.settle_request("42", allowed=True)
            self.assertEqual(state.request_state("42"), "")
            self.assertIsNone(state.request("42"))

    def test_a_request_that_never_reached_the_owner_can_be_sent_again(self):
        with TempDir() as directory:
            state = State(directory / "state.json")
            state.remember_request("42", "Alex")
            state.forget_request("42")
            self.assertEqual(state.request_state("42"), "")
            state.remember_request("42", "Alex")
            self.assertEqual(state.request_state("42"), "pending")
