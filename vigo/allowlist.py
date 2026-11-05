"""Who may ask: the owner, the people the file lists, and those the owner allows.

The allowlist has to be the same on every device, so it lives in Discord, not
on a disk: a message the bot owns, in the state channel when the file names
one, otherwise in the bot's DM with its owner.  A device reads it before every
decision, so an allow made from the Mac holds on the PC at once.

Only the daemon decides this.  Nothing a person writes - in a prompt, a
reaction or an answer - can change it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from . import log
from .config import Config
from .discord_api import Discord, DiscordError
from .state import State

MARKER = "VIGO-ALLOWLIST 1"
MOST = 60  # allows at once; past this the message would not fit


class AllowError(Exception):
    """The stored list cannot be read, so nothing may change it."""


@dataclass
class Entry:
    id: str
    name: str
    since: int
    until: int | None

    @property
    def expired(self) -> bool:
        return self.until is not None and self.until <= time.time()

    def as_json(self) -> dict:
        return {"id": self.id, "name": self.name, "since": self.since, "until": self.until}

    def until_text(self) -> str:
        if self.until is None:
            return "for good"
        left = int(self.until - time.time())
        if left <= 0:
            return "ended"
        return f"<t:{self.until}:R>"


class AllowList:
    def __init__(self, config: Config, api: Discord, state: State):
        self.config = config
        self.api = api
        self.state = state
        self.owner = config.owner
        self.listed = set(config.users)
        self._channel: str | None = None
        self._message: str | None = None
        self.where = "not ready"

    # -- where the list lives ---------------------------------------------

    def home(self) -> str:
        """The channel both shared messages live in."""
        return self._home()

    def _home(self) -> str:
        if self._channel:
            return self._channel
        if self.config.state_channel:
            self._channel = self.config.state_channel
            self.where = f"<#{self._channel}>"
            return self._channel
        try:
            channel = self.api.open_dm(self.owner)
        except DiscordError as error:
            raise AllowError(f"cannot open a DM with you ({error.message})") from error
        self._channel = channel
        self.where = "the bot's DM with you"
        return channel

    def _find(self, channel: str) -> str | None:
        remembered = self.state.state_message(channel)
        if remembered:
            reply = self.api._call("GET", f"/channels/{channel}/messages/{remembered}")
            if reply.ok:
                return remembered
            if not isinstance(reply.body, dict) or reply.body.get("code") != 10008:
                raise AllowError(
                    f"cannot read the allowlist in {self.where} ({reply.status}); "
                    "set 'stateChannel' to a private channel the bot can read"
                )
            self.state.remember_state_message(channel, "")
        try:
            found = self.api.messages(channel, limit=50)
        except DiscordError as error:
            raise AllowError(
                f"cannot read {self.where} ({error.message}); "
                "set 'stateChannel' to a private channel the bot can read"
            ) from error
        for message in found:
            content = message.get("content") or ""
            if content.startswith(MARKER):
                self.state.remember_state_message(channel, message["id"])
                return message["id"]
        return None

    # -- reading ----------------------------------------------------------

    def entries(self) -> list[Entry]:
        channel = self._home()
        message_id = self._find(channel)
        if not message_id:
            return []
        try:
            reply = self.api._call("GET", f"/channels/{channel}/messages/{message_id}")
        except DiscordError:
            return []
        if not reply.ok or not isinstance(reply.body, dict):
            self.state.remember_state_message(channel, "")
            return []
        content = reply.body.get("content") or ""
        if not content.startswith(MARKER):
            raise AllowError(f"the message in {self.where} is not the allowlist any more")
        raw = content[len(MARKER) :].strip()
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise AllowError(f"the allowlist in {self.where} is not JSON ({error.msg})") from error
        if not isinstance(data, dict) or not isinstance(data.get("allowed", []), list):
            raise AllowError(f"the allowlist in {self.where} has no 'allowed' list")
        if data.get("v", 1) != 1:
            raise AllowError(
                f"the allowlist in {self.where} was written by a newer vigo "
                f"(schema v{data.get('v')}); upgrade this one before changing it"
            )
        fresh: list[Entry] = []
        for item in data.get("allowed", []):
            if not isinstance(item, dict):
                raise AllowError(f"the allowlist in {self.where} holds an entry that is not an object")
            try:
                entry = Entry(
                    id=str(item["id"]),
                    name=str(item.get("name", "")),
                    since=int(item["since"]),
                    until=None if item.get("until") is None else int(item["until"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise AllowError(f"the allowlist in {self.where} has a broken entry ({error})") from error
            if not entry.expired:
                fresh.append(entry)
        return fresh

    def _store(self, entries: list[Entry]) -> None:
        channel = self._home()
        message_id = self._find(channel)
        body = {"v": 1, "allowed": [entry.as_json() for entry in entries]}
        content = f"{MARKER}\n{json.dumps(body, separators=(',', ':'))}"
        if len(content) > 1950:
            raise AllowError(f"{MOST} allows at most; take some back with /vigo deny")
        if message_id:
            reply = self.api._call("PATCH", f"/channels/{channel}/messages/{message_id}",
                                   {"content": content, "allowed_mentions": {"parse": []}})
        else:
            reply = self.api._call("POST", f"/channels/{channel}/messages",
                                   {"content": content, "allowed_mentions": {"parse": []}})
        if not reply.ok or not isinstance(reply.body, dict):
            raise AllowError(f"Discord would not take the allowlist in {self.where} ({reply.status})")
        self.state.remember_state_message(channel, reply.body["id"])

    # -- deciding ---------------------------------------------------------

    def may_ask(self, user_id: str) -> bool:
        if user_id == self.owner or user_id in self.listed:
            return True
        try:
            return any(entry.id == user_id for entry in self.entries())
        except Exception as error:  # a broken list refuses; it never allows by accident
            log.error(f"allowlist: {error}")
            return False

    def label(self, user_id: str) -> str:
        if user_id == self.owner:
            return "owner"
        if user_id in self.listed:
            return "listed"
        try:
            for entry in self.entries():
                if entry.id == user_id:
                    return f"allowed until {entry.until_text()}" if entry.until else "allowed for good"
        except AllowError:
            pass
        return "stranger"

    # -- changing ---------------------------------------------------------

    def allow(self, user_id: str, name: str, seconds: int | None) -> str:
        if user_id == self.owner:
            raise AllowError("that is you, who needs no allow")
        if user_id in self.listed:
            raise AllowError("that person is already in the file's list")
        entries = self.entries()
        replaced = None
        for index, entry in enumerate(entries):
            if entry.id == user_id:
                replaced = entry
                entries.pop(index)
                break
        until = None if seconds is None else int(time.time()) + seconds
        entries.append(Entry(id=user_id, name=name[:80], since=int(time.time()), until=until))
        if len(entries) > MOST:
            raise AllowError(f"{MOST} allows at once; deny one first")
        self._store(entries)
        fresh = Entry(id=user_id, name=name[:80], since=int(time.time()), until=until)
        if replaced:
            return f"**{metadata_name(name)}** is allowed {fresh.until_text()} (the old allow ran {replaced.until_text()})"
        return f"**{metadata_name(name)}** is allowed {fresh.until_text()}"

    def deny(self, user_id: str) -> tuple[bool, str]:
        if user_id in self.listed:
            return False, "that person is in the file's list; the file is what takes them out"
        entries = self.entries()
        for index, entry in enumerate(entries):
            if entry.id == user_id:
                entries.pop(index)
                self._store(entries)
                return True, f"**{metadata_name(entry.name)}** is no longer allowed"
        return False, "they have no allow in force"

    def describe(self) -> str:
        try:
            entries = self.entries()
        except AllowError as error:
            return f"⚠️ {error}"
        lines = [f"The allowlist is in {self.where}."]
        if entries:
            for entry in sorted(entries, key=lambda item: item.until or 0):
                lines.append(f"• **{metadata_name(entry.name)}** (`{entry.id}`) — {entry.until_text()}")
        else:
            lines.append("• nobody is allowed for a time.")
        if self.config.users:
            lines.append("In the file's list: " + ", ".join(f"`{user}`" for user in self.config.users))
        else:
            lines.append("The file lists no one.")
        return "\n".join(lines)


def metadata_name(name: str) -> str:
    safe = name.replace("*", "").replace("_", " ").replace("`", "").replace("@", "@\u200b")
    return safe.strip() or "they"
