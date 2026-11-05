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


if __name__ == "__main__":
    unittest.main()
