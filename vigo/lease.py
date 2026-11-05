"""One channel's run at a time, across daemons.

Both daemons receive every command and each keeps its own queue, so two
questions for one channel can reach one device session at the same moment. The
device then merges them into a single turn, and one answer answers both.

The device's session is the only record both daemons can read, so the claim
lives in its metadata, beside the channel it was made for. Metadata is replaced
rather than merged, so every write here starts from what the session holds and
changes one key.

Liveness is the heartbeat moving, not a clock: two machines' clocks are not one
clock, and a claim that merely looked expired would put two questions into one
turn, which is the thing this exists to prevent. A claim whose beat has not
moved for `stale_after` seconds, measured on the reader's own clock, is dead
and may be taken.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import log
from .opencode_api import OpenCode, OpenCodeError

KEY = "vigoRun"       # the run's claim, inside the session's metadata
OWNER = "owner"       # the device whose daemon holds it
BEAT = "beat"         # the heartbeat that proves it is alive

BEAT_EVERY = 5.0      # seconds between heartbeat writes while a run works
STALE_AFTER = 20.0    # seconds a heartbeat may stand still before it is dead


class Lease:
    """This run's claim on a channel's session, kept on the device."""

    def __init__(self, client: OpenCode, session: str, owner: str,
                 beat_every: float = BEAT_EVERY, stale_after: float = STALE_AFTER,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.session = session
        self.owner = owner
        self.beat_every = beat_every
        self.stale_after = stale_after
        self.clock = clock
        self.beat = 0
        self.mine = False
        self._seen: object = None      # the beat we last saw of somebody else's
        self._moved_at: float | None = None   # when that beat last changed, on our clock
        self._wrote_at = 0.0           # when we last wrote ours

    # -- taking it --------------------------------------------------------

    def claim(self) -> str:
        """Take the session for this run: "mine", "theirs", or "unknown".

        "unknown" is a device that will not keep metadata: the run goes ahead,
        because a device that cannot say who holds the channel must not stop
        the work, and the log says why it went ahead.
        """
        held = self._held()
        if held is None:
            return "mine" if self._write(1) else "unknown"
        if str(held.get(OWNER) or "") == self.owner:
            # Our own claim: this daemon restarted while the run was working.
            beat = held.get(BEAT)
            start = beat + 1 if isinstance(beat, int) else 1
            return "mine" if self._write(start) else "unknown"
        if self._moved(held.get(BEAT)):
            return "theirs"  # it beat since we last looked: somebody is working
        if self._quiet_for() < self.stale_after:
            return "theirs"  # not still for long enough to call it dead
        log.info("lease: the claim on this session stopped beating; taking it")
        return "mine" if self._write(1) else "unknown"

    def renew(self) -> None:
        """Bump the heartbeat, now and then, while the run works."""
        if not self.mine or self.clock() - self._wrote_at < self.beat_every:
            return
        self._write(self.beat + 1)

    def release(self) -> None:
        """Give the channel up. A failed write leaves it to go stale."""
        if not self.mine:
            return
        self.mine = False
        self._write(None)

    # -- what the device says --------------------------------------------

    def _held(self) -> dict | None:
        try:
            metadata = self.client.session(self.session).get("metadata")
        except OpenCodeError as error:
            log.info(f"lease: cannot read it ({error.message})")
            return None
        if not isinstance(metadata, dict):
            return None
        held = metadata.get(KEY)
        return held if isinstance(held, dict) else None

    def _moved(self, beat: object) -> bool:
        """Whether somebody else's heartbeat advanced since we last looked."""
        now = self.clock()
        if beat != self._seen:
            self._seen = beat
            self._moved_at = now
            return True
        return False

    def _quiet_for(self) -> float:
        if self._moved_at is None:
            return 0.0
        return self.clock() - self._moved_at

    def _write(self, beat: int | None) -> bool:
        """Put our claim on the session, or take it off, keeping the rest."""
        try:
            current = self.client.session(self.session).get("metadata")
        except OpenCodeError as error:
            log.info(f"lease: cannot read the metadata to write it ({error.message})")
            return False
        merged = dict(current) if isinstance(current, dict) else {}
        if beat is None:
            merged.pop(KEY, None)
        else:
            merged[KEY] = {OWNER: self.owner, BEAT: beat}
        try:
            self.client.set_metadata(self.session, merged)
        except OpenCodeError as error:
            log.info(f"lease: the device would not take it ({error.message})")
            return False
        if beat is not None:
            self.beat = beat
            self.mine = True
            self._wrote_at = self.clock()
            self._seen = beat
            self._moved_at = self.clock()
        return True


def held_by_other(client: OpenCode, session: str, owner: str) -> bool:
    """Whether another daemon's claim on this session is there at all.

    One look, for a command that starts a turn without a run of its own
    (`/vigo goal`): it cannot take a lease it would never release, and it must
    not prompt into a turn somebody else holds. A claim left behind by a daemon
    that died refuses too, which is why the message points at `/vigo stop`: one
    look cannot tell a dead beat from a slow one, and guessing wrong here is
    the thing this module exists to prevent.
    """
    try:
        metadata = client.session(session).get("metadata")
    except OpenCodeError:
        return False  # cannot tell: the prompt is not held back by a guess
    held = metadata.get(KEY) if isinstance(metadata, dict) else None
    if not isinstance(held, dict):
        return False
    return str(held.get(OWNER) or "") != owner
