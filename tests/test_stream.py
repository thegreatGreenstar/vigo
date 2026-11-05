"""The SSE reader: it gives the connection back when the run is over."""

import sys
import threading
import traceback
import unittest

from vigo.opencode_api import EventStream
from tests.fake_opencode import FakeOpenCode


class EventStreamTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeOpenCode("macbook")
        self.addCleanup(self.device.close)

    def test_close_ends_the_reader_and_its_thread(self):
        """Closing hands the connection back instead of leaving it reading.

        Setting the flag alone left the reader blocked in a read until the
        device had something to say: one connection and one thread per run,
        and on Windows enough of them to drop the connection the next run
        needed.
        """
        stop = threading.Event()
        stream = EventStream(self.device.url, "pw", stop).start()
        self.addCleanup(stream.close)
        self.assertTrue(stream.ready.wait(5.0), "the stream connected")
        stream.close()
        stream.thread.join(10.0)
        if stream.thread.is_alive():
            # Say where it is parked, not just that it is: the wake differs by
            # platform, and a bare assertion cannot tell which one it is.
            frame = sys._current_frames().get(stream.thread.ident)
            where = "".join(traceback.format_stack(frame)) if frame else "no frame"
            self.fail(f"the reader is still running, in:\n{where}")

    def test_an_event_reaches_the_queue(self):
        stop = threading.Event()
        stream = EventStream(self.device.url, "pw", stop).start()
        self.addCleanup(stream.close)
        self.assertTrue(stream.ready.wait(5.0))
        session = self.device.new_session("t")
        self.device.emit("session.idle", {"sessionID": session})
        event = stream.events.get(timeout=5.0)
        self.assertEqual(event.get("type"), "session.idle")
        self.assertEqual(event["data"]["sessionID"], session)
