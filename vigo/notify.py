"""Saying something to a person in a DM, wherever the saying comes from.

The daemon DMs the owner when it pulls an update, and `vigo dm "…"` lets a
session on this machine do the same — an OpenCode session that finished a job,
say.  An answer that misses its interaction is DMed to the person who asked.
One code path, one place where a DM is opened and split, and one token: the
settings file's, read the way every other call reads it.

Nothing here decides *whether* to speak; the caller does.  A failure is raised,
never swallowed: a DM that did not arrive must not look like one that did.
"""
from __future__ import annotations

from . import text
from .config import Config
from .discord_api import Discord

# Discord's own limit is 2000; `text.chunk` splits an answer the way it splits
# a long one, so a long status report arrives as several messages, not one
# refused.
def send_dm(config: Config, user_id: str, content: str,
            api: Discord | None = None) -> int:
    """DM one person; return how many messages it took.

    Raises DiscordError when Discord will not take it, and whatever the HTTP
    layer raises when nothing answers — the caller says so and exits non-zero.
    """
    body = (content or "").strip()
    if not body:
        raise ValueError("nothing to say")
    client = api or Discord(config.token, config.app_id)
    channel = client.open_dm(user_id)
    pieces = text.chunk(body) or [body]
    for piece in pieces:
        client.send(channel, {
            "content": piece,
            "allowed_mentions": {"parse": []},
        })
    return len(pieces)


def send_owner(config: Config, content: str, api: Discord | None = None) -> int:
    """DM the owner, the one person a device always speaks to."""
    return send_dm(config, config.owner, content, api=api)
