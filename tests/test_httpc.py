"""One HTTP call: an answer comes back whole, and a redirect is not followed."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vigo import httpc


class CallTest(unittest.TestCase):
    """A local server, because every call here is one plain request."""

    def setUp(self):
        self.seen: list[tuple[str, str]] = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # keep the tests quiet
                pass

            def do_GET(self) -> None:
                test.seen.append((self.path, self.headers.get("Authorization", "")))
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{port}/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_a_plain_answer_comes_back_whole(self) -> None:
        status, headers, raw = httpc.call(
            f"{self.url}/ok", headers={"Authorization": "Bot secret"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(httpc.parse_json(raw), {"ok": True})
        self.assertEqual(self.seen, [("/ok", "Bot secret")])

    def test_a_redirect_is_the_answer_and_is_not_followed(self) -> None:
        """`urllib` would carry `Authorization` to the host the Location names."""
        status, _, raw = httpc.call(
            f"{self.url}/start", headers={"Authorization": "Bot secret"},
        )
        self.assertEqual(status, 302)
        self.assertEqual(raw, b"")
        self.assertEqual([path for path, _ in self.seen], ["/start"],
                         "the redirect target was never asked")

    def test_an_answer_past_the_bound_is_refused(self) -> None:
        # The bound is a parameter, not a module global: the parallel runner
        # shares the module, and a case that lowered it for every other call
        # made a lease read fail as "the answer is larger than 8 bytes".
        with self.assertRaises(httpc.HttpError) as caught:
            httpc.call(f"{self.url}/ok", most_bytes=8)
        self.assertIn("larger than", str(caught.exception))

    def test_a_where_line_holds_no_user_or_password(self) -> None:
        self.assertEqual(httpc._where("http://user:secret@example.com/a/b"), "example.com")

    def test_nothing_answered_is_an_httperror(self) -> None:
        with self.assertRaises(httpc.HttpError):
            httpc.call("http://127.0.0.1:1/", timeout=1.0)


if __name__ == "__main__":
    unittest.main()
