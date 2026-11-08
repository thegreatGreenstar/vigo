import unittest

from vigo import text


class ChunkTest(unittest.TestCase):
    def test_short_text_is_one_message(self):
        self.assertEqual(text.chunk("hello"), ["hello"])

    def test_empty_is_nothing(self):
        self.assertEqual(text.chunk("   \n "), [])

    def test_every_piece_fits(self):
        body = "\n".join(f"line {index} " + "x" * 40 for index in range(200))
        pieces = text.chunk(body, limit=300)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece), 304)  # a closing fence may add 4
            self.assertTrue(piece.strip())

    def test_a_fence_is_closed_and_reopened(self):
        body = "```python\n" + "\n".join(f"value_{index} = {index}" for index in range(200)) + "\n```"
        pieces = text.chunk(body, limit=400)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertEqual(piece.count("```") % 2, 0, "each message closes its fence")
            self.assertTrue(piece.startswith("```python"))
        rejoined = "\n".join(piece for piece in pieces)
        for index in range(200):
            self.assertIn(f"value_{index} = {index}", rejoined)

    def test_a_line_longer_than_a_message_is_cut(self):
        pieces = text.chunk("a" * 1000, limit=200)
        self.assertEqual("".join(pieces), "a" * 1000)
        for piece in pieces:
            self.assertLessEqual(len(piece), 200)

    def test_messages_keep_a_short_answer_whole_however_many_paragraphs(self):
        """Two paragraphs is the device's target, not the code's cut."""
        body = "one\n\ntwo\n\nthree"
        self.assertEqual(text.messages(body), [body])

    def test_messages_carry_a_long_answer_whole(self):
        body = "word " * 1200
        parts = text.messages(body)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), text.LIMIT)
        self.assertEqual("".join(parts), body, "nothing is lost between the messages")

    def test_a_runaway_answer_stops_and_says_so(self):
        parts = text.messages("word " * 6000, most=2, more="… (there is more)")
        self.assertEqual(len(parts), 2)
        self.assertIn("there is more", parts[-1])
        self.assertLessEqual(len(parts[-1]), 2000, "the line still fits a message")

    def test_messages_close_a_code_fence_they_cut(self):
        body = "before\n\n```python\n" + "print(1)\n" * 400 + "```\n\nafter"
        parts = text.messages(body)
        self.assertIn("print(1)", parts[0])
        for part in parts:
            self.assertEqual(part.count("```") % 2, 0, "each message closes its fence")

    def test_messages_are_empty_for_an_empty_answer(self):
        self.assertEqual(text.messages("  \n "), [])

    # -- secrets ----------------------------------------------------------

    def test_a_key_in_an_answer_is_replaced(self):
        answer, what = text.scrub("the key is sk-proj-abcdefghijklmnopqrstuvwxyz0123, keep it")
        self.assertEqual(what, "API key")
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwxyz0123", answer)
        self.assertIn("[redacted: API key]", answer)
        self.assertIn("keep it", answer, "the rest of the answer stands")

    def test_a_private_key_block_goes_whole(self):
        block = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----"
        answer, what = text.scrub(f"before\n{block}\nafter")
        self.assertEqual(what, "private key")
        self.assertNotIn("AAAA", answer)
        self.assertTrue(answer.startswith("before\n"))
        self.assertTrue(answer.endswith("\nafter"))

    def test_an_assigned_secret_keeps_its_label(self):
        answer, what = text.scrub("password = hunter2hunter2hunter2\nand nothing else")
        self.assertEqual(what, "secret")
        self.assertIn("password [redacted: secret]", answer)
        self.assertNotIn("hunter2", answer)

    def test_other_tokens_are_caught(self):
        for body, what in (
            ("ghp_0123456789abcdefghijABCDEFGHIJ", "GitHub token"),
            ("AKIAIOSFODNN7EXAMPLE", "AWS key"),
            ("AIza" + "A" * 35, "Google key"),
            ("xoxb-1234567890-abcdefghij", "Slack token"),
            ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijkl",
             "bearer token"),
            ("sk_live_" + "a" * 24, "Stripe key"),
        ):
            with self.subTest(what=what):
                answer, found = text.scrub(body)
                self.assertEqual(found, what)
                self.assertNotIn(body, answer)

    def test_the_bot_own_token_is_caught_before_a_channel_reads_it(self):
        """The answer is posted by the bot, so its own credential is the one
        an agent can read and a channel must never see."""
        token = "Bot " + "M" * 24 + ".GaBcDe." + "a" * 27
        answer, what = text.scrub(f"the token is {token}")
        self.assertEqual(what, "Discord token")
        self.assertNotIn("GaBcDe", answer)

    def test_a_discord_webhook_is_caught(self):
        url = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnop"
        answer, what = text.scrub(f"post with {url}")
        self.assertEqual(what, "Discord webhook")
        self.assertNotIn("abcdefghijklmnop", answer)

    def test_an_ordinary_answer_is_left_alone(self):
        for body in (
            "token = os.environ['OPENAI_API_KEY']",
            "set the password in the settings file",
            "see https://example.com/docs and mail dev@example.com",
            "the task-runner-config file",
            "path: /Users/someone/project/src/main.c",
        ):
            with self.subTest(body=body):
                self.assertEqual(text.scrub(body), (body, ""))

    def test_subject_names_the_first_line_and_drops_the_stop(self):
        self.assertEqual(text.subject("why is CI red?\n\nlong body"), "why is CI red")
        self.assertEqual(text.subject("fix the thing."), "fix the thing")
        self.assertEqual(text.subject("  \n "), "")
        self.assertLessEqual(len(text.subject("x" * 200)), 80)

    def test_a_failure_names_what_the_person_can_do_about_it(self):
        for said, line in (
            ("429 Too Many Requests", "rate limited"),
            ("insufficient_quota: no credits left", "no quota left"),
            ("maximum context length is 128000 tokens", "out of context"),
            ("401 Unauthorized", "refused the key"),
            ("something odd happened", "the agent failed"),
            ("", "the agent failed"),
        ):
            with self.subTest(said=said):
                self.assertIn(line, text.cause(said))

    def test_tail_and_shorten(self):
        self.assertEqual(text.tail("abcdef", 4), "…def")
        self.assertEqual(text.shorten("abcdef", 4), "abc…")

    def test_durations(self):
        self.assertEqual(text.duration(12), "12s")
        self.assertEqual(text.duration(75), "1m 15s")
        self.assertEqual(text.duration(3700), "1h 01m")

    def test_money_and_tokens(self):
        self.assertEqual(text.money(0), "$0")
        self.assertEqual(text.money(0.002), "$0.0020")
        self.assertEqual(text.money(1.5), "$1.50")
        self.assertEqual(text.tokens(999), "999")
        self.assertEqual(text.tokens(12000), "12.0k")


