"""A stall watchdog: it says where every thread is when a loop stops moving.

A hang has no exception and no line.  The log of a hung daemon is silence, and
the first question is which loop stopped and where it is parked.  Each long
loop calls `beat(name)` often.  This thread compares those beats with the
clock, and when one stands still past `limit` seconds it writes one error line
with the stack of every thread.  It writes once for each stall, because a
second copy of the same stacks says nothing new and buries the first.

A loop that moves again clears its stall, so the log shows both ends: where it
was stuck, and how long it was there before it went on.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from collections.abc import Callable

from . import log

LIMIT = 90.0   # seconds a beat may stand still before the log says so
TICK = 5.0     # seconds between checks


class Watchdog:
    """The beats, and the thread that judges them."""

    def __init__(self, limit: float = LIMIT, tick: float = TICK,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.tick = tick
        self.clock = clock
        self.lock = threading.Lock()
        self.beats: dict[str, list[float]] = {}   # name -> [last beat, reported for]
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    # -- what the loops say -------------------------------------------------

    def beat(self, name: str, at: float | None = None) -> None:
        """This loop moved.  A stall it just left is reported as ended."""
        now = self.clock() if at is None else at
        quiet = 0.0
        with self.lock:
            entry = self.beats.get(name)
            if entry is None:
                self.beats[name] = [now, 0.0]
                return
            if entry[1]:
                quiet = now - entry[0]
            entry[0] = now
            entry[1] = 0.0
        if quiet:
            log.info(f"watchdog: {name} moves again after {quiet:.1f}s still")

    def drop(self, name: str) -> None:
        """The loop is over on purpose, so it is no longer waited for."""
        with self.lock:
            self.beats.pop(name, None)

    # -- what the watchdog says --------------------------------------------

    def stalled(self, now: float | None = None) -> list[tuple[str, float]]:
        """The beats that are past the limit, each reported once."""
        moment = self.clock() if now is None else now
        found = []
        with self.lock:
            for name, entry in self.beats.items():
                if entry[1] or moment - entry[0] <= self.limit:
                    continue
                entry[1] = moment - entry[0]
                found.append((name, entry[1]))
        return found

    def check(self, now: float | None = None) -> list[str]:
        """Report the stalls that are new, with the stack of every thread."""
        stalls = self.stalled(now)
        if not stalls:
            return []
        said = ", ".join(f"{name} for {seconds:.1f}s" for name, seconds in stalls)
        log.error(f"watchdog: no movement in {said}; the stacks of every thread follow")
        log.error(stacks())
        return [name for name, _seconds in stalls]

    def start(self) -> Watchdog:
        if self.thread is not None and self.thread.is_alive():
            return self
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, name="vigo-watchdog", daemon=True)
        self.thread.start()
        log.debug(f"watchdog: on; a loop still for {self.limit:.0f}s is reported")
        return self

    def stop(self) -> None:
        self.stopped.set()
        thread = self.thread
        if thread is not None:
            thread.join(1.0)

    def _run(self) -> None:
        while not self.stopped.wait(self.tick):
            self.check()


def stacks() -> str:
    """Every live thread and the frames it is parked in, by name."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    rows: list[str] = []
    for ident, frame in sys._current_frames().items():
        name = names.get(ident, f"thread-{ident}")
        if name == "vigo-watchdog":
            continue
        rows.append(f"thread {name} is at:")
        rows.extend(traceback.format_stack(frame))
    return "".join(rows).rstrip()


WATCH = Watchdog()


def beat(name: str, at: float | None = None) -> None:
    WATCH.beat(name, at)


def drop(name: str) -> None:
    WATCH.drop(name)


def start() -> Watchdog:
    return WATCH.start()


def stop() -> None:
    WATCH.stop()
