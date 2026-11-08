"""The status Discord shows for this daemon, from what the daemon is doing.

Discord takes a status over the gateway: online, idle, do not disturb, or
invisible.  The bot shows what it is doing rather than a light that is always
green:

- `online` (green): this daemon answers something, or answered anything within
  the last five minutes.
- `idle`: no answer within those five minutes, and a server can take a
  question.
- `dnd` (red): every server that answers is at work on a turn, and nothing was
  answered recently, so a question would wait behind that work.  A turn at
  work is what the server is doing, not a session count at its limit.  It is
  also what a daemon shows when no server answers it at all: nothing can be
  asked there, so a question would not run.

The bot is never shown invisible.  That status makes the account look offline
while its gateway connection is up, and a daemon that cannot reach its own
OpenCode server does not know that another daemon cannot answer.  A bot with
no gateway connection is offline already, because Discord draws that itself.

Every device runs a daemon, and each one sets the status of the same bot
account over its own gateway connection, so the last update to arrive is the
one Discord keeps.  Each daemon therefore shows the one status that the facts
on any daemon support, best first: online, then idle, then do not disturb.
Two daemons that see the same servers then agree whatever order their updates
arrive in, and one machine at work cannot turn the account red while another
machine is free.

Only a change is sent.  Discord limits presence updates, and the gateway keeps
the value and sends it again after a reconnect.
"""

from __future__ import annotations

import time
from collections.abc import Callable

ONLINE_FOR = 300.0  # seconds a finished answer keeps the bot online
TICK = 5.0          # seconds between looks at what the daemon does
BUSY_EVERY = 30.0   # seconds between reads of the servers' busy sessions
BUSY_WORKING = TICK  # while this daemon thinks, the light must turn at once
PEER_EVERY = 15.0   # seconds between reads of what the other daemons show

# What each daemon shows when the daemons disagree: the best news wins.  The
# account never looks offline, so do not disturb is the floor; `invisible` is
# still read so that a daemon of an older version cannot make it offline.
RANK = {"online": 3, "idle": 2, "dnd": 1, "invisible": 0}


def status_for(*, working: bool, up: int, busy: int, last: float | None,
               now: float) -> str:
    """The status that fits these facts, best first.

    `up` is how many servers answer, `busy` how many of those have a turn at
    work, and `last` when the bot last answered anything (None for never).
    A server that answers with a turn at work is not the same as a server that
    is down: one can be asked later, the other cannot be asked at all.

    The order is the one the daemons agree on.  Work of this daemon is green,
    not red, because the account shows the work being done: a question waits
    behind it, but a light that turns red each time the bot thinks says the
    bot is unavailable while it runs.

    A daemon whose servers are all down shows do not disturb, never invisible:
    it is still connected to Discord, and another daemon may be able to answer.
    """
    if up == 0:
        return "dnd"
    if working or (last is not None and now - last <= ONLINE_FOR):
        return "online"
    if busy >= up:
        return "dnd"
    return "idle"


def merge(*statuses: str) -> str:
    """The status all daemons show: the best of what each one sees.

    A daemon that did not answer at all is left out by the caller, so a peer
    link that is down does not make the account look offline while this
    machine answers.  An unknown status counts as the lowest one, because a
    daemon that says nothing useful cannot promise that it can take a
    question.  The result never goes below do not disturb: the merged status
    is sent over a live gateway connection, and that is not offline.
    """
    best = "dnd"
    for status in statuses:
        if RANK.get(status, 0) > RANK.get(best, 0):
            best = status
    return best


class Presence:
    """What this daemon shows, and when it last answered anything."""

    def __init__(self, send: Callable[[str], None],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.send = send
        self.clock = clock
        self.last: float | None = None
        self.status = ""   # what is on the gateway
        self.own = ""      # what this daemon sees, before the others are merged
        self.stopped = False

    def touch(self) -> None:
        """The bot answered something: it stays online for five more minutes."""
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
