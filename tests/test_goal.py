"""When an ask is work to be done, and when it is a question."""

import unittest

from vigo.goal import is_task


class IsTaskTest(unittest.TestCase):
    def test_a_request_is_a_task(self):
        for said in ("fix the failing test",
                     "rename the table to devices",
                     "add a test for the election",
                     "can you fix the tests?",
                     "could you rename this",
                     "please squash those commits",
                     "go make the build green",
                     "let's ship it"):
            self.assertTrue(is_task(said), f"{said!r} is work")

    def test_a_question_is_not_a_task(self):
        for said in ("why is CI red?",
                     "what does status say",
                     "how does the election work?",
                     "is everything good for me to shutdown?",
                     "where does the state file live",
                     "who may use the bot",
                     "did the push land",
                     "should I restart it?"):
            self.assertFalse(is_task(said), f"{said!r} wants an answer")

    def test_nothing_asked_is_no_task(self):
        for said in ("", "   ", "..."):
            self.assertFalse(is_task(said))

    def test_a_question_mark_does_not_make_a_request_a_question(self):
        self.assertTrue(is_task("can you read this?"))
        self.assertFalse(is_task("what changed?"))

    def test_a_task_without_a_question_mark_stays_one(self):
        self.assertTrue(is_task("check the log"))
        self.assertFalse(is_task("check the log?"), "the ? turns it into a question")
