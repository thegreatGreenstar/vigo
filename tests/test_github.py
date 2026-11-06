"""The GitHub work an answer names is put in one line before its prose."""

import unittest

from vigo import github

PR = "https://github.com/scarletnx/ScarletNX/pull/1251"
COMMIT = "https://github.com/scarletnx/ScarletNX/commit/aaf1a0d0b0c1d2e3f4"


class ArtifactsTest(unittest.TestCase):
    def test_a_pull_request_is_named_by_its_number(self):
        found = github.artifacts(f"Opened {PR}.")
        self.assertEqual([(a.kind, a.key) for a in found], [("pull", "#1251")])

    def test_a_commit_is_named_by_seven_characters(self):
        found = github.artifacts(COMMIT)
        self.assertEqual([a.key for a in found], ["aaf1a0d"])

    def test_an_issue_and_a_discussion_keep_their_word(self):
        found = github.artifacts(
            "https://github.com/o/r/issues/12 and https://github.com/o/r/discussions/9")
        self.assertEqual([a.label for a in found], ["issue #12", "discussion #9"])

    def test_a_short_reference_becomes_a_link(self):
        found = github.artifacts("fixed in scarletnx/ScarletNX#1258 today")
        self.assertEqual([a.label for a in found], ["scarletnx/ScarletNX#1258"])
        self.assertEqual(found[0].url, "https://github.com/scarletnx/ScarletNX/issues/1258")

    def test_the_order_is_the_order_of_the_text(self):
        found = github.artifacts(f"{PR} then {COMMIT}")
        self.assertEqual([a.key for a in found], ["#1251", "aaf1a0d"])

    def test_one_piece_of_work_is_named_once(self):
        found = github.artifacts(
            f"{PR} and {PR.replace('/pull/', '/issues/')} and ScarletNX#1251")
        self.assertEqual(len(found), 1, "the same number in one repository is one thing")

    def test_two_repositories_keep_their_own_numbers(self):
        found = github.artifacts(
            "https://github.com/o/one/pull/7 https://github.com/o/two/pull/7")
        self.assertEqual(len(found), 2)

    def test_a_place_is_not_work(self):
        for text in (
            "https://github.com/scarletnx/ScarletNX",
            "https://github.com/scarletnx/ScarletNX/tree/main/src",
            "https://github.com/scarletnx/ScarletNX/blob/main/README.md#L4",
            "https://github.com/scarletnx/ScarletNX/compare/a...b",
            "https://gitlab.com/o/r/merge_requests/3",
            "no link at all",
        ):
            with self.subTest(text=text):
                self.assertEqual(github.artifacts(text), [])


class MarkTest(unittest.TestCase):
    def test_a_pull_request_leads_the_answer(self):
        line = github.mark(f"the fix is right.\n\n{PR}")
        self.assertEqual(line, f"GitHub: [PR #1251]({PR})")

    def test_nothing_to_say_is_an_empty_line(self):
        self.assertEqual(github.mark("the tests pass"), "")

    def test_a_link_already_first_is_not_repeated(self):
        self.assertEqual(github.mark(f"{PR}\n\nthe fix is right."), "")

    def test_only_the_work_that_is_not_first_is_added(self):
        line = github.mark(f"{PR}\n\nand {COMMIT}")
        self.assertEqual(line, f"GitHub: [commit aaf1a0d]({COMMIT})")

    def test_more_than_six_says_how_many_were_left(self):
        text = "eight asks:\n\n" + "\n\n".join(
            f"https://github.com/o/r/pull/{number}" for number in range(1, 9))
        line = github.mark(text)
        self.assertIn("and 2 more", line)
        self.assertNotIn("#7", line)
        self.assertEqual(line.count("[PR"), github.MOST)


if __name__ == "__main__":
    unittest.main()
