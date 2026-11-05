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
        self.data: dict = {"sessions": {}, "tickets": {}, "forms": {}, "channels": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self.data.update(loaded)
                self.data.setdefault("sessions", {})
                self.data.setdefault("tickets", {})
                self.data.setdefault("forms", {})
                self.data.setdefault("channels", {})
                self._migrate_sessions()
        except (OSError, json.JSONDecodeError) as error:
            broken = self.path.with_suffix(".bad")
            try:
                os.replace(self.path, broken)
            except OSError:
                pass
            log.error(f"state: {self.path} could not be read ({error}); kept as {broken}")

    def _migrate_sessions(self) -> None:
        """One session per channel becomes one per channel and device.

        A channel may be asked on either device, and keeping only the newest
        made every switch create a fresh session on the device it returned to.
        An older file holds `{channel: {device, session, title, at}}`; it is
        the session on that device, so it becomes one entry under its name.
        """
        sessions = self.data.get("sessions")
        if not isinstance(sessions, dict):
            self.data["sessions"] = {}
            return
        for channel, entry in list(sessions.items()):
            if isinstance(entry, dict) and "session" in entry:
                sessions[channel] = {str(entry.get("device") or ""): entry}

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
        """The newest session this channel has, on whichever device it is on."""
        with self.lock:
            line = self.data["sessions"].get(channel_id) or {}
            if not isinstance(line, dict) or not line:
                return None
            newest = None
            for entry in line.values():
                if not isinstance(entry, dict):
                    continue
                # The last one stamped at the newest time: two sessions made
                # inside one second still order by when they were made.
                if newest is None or float(entry.get("at") or 0) >= float(newest.get("at") or 0):
                    newest = entry
            return dict(newest) if newest else None

    def session_on(self, channel_id: str, device: str) -> dict | None:
        """This channel's session on that device, when it has one there."""
        with self.lock:
            line = self.data["sessions"].get(channel_id) or {}
            if not isinstance(line, dict):
                return None
            entry = line.get(device)
            return dict(entry) if isinstance(entry, dict) else None

    def remember_session(self, channel_id: str, device: str, session_id: str, title: str) -> None:
        with self.lock:
            line = self.data["sessions"].get(channel_id)
            if not isinstance(line, dict):
                line = self.data["sessions"][channel_id] = {}
            line[device] = {
                "device": device,
                "session": session_id,
                "title": title,
                "at": int(time.time()),
            }
            self._write()

    def forget_session(self, channel_id: str, device: str | None = None) -> None:
        """Forget one device's session for the channel, or every one of them."""
        with self.lock:
            if device is None:
                self.data["sessions"].pop(channel_id, None)
            else:
                line = self.data["sessions"].get(channel_id)
                if isinstance(line, dict):
                    line.pop(device, None)
                    if not line:
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

    # -- questions the agent asks ----------------------------------------

    def new_form(self, ticket: str, device: str, session: str, form: str,
                 fields: list[dict], channel: str) -> None:
        with self.lock:
            self.data["forms"][ticket] = {
                "device": device,
                "session": session,
                "form": form,
                "fields": fields,
                "answers": {},
                "messages": [],
                "channel": channel,
                "at": int(time.time()),
            }
            self._write()

    def form(self, ticket: str) -> dict | None:
        with self.lock:
            return self.data["forms"].get(ticket)

    def form_messages(self, ticket: str, messages: list[dict]) -> None:
        with self.lock:
            entry = self.data["forms"].get(ticket)
            if entry is not None:
                entry["messages"] = messages
                self._write()

    def answer_form(self, ticket: str, key: str, value) -> dict | None:
        with self.lock:
            entry = self.data["forms"].get(ticket)
            if entry is None:
                return None
            entry["answers"][key] = value
            self._write()
            return entry

    def close_form(self, ticket: str) -> None:
        with self.lock:
            self.data["forms"].pop(ticket, None)
            self._write()

    def prune_forms(self, age: int = 86400) -> None:
        cutoff = time.time() - age
        with self.lock:
            stale = [
                key for key, value in self.data["forms"].items()
                if value.get("at", 0) < cutoff
            ]
            if stale:
                for key in stale:
                    self.data["forms"].pop(key, None)
                self._write()

    # -- the allowlist's message in Discord -------------------------------

    def state_message(self, channel_id: str) -> str | None:
        with self.lock:
            return self.data["channels"].get(channel_id)

    def remember_state_message(self, channel_id: str, message_id: str) -> None:
        with self.lock:
            self.data["channels"][channel_id] = message_id
            self._write()
