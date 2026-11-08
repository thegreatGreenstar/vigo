"""What this device remembers between runs.

Ids and times, and the title it gave a session: which Discord channel is
talking to which OpenCode session, which permission card is which request, and
the turn an upgrade's restart cut off.  The file is per device, in
`~/.local/state/vigo/state.json`, folder 700 and file 600.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import log

ANSWER_AGE = 7 * 86400  # seconds a message stays a way back into its conversation
SUSPEND_AGE = 3600.0   # seconds carried-over work may wait for its restart
REQUEST_COOLDOWN = 3600.0  # seconds before one person may ask the owner again
REQUEST_PENDING = 86400.0  # seconds a request waits before the person may ask again
REQUEST_KEEP = 30 * 86400  # seconds a settled request is remembered, for the cooldown


def default_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "vigo"


class State:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or (default_dir() / "state.json"))
        self.lock = threading.Lock()
        self.data: dict = {"sessions": {}, "tickets": {}, "forms": {}, "answers": {},
                           "channels": {}, "suspended": {}, "requests": {}}
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
                self.data.setdefault("answers", {})
                self.data.setdefault("channels", {})
                self.data.setdefault("suspended", {})
                self.data.setdefault("requests", {})
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

    # -- the work an upgrade interrupts ------------------------------------

    def suspend_runs(self, carried: dict[str, dict]) -> None:
        """Write down the work a restart for an upgrade will cut off.

        Only an upgrade writes here.  A run that a crash or a stop ends is
        over, and an answer posted hours later is not the one its channel
        waits for, so nothing else may leave a record behind.
        """
        with self.lock:
            self.data["suspended"] = carried
            self._write()

    def take_suspended(self, age: float = SUSPEND_AGE) -> dict[str, dict]:
        """The carried-over work, once, for the process the restart started.

        It is given up as it is read: the new process resumes it, and a run
        that it loses again is lost, as any other crash loses one.  A record
        older than `age` is dropped, because the restart it waited for did not
        happen and the turn it names is over.
        """
        with self.lock:
            held = self.data.get("suspended")
            self.data["suspended"] = {}
            if not isinstance(held, dict):
                held = {}
            cutoff = time.time() - age
            kept: dict[str, dict] = {}
            stale = 0
            for channel, entry in held.items():
                at = entry.get("at") if isinstance(entry, dict) else None
                if not isinstance(at, (int, float)) or at < cutoff:
                    stale += 1
                    continue
                kept[channel] = entry
            if held:
                self._write()
        if stale:
            log.info(f"state: {stale} carried-over turn(s) waited past their restart")
        return kept

    # -- permission cards -------------------------------------------------

    def new_ticket(self, ticket: str, device: str, session: str, request: str,
                   channel: str, owner: bool = True) -> None:
        with self.lock:
            self.data["tickets"][ticket] = {
                "device": device,
                "session": session,
                "request": request,
                "channel": channel,
                "owner": owner,
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
                 fields: list[dict], channel: str, guest: bool = False) -> None:
        with self.lock:
            self.data["forms"][ticket] = {
                "device": device,
                "session": session,
                "form": form,
                "fields": fields,
                "answers": {},
                "messages": [],
                "channel": channel,
                "guest": guest,
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

    def answer_form(self, ticket: str, key: str, value: object) -> dict | None:
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

    # -- people asking the owner for access -------------------------------

    def request_state(self, user_id: str, now: float | None = None) -> str:
        """Whether that person has a request waiting, is cooling down, or may ask.

        A request is one DM to the owner.  The record also survives the owner's
        answer, so a denial buys an hour of quiet rather than a button that can
        be pressed again at once.  A request that waits a day without an answer
        stops blocking the person, who may ask again.
        """
        with self.lock:
            entry = self.data["requests"].get(user_id)
            if not isinstance(entry, dict):
                return ""
            stamp = float(entry.get("at") or 0)
            now = time.time() if now is None else now
            if entry.get("pending"):
                return "pending" if now - stamp < REQUEST_PENDING else ""
            if now - stamp < REQUEST_COOLDOWN:
                return "cooling"
            return ""

    def remember_request(self, user_id: str, name: str) -> None:
        with self.lock:
            now = int(time.time())
            self.data["requests"][user_id] = {
                "name": name[:80], "at": now, "pending": True,
            }
            cutoff = now - REQUEST_KEEP
            stale = [key for key, entry in self.data["requests"].items()
                     if isinstance(entry, dict) and float(entry.get("at") or 0) < cutoff]
            for key in stale:
                self.data["requests"].pop(key, None)
            self._write()

    def request(self, user_id: str) -> dict | None:
        with self.lock:
            entry = self.data["requests"].get(user_id)
            return dict(entry) if isinstance(entry, dict) else None

    def pending_requests(self, now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        with self.lock:
            return sum(1 for entry in self.data["requests"].values()
                       if isinstance(entry, dict) and entry.get("pending")
                       and stamp - float(entry.get("at") or 0) < REQUEST_PENDING)

    def settle_request(self, user_id: str, allowed: bool) -> None:
        """Close a request: an allow leaves no record, a denial starts the wait."""
        with self.lock:
            if allowed:
                self.data["requests"].pop(user_id, None)
            else:
                entry = self.data["requests"].setdefault(user_id, {})
                entry["pending"] = False
                entry["at"] = int(time.time())
            self._write()

    def forget_request(self, user_id: str) -> None:
        """Drop a request that never reached the owner, so it can be sent again."""
        with self.lock:
            self.data["requests"].pop(user_id, None)
            self._write()

    # -- the messages this daemon posted --------------------------------

    def remember_answer(self, message_id: str, channel: str) -> None:
        """Remember one message this daemon posted, so a reply to it is a question.

        It is also how the daemons elect one answerer: only the daemon that
        posted the message has it, so only that daemon acts on a reply.
        """
        with self.lock:
            self.data["answers"][message_id] = {
                "channel": channel,
                "at": int(time.time()),
            }
            cutoff = time.time() - ANSWER_AGE
            stale = [key for key, entry in self.data["answers"].items()
                     if float(entry.get("at") or 0) < cutoff]
            for key in stale:
                self.data["answers"].pop(key, None)
            self._write()

    def answer_for(self, message_id: str) -> dict | None:
        """The message, if this daemon posted it, and the channel it is in."""
        with self.lock:
            entry = self.data["answers"].get(message_id)
            return dict(entry) if isinstance(entry, dict) else None

    # -- the allowlist's message in Discord -------------------------------

    def state_message(self, channel_id: str) -> str | None:
        with self.lock:
            return self.data["channels"].get(channel_id)

    def remember_state_message(self, channel_id: str, message_id: str) -> None:
        with self.lock:
            self.data["channels"][channel_id] = message_id
            self._write()
