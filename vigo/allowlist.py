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
    def __init__(self, config: Config, api: Discord, state: State) -> None:
        self.config = config
        self.api = api
        self.state = state
        self.owner = config.owner
        self.listed = set(config.users)
        self._channel: str | None = None
        self._bot: str | None = None  # this bot's own user id, asked once
        self._saw_own = False  # whether the scan met a message this bot wrote
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

    def _mine(self) -> str:
        """This bot's own user id, which is the application's.

        The list is a message, so it is only as trustworthy as the person who
        wrote it: in a channel where somebody else can post, a message that
        opens with the marker would otherwise grant whoever wrote it what it
        asks for.  The id is asked once, because it never changes, and a
        Discord that will not say it stops the read rather than guessing.
        """
        if self._bot is None:
            try:
                self._bot = str(self.api.me().get("id") or "")
            except DiscordError as error:
                raise AllowError(
                    f"cannot ask Discord which user this bot is ({error.message})"
                ) from error
            if not self._bot:
                raise AllowError("Discord did not name this bot's user id")
        return self._bot

    def _own(self, message: dict) -> bool:
        """Whether this bot posted the message, so its content may be read."""
        author = message.get("author")
        who = str((author or {}).get("id") or "") if isinstance(author, dict) else ""
        if who and who == self._mine():
            self._saw_own = True
            return True
        if (message.get("content") or "").startswith(MARKER):
            log.debug(f"allowlist: a list-shaped message from {who or 'an unknown author'} is ignored")
        return False

    def _own_message(self, channel: str) -> dict | None:
        """The bot's own list message, read once.

        The id this daemon remembered is tried first: one GET answers the
        whole decision.  A message that is gone, or one that is not the bot's,
        sends the search back to the channel's newest messages.
        """
        remembered = self.state.state_message(channel)
        self._saw_own = False
        if remembered:
            reply = self.api._call("GET", f"/channels/{channel}/messages/{remembered}")
            if reply.ok and isinstance(reply.body, dict):
                if self._own(reply.body):
                    return reply.body
                # Somebody else's message under that id: forget it, and find
                # the bot's own rather than reading or overwriting this one.
                log.error("allowlist: the message it remembered is not this bot's; "
                          "reading the list again from the channel")
                self.state.remember_state_message(channel, "")
            elif not isinstance(reply.body, dict) or reply.body.get("code") != 10008:
                raise AllowError(
                    f"cannot read the allowlist in {self.where} ({reply.status}); "
                    "set 'stateChannel' to a private channel the bot can read"
                )
            else:
                self.state.remember_state_message(channel, "")
        try:
            found = self.api.messages(channel, limit=100)
        except DiscordError as error:
            raise AllowError(
                f"cannot read {self.where} ({error.message}); "
                "set 'stateChannel' to a private channel the bot can read"
            ) from error
        before = ""
        # A busy channel can push the list out of the newest page.  Walk back
        # a bounded way rather than call the list missing: treating a flood as
        # no list would let the next allow write a new one and drop the rest.
        for _page in range(10):
            for message in found:
                content = message.get("content") or ""
                mine = self._own(message)
                self._saw_own = self._saw_own or mine
                if content.startswith(MARKER) and mine:
                    self.state.remember_state_message(channel, message["id"])
                    return message
            if not found:
                break
            before = str(found[-1].get("id") or "")
            if not before or len(found) < 100:
                break
            try:
                found = self.api.messages(channel, limit=100, before=before)
            except DiscordError as error:
                raise AllowError(
                    f"cannot read {self.where} ({error.message}); "
                    "set 'stateChannel' to a private channel the bot can read"
                ) from error
        return None

    # -- reading ----------------------------------------------------------

    def entries(self) -> list[Entry]:
        channel = self._home()
        message = self._own_message(channel)
        if message is None:
            return []
        content = message.get("content") or ""
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
        found = self._own_message(channel)
        message_id = str((found or {}).get("id") or "")
        if not message_id and self._saw_own:
            # This bot wrote in the channel, but no list was in reach.  A new
            # message would become the list and the old entries would be lost,
            # so nothing changes until the old one can be read.
            raise AllowError(
                f"the allowlist in {self.where} cannot be found in the newest "
                "messages; nothing was changed. Set 'stateChannel' to a private "
                "channel that holds the list"
            )
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
            return f"{error}"
        lines = [f"The allowlist is in {self.where}."]
        if entries:
            for entry in sorted(entries, key=lambda item: item.until or 0):
                lines.append(f"• **{metadata_name(entry.name)}** (`{entry.id}`), {entry.until_text()}")
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