class ShareableTest(unittest.TestCase):
    """What the public queue may show of a task, and what it may not."""

    def test_a_general_task_is_shown(self):
        for said in ("fix the failing CI job", "update the README", "why is the build red"):
            with self.subTest(said=said):
                self.assertTrue(text.shareable(said))

    def test_a_path_is_not_shown(self):
        for said in ("read /Users/someone/notes.txt",
                     "open ~/Projects/thing",
                     "edit C:\\Users\\someone\\file.txt",
                     "read /home/someone/.ssh/config"):
            with self.subTest(said=said):
                self.assertFalse(text.shareable(said))

    def test_an_address_or_an_id_is_not_shown(self):
        for said in ("mail someone@example.com",
                     "ssh aarons-macbook.local",
                     "ping 192.168.1.20",
                     "see https://example.com/private",
                     "ask <@145512144145512144>",
                     "the channel is 145512144145512144",
                     "job 8f14e45f-ceea-467a-9e4c-0f0a1b2c3d4e"):
            with self.subTest(said=said):
                self.assertFalse(text.shareable(said))

    def test_a_secret_is_not_shown(self):
        self.assertFalse(text.shareable("use sk-ant-abcdefghijklmnopqrstuvwxyz0123"))

    def test_a_device_name_is_not_shown(self):
        names = ("macbook", "pc")
        self.assertFalse(text.shareable("run it on macbook", names))
        self.assertFalse(text.shareable("the PC is off", names))
        self.assertTrue(text.shareable("run it on the desktop", names))


if __name__ == "__main__":
    unittest.main()
