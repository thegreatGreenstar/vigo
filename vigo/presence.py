"""Which daemons are running, and which version each one is.

Two daemons share no protocol: each serves its own interactions and its own
device. The one thing they share is Discord, so that is where they leave their
names, in a message of their own, and where `devices` looks to warn about a
daemon left on an older build.
"""

from __future__ import annotations

import json
import time

from . import __version__, log
from .discord_api import Discord, DiscordError
from .state import State

MARKER = "VIGO-DAEMONS 1"
SCHEMA = 1
KEEP = 7 * 24 * 3600  # forget a daemon not seen for a week


class Presence:
    def __init__(self, device: str, api: Discord, state: State, home):
        self.device = device
        self.api = api
        self.state = state
        self._home = home  # returns the channel id the allowlist uses
        self._message = ""

    def _find(self) -> str | None:
        channel = self._home()
        remembered = self.state.daemons_message(channel)
        if remembered:
            return remembered
        for message in self.api.messages(channel, limit=50):
            if (message.get("content") or "").startswith(MARKER):
                self.state.remember_daemons_message(channel, message["id"])
                return message["id"]
        return None

    def read(self) -> dict[str, dict]:
        channel = self._home()
        message = self._find()
        if not message:
            return {}
        reply = self.api._call("GET", f"/channels/{channel}/messages/{message}")
        if not reply.ok or not isinstance(reply.body, dict):
            return {}
        raw = (reply.body.get("content") or "")[len(MARKER):].strip()
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            log.info("presence: the daemons message is not JSON; ignoring it")
            return {}
        if not isinstance(data, dict) or data.get("v") != SCHEMA:
            log.info("presence: the daemons message is a version we do not read")
            return {}
        daemons = data.get("daemons")
        return daemons if isinstance(daemons, dict) else {}

    def announce(self) -> None:
        """Leave this daemon's name and version, and forget the ones long gone."""
        try:
            channel = self._home()
            found = self.read()
            now = int(time.time())
            found[self.device] = {"version": __version__, "at": now}
            kept = {
                name: info for name, info in found.items()
                if isinstance(info, dict) and now - int(info.get("at", 0)) < KEEP
            }
            body = {"v": SCHEMA, "daemons": kept}
            content = f"{MARKER}\n{json.dumps(body, separators=(',', ':'))}"
            message = self._find()
            if message:
                reply = self.api._call("PATCH", f"/channels/{channel}/messages/{message}",
                                       {"content": content, "allowed_mentions": {"parse": []}})
            else:
                reply = self.api._call("POST", f"/channels/{channel}/messages",
                                       {"content": content, "allowed_mentions": {"parse": []}})
            if reply.ok and isinstance(reply.body, dict):
                self.state.remember_daemons_message(channel, reply.body["id"])
                log.info(f"presence: {self.device} is {__version__}")
            else:
                log.info(f"presence: Discord would not take it ({reply.status})")
        except (DiscordError, OSError) as error:
            log.info(f"presence: not announced ({error})")

    def stale(self) -> list[str]:
        """Daemons that are running a different build from this one."""
        return [
            f"`{name}` runs vigo {info.get('version', '?')}, not {__version__}"
            for name, info in self.read().items()
            if name != self.device and info.get("version") not in (None, __version__)
        ]
