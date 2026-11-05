"""Discord's REST API v10, as much of it as the bot uses."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import httpc, log

API = "https://discord.com/api/v10"


class DiscordError(Exception):
    """Discord answered with an error the bot cannot retry."""

    def __init__(self, status: int, code: int, message: str, where: str):
        super().__init__(
            f"{where}: Discord said {status} {message}" + (f" ({code})" if code else "")
        )
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Reply:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Discord:
    def __init__(self, token: str, app_id: str, base: str = API, timeout: float = 20.0):
        self.token = token
        self.app_id = app_id
        self.base = base.rstrip("/")
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _error(reply: Reply, fallback: str, where: str) -> DiscordError:
        code = 0
        message = fallback
        if isinstance(reply.body, dict):
            message = str(reply.body.get("message") or fallback)
            try:
                code = int(reply.body.get("code") or 0)
            except (TypeError, ValueError):
                code = 0
        return DiscordError(reply.status, code, message, where)

    def _call(self, method: str, path: str, payload: Any = None, *, retries: int = 3) -> Reply:
        url = path if path.startswith("http") else f"{self.base}{path}"
        headers = {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "vigo (https://github.com/thegreatGreenstar/vigo, 1.0)",
        }
        body = None
        if payload is not None:
            body = httpc.json_body(payload)
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            attempt += 1
            try:
                status, answer_headers, raw = httpc.call(
                    url, method, headers=headers, body=body, timeout=self.timeout
                )
            except httpc.HttpError as error:
                raise DiscordError(0, 0, str(error), f"{method} {path}") from error

            if status == 429:
                wait = 1.0
                parsed = httpc.parse_json(raw)
                if isinstance(parsed, dict) and isinstance(parsed.get("retry_after"), (int, float)):
                    wait = float(parsed["retry_after"])
                else:
                    try:
                        wait = float(answer_headers.get("Retry-After", "1"))
                    except ValueError:
                        wait = 1.0
                if attempt <= retries:
                    log.debug(f"{method} {path}: 429, waiting {wait:.2f}s")
                    time.sleep(min(wait, 30.0) + 0.05)
                    continue
                raise DiscordError(429, 0, "rate limited", f"{method} {path}")

            if 500 <= status < 600 and attempt <= 2:
                time.sleep(0.5 * attempt)
                continue

            return Reply(status, httpc.parse_json(raw))

    # -- the bot's own user, and the gateway ------------------------------

    def me(self) -> dict:
        """The bot's own user, which also proves the token."""
        reply = self._call("GET", "/users/@me")
        if not reply.ok or not isinstance(reply.body, dict):
            raise self._error(reply, "the token is not the bot's", "GET /users/@me")
        return reply.body

    def gateway_url(self) -> str:
        reply = self._call("GET", "/gateway/bot")
        if not reply.ok or not isinstance(reply.body, dict):
            raise self._error(reply, "no gateway url", "GET /gateway/bot")
        return reply.body["url"]

    # -- slash commands ---------------------------------------------------

    def guild_commands(self, guild_id: str) -> list[dict]:
        reply = self._call("GET", f"/applications/{self.app_id}/guilds/{guild_id}/commands")
        if not reply.ok:
            raise self._error(reply, "cannot read commands", f"guild {guild_id}")
        return reply.body if isinstance(reply.body, list) else []

    def put_guild_commands(self, guild_id: str, commands: list[dict]) -> list[dict]:
        reply = self._call(
            "PUT", f"/applications/{self.app_id}/guilds/{guild_id}/commands", commands
        )
        if not reply.ok:
            raise self._error(reply, "cannot register commands", f"guild {guild_id}")
        return reply.body if isinstance(reply.body, list) else []

    def global_commands(self) -> list[dict]:
        reply = self._call("GET", f"/applications/{self.app_id}/commands")
        if not reply.ok:
            raise self._error(reply, "cannot read commands", "global commands")
        return reply.body if isinstance(reply.body, list) else []

    def put_global_commands(self, commands: list[dict]) -> list[dict]:
        reply = self._call("PUT", f"/applications/{self.app_id}/commands", commands)
        if not reply.ok:
            raise self._error(reply, "cannot register commands", "global commands")
        return reply.body if isinstance(reply.body, list) else []

    # -- interactions -----------------------------------------------------

    def respond(self, interaction_id: str, token: str, payload: dict) -> Reply:
        """Answer an interaction.

        A 400 with code 40060 means another daemon answered it first; the
        caller compares `status` and stops.
        """
        return self._call("POST", f"/interactions/{interaction_id}/{token}/callback", payload)

    def edit_original(self, token: str, payload: dict) -> dict:
        reply = self._call("PATCH", f"/webhooks/{self.app_id}/{token}/messages/@original", payload)
        if not reply.ok:
            raise self._error(reply, "cannot edit the answer", "interaction response")
        return reply.body if isinstance(reply.body, dict) else {}

    def followup(self, token: str, payload: dict) -> dict:
        """A message after the answer has been sent.

        Discord treats the first one sent right after a deferral as an edit of
        the deferred message, so callers only use this once the answer itself
        is in that message.
        """
        reply = self._call("POST", f"/webhooks/{self.app_id}/{token}", payload)
        if not reply.ok:
            raise self._error(reply, "cannot post a followup", "interaction response")
        return reply.body if isinstance(reply.body, dict) else {}

    # -- messages ---------------------------------------------------------

    def open_dm(self, user_id: str) -> str:
        reply = self._call("POST", "/users/@me/channels", {"recipient_id": user_id})
        if not reply.ok or not isinstance(reply.body, dict) or "id" not in reply.body:
            raise self._error(reply, "cannot open a DM", f"DM {user_id}")
        return reply.body["id"]

    def send(self, channel_id: str, payload: dict) -> dict:
        reply = self._call("POST", f"/channels/{channel_id}/messages", payload)
        if not reply.ok or not isinstance(reply.body, dict):
            raise self._error(reply, "cannot post", f"channel {channel_id}")
        return reply.body

    def edit(self, channel_id: str, message_id: str, payload: dict) -> dict:
        reply = self._call("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload)
        if not reply.ok or not isinstance(reply.body, dict):
            raise self._error(reply, "cannot edit", f"message {message_id}")
        return reply.body

    def messages(self, channel_id: str, limit: int = 50) -> list[dict]:
        reply = self._call("GET", f"/channels/{channel_id}/messages?limit={int(limit)}")
        if not reply.ok or not isinstance(reply.body, list):
            raise self._error(reply, "cannot read", f"channel {channel_id}")
        return reply.body

    def channel(self, channel_id: str) -> dict:
        reply = self._call("GET", f"/channels/{channel_id}")
        if not reply.ok or not isinstance(reply.body, dict):
            raise self._error(reply, "cannot read", f"channel {channel_id}")
        return reply.body
