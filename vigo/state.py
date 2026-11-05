"""What this device remembers between runs.

Only ids and times: which Discord channel is talking to which OpenCode
session, and which permission card is which request.  The file is per device,
in `~/.local/state/vigo/state.json`, folder 700 and file 600.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import log


def default_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "vigo"


class State:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or (default_dir() / "state.json"))
        self.lock = threading.Lock()
        self.data: dict = {"sessions": {}, "tickets": {}, "channels": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self.data.update(loaded)
                self.data.setdefault("sessions", {})
                self.data.setdefault("tickets", {})
                self.data.setdefault("channels", {})
        except (OSError, json.JSONDecodeError) as error:
            broken = self.path.with_suffix(".bad")
            try:
                os.replace(self.path, broken)
            except OSError:
                pass
            log.error(f"state: {self.path} could not be read ({error}); kept as {broken}")

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        temp = self.path.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)

    # -- channels and sessions -------------------------------------------

    def session_for(self, channel_id: str) -> dict | None:
        with self.lock:
            return self.data["sessions"].get(channel_id)

    def remember_session(self, channel_id: str, device: str, session_id: str, title: str) -> None:
        with self.lock:
            self.data["sessions"][channel_id] = {
                "device": device,
                "session": session_id,
                "title": title,
                "at": int(time.time()),
            }
            self._write()

    def forget_session(self, channel_id: str) -> None:
        with self.lock:
            self.data["sessions"].pop(channel_id, None)
            self._write()

    # -- permission cards -------------------------------------------------

    def new_ticket(self, ticket: str, device: str, session: str, request: str,
                   channel: str) -> None:
        with self.lock:
            self.data["tickets"][ticket] = {
                "device": device,
                "session": session,
                "request": request,
                "channel": channel,
                "at": int(time.time()),
            }
            self._write()

    def ticket(self, ticket: str) -> dict | None:
        with self.lock:
            return self.data["tickets"].get(ticket)

    def close_ticket(self, ticket: str) -> None:
        with self.lock:
            self.data["tickets"].pop(ticket, None)
            self._write()

    def prune_tickets(self, age: int = 86400) -> None:
        cutoff = time.time() - age
        with self.lock:
            stale = [
                key for key, value in self.data["tickets"].items()
                if value.get("at", 0) < cutoff
            ]
            if stale:
                for key in stale:
                    self.data["tickets"].pop(key, None)
                self._write()

    # -- the allowlist's message in Discord -------------------------------

    def state_message(self, channel_id: str) -> str | None:
        with self.lock:
            return self.data["channels"].get(channel_id)

    def remember_state_message(self, channel_id: str, message_id: str) -> None:
        with self.lock:
            self.data["channels"][channel_id] = message_id
            self._write()
