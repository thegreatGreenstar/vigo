"""What the log answers for a daemon that hangs or dies.

A hang writes no line at all, so these tests check the two things that make
one readable afterwards: a stamp with the thread that wrote it, and a stack
when a loop stops moving.  A crash is checked the same way, from the hooks
that catch what no `except` did.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import threading
import unittest
from types import SimpleNamespace

from vigo import log, watchdog
from vigo.ask import Ask
from tests.support import TempDir, load_settings

# `log.attach` is one sink for the process.  The suite runs its cases on
# threads, so one capture at a time, or a capture could leave another's behind.
CAPTURE = threading.Lock()


class Capture:
    """A stand-in for stderr that keeps what the bot wrote."""

    def __init__(self) -> None:
        self.text = io.StringIO()

    def write(self, said: str) -> int:
        return self.text.write(said)

    def flush(self) -> None:
        pass

    def __enter__(self) -> Capture:
        log.attach(self)
        return self

    def __exit__(self, *args: object) -> None:
        log.attach(None)

    def getvalue(self) -> str:
        return self.text.getvalue()


class Captured(unittest.TestCase):
    """A test that reads the log, and one capture for the process at a time."""

    def setUp(self) -> None:
        CAPTURE.acquire()
        self.addCleanup(CAPTURE.release)
        self.addCleanup(log.level, "info")
        self.addCleanup(log.attach, None)
        log.level("info")


class LogTest(Captured):
    def test_a_line_carries_the_date_the_thread_and_the_elapsed_time(self):
        with Capture() as out:
            log.info("events: up")
        line = out.getvalue().splitlines()[0]
        name = threading.current_thread().name
        self.assertRegex(
            line,
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} "
            rf"\+\s*\d+\.\d{{3}}s info  \[{re.escape(name)}\] events: up$",
        )

    def test_debug_stays_out_at_info(self):
        with Capture() as out:
            log.debug("http: a call said 200 in 4ms")
        self.assertNotIn("http: a call said 200", out.getvalue())

    def test_a_failure_carries_the_stack_in_the_same_block(self):
        error = ValueError("bad input")
        try:
            raise error
        except ValueError:
            with Capture() as out:
                log.exception("app: a handler broke")
        said = out.getvalue()
        name = threading.current_thread().name
        self.assertIn(f"error [{name}] app: a handler broke", said)
        self.assertIn("Traceback (most recent call last)", said)
        self.assertIn("ValueError: bad input", said)
        self.assertIn("test_log.py", said)

    def test_a_failure_outside_its_except_block_still_carries_the_stack(self):
        """A pool future hands its error to a callback after the block has ended."""
        try:
            raise RuntimeError("the task died")
        except RuntimeError as caught:
            error = caught
        with Capture() as out:
            log.failure(f"app: a handler broke: {type(error).__name__}: {error}", error)
        said = out.getvalue()
        self.assertIn("Traceback (most recent call last)", said)
        self.assertIn("RuntimeError: the task died", said)

    def test_the_uncaught_hooks_write_one_line_and_the_stack(self):
        saved = (sys.excepthook, threading.excepthook, sys.unraisablehook)
        self.addCleanup(self._restore, saved)
        with Capture() as out:
            log.install()

            def die() -> None:
                raise RuntimeError("a thread died")

            thread = threading.Thread(target=die, name="worker-x")
            thread.start()
            thread.join()
            try:
                raise ValueError("the main thread died")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        said = out.getvalue()
        self.assertIn("uncaught in worker-x: RuntimeError: a thread died", said)
        self.assertIn("uncaught: ValueError: the main thread died", said)
        self.assertIn("Traceback (most recent call last)", said)
        self.assertIn("test_log.py", said)

    def _restore(self, saved: tuple[object, object, object]) -> None:
        sys.excepthook, threading.excepthook, sys.unraisablehook = saved  # type: ignore[assignment]


class WatchdogTest(Captured):
    def test_a_stall_is_reported_once_with_every_thread(self):
        watch = watchdog.Watchdog(limit=3.0, clock=lambda: 0.0)
        watch.beat("gateway", at=0.0)
        with Capture() as out:
            self.assertEqual(watch.check(now=2.0), [])
            self.assertEqual(watch.check(now=4.0), ["gateway"])
        said = out.getvalue()
        self.assertIn("watchdog: no movement in gateway for 4.0s", said)
        self.assertIn("thread MainThread is at:", said)
        self.assertIn("test_a_stall_is_reported_once_with_every_thread", said)
        with Capture() as out:
            self.assertEqual(watch.check(now=5.0), [], "a stall is reported once")
        self.assertNotIn("watchdog: no movement in gateway", out.getvalue())

    def test_a_loop_that_moves_again_clears_its_stall(self):
        watch = watchdog.Watchdog(limit=3.0, clock=lambda: 0.0)
        watch.beat("gateway", at=0.0)
        with Capture() as out:
            self.assertEqual(watch.check(now=4.0), ["gateway"])
            self.assertIn("gateway for 4.0s", out.getvalue())
        with Capture() as out:
            watch.beat("gateway", at=6.0)
        self.assertIn("watchdog: gateway moves again after 6.0s still", out.getvalue())
        with Capture() as out:
            self.assertEqual(watch.check(now=8.0), [])
            self.assertEqual(watch.check(now=10.0), ["gateway"])
        self.assertIn("gateway for 4.0s", out.getvalue())

    def test_a_loop_that_ends_is_not_waited_for(self):
        watch = watchdog.Watchdog(limit=1.0, clock=lambda: 0.0)
        watch.beat("ask:chan", at=0.0)
        watch.drop("ask:chan")
        with Capture() as out:
            self.assertEqual(watch.check(now=60.0), [])
        self.assertNotIn("watchdog:", out.getvalue())

    def test_a_broken_run_says_so_with_its_stack_and_gives_up_its_beat(self):
        worker = Ask.__new__(Ask)
        worker.channel_id = "chan-test"
        worker.device_name = "macbook"
        worker.lease = None
        worker.run = object()  # type: ignore[assignment]
        told: list[str] = []
        ended: list[str] = []
        worker._tell = lambda content: told.append(content)  # type: ignore[method-assign]

        def broken() -> None:
            raise ValueError("the device vanished")

        worker._run = broken  # type: ignore[method-assign]
        worker.app = SimpleNamespace(  # type: ignore[assignment]
            unregister_run=lambda channel, run=None: ended.append(channel),
        )
        watchdog.beat("ask:chan-test")
        with Capture() as out:
            worker.run_it()
        said = out.getvalue()
        self.assertIn("ask: broke on macbook: ValueError: the device vanished", said)
        self.assertIn("Traceback (most recent call last)", said)
        self.assertEqual(told, ["The run broke. The log says what."])
        self.assertEqual(ended, ["chan-test"])
        self.assertNotIn("ask:chan-test", watchdog.WATCH.beats)

    def test_a_handler_that_raises_is_logged_and_the_bot_lives(self):
        from vigo.app import App
        from vigo.discord_api import Discord
        from vigo.state import State

        with TempDir() as directory:
            settings = load_settings(directory)
            app = App(settings, api=Discord("token", settings.app_id),
                      state=State(directory / "state.json"))
            self.addCleanup(app.shutdown)

            def broken(payload: dict) -> None:
                raise RuntimeError("the handler died")

            app.handle_interaction = broken  # type: ignore[method-assign]
            with Capture() as out:
                app.on_event("INTERACTION_CREATE", {"id": "1"})
                app.pool.shutdown(wait=True)
            said = out.getvalue()
            self.assertIn("app: a handler broke: RuntimeError: the handler died", said)
            self.assertIn("Traceback (most recent call last)", said)


class HttpLogTest(Captured):
    def test_a_call_is_named_by_its_host_only(self):
        from vigo import httpc

        log.level("debug")
        with Capture() as out:
            with contextlib.suppress(httpc.HttpError):
                # A refused connection is enough: the line is written before the raise.
                httpc.call("http://127.0.0.1:1/vigo/info?token=secret", timeout=1.0)
        said = out.getvalue()
        self.assertIn("http: GET 127.0.0.1:1 failed after", said)
        self.assertNotIn("/vigo/info", said, "a path can carry a token and stays out")
        self.assertNotIn("secret", said)


if __name__ == "__main__":
    unittest.main()
