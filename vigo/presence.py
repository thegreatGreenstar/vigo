"""The status Discord shows for this daemon, from what the daemon is doing.

Discord takes a status over the gateway: online, idle, do not disturb, or
invisible. The bot shows what it is doing rather than a light that is always
green:

- `invisible`: no OpenCode server answers, so there is nothing to ask.
- `dnd`: every server that answers is at work on a turn, including the turn
  this daemon is thinking in.  A question would wait behind that work, so this
  is the do-not-disturb the owner sees without asking.  A turn at work is what
  the server is doing, not a session count at its limit.
- `online`: an interaction is being answered, an ask waits, or the bot answered
  anything within the last two minutes.
- `idle`: none of the above.

Only a change is sent. Discord limits presence updates, and the gateway keeps
the value and sends it again after a reconnect.
"""

from __future__ import annotations

import time
from collections.abc import Callable

ONLINE_FOR = 120.0  # seconds a finished answer keeps the bot online
TICK = 5.0          # seconds between looks at what the daemon does
BUSY_EVERY = 30.0   # seconds between reads of the servers' busy sessions
BUSY_WORKING = TICK  # while this daemon thinks, the light must turn at once


def status_for(*, working: bool, up: int, busy: int, last: float | None,
               now: float) -> str:
    """The status that fits these facts, best first.

    `up` is how many servers answer, `busy` how many of those have a turn at
    work, and `last` when the bot last answered anything (None for never).
    A server that answers with a turn at work is not the same as a server that
    is down: one can be asked later, the other cannot be asked at all.  A turn
    at work is what the server is doing now, not a session list that is full,
    so it is what makes the status do not disturb.
    """
    if up == 0:
        return "invisible"
    if busy >= up:
        return "dnd"
    if working or (last is not None and now - last <= ONLINE_FOR):
        return "online"
    return "idle"


class Presence:
    """What this daemon shows, and when it last answered anything."""

    def __init__(self, send: Callable[[str], None],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.send = send
        self.clock = clock
        self.last: float | None = None
        self.status = ""
        self.stopped = False

    def touch(self) -> None:
        """The bot answered something: it stays online for two more minutes."""
        self.last = self.clock()

    def now(self) -> float:
        return self.clock()

    def show(self, status: str) -> None:
        """Send the status when it differs from the one already shown."""
        if status == self.status:
            return
        self.status = status
        self.send(status)

    def wait(self, seconds: float) -> None:
        """Wait between looks, in small steps so a stop is noticed at once."""
        end = time.monotonic() + seconds
        while not self.stopped and time.monotonic() < end:
            time.sleep(0.1)
