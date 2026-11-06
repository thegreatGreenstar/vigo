"""Turning an agent's answer into Discord messages."""

from __future__ import annotations

import re

LIMIT = 1900  # 2000 is Discord's; the margin keeps a fence's closing line safe.

# What is a secret wherever it appears: a key, a token, a private key block.
# Only shapes that cannot be a normal word are here, so an answer about code is
# not mangled.  A path, an e-mail or a hostname is left alone: those are what an
# answer about a machine has to say, and the person asking may see them.
SECRETS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("private key",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[redacted: private key]"),
    ("API key",
     re.compile(r"\b(?:sk|rk)-(?:ant-|proj-|live-)?[A-Za-z0-9_-]{20,}"),
     "[redacted: API key]"),
    ("GitHub token",
     re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
     "[redacted: GitHub token]"),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted: AWS key]"),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[redacted: Slack token]"),
    ("secret",
     re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*[:=]\s*[\"']?[A-Za-z0-9_+/\-]{16,}"),
     r"\1 [redacted: secret]"),
)


def scrub(body: str) -> tuple[str, str]:
    """The text with secret-shaped values replaced, and the first kind removed.

    Only an answer other people will read goes through this.  An answer the
    asker alone sees keeps its values, because the person who asked is the
    person who may see them.
    """
    found = ""
    for name, pattern, instead in SECRETS:
        body, count = pattern.subn(instead, body)
        if count:
            found = found or name
    return body, found


# What a task may not hold to appear in a channel: a path into somebody's
# files, an address that names a person or a machine, an id, a secret.  The
# public queue keeps the tasks that hold none of these and says `redacted task`
# for the rest, so the list errs toward hiding rather than toward saying a name.
PERSONAL: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[\s(\"'`])~[/\\]"),  # a home folder
    re.compile(r"[A-Za-z]:\\"),  # a Windows path
    re.compile(r"(?:^|[\s(\"'`])/(?:Users|home|root|Volumes|mnt|private|tmp)/"),  # a path of its own
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # an e-mail
    re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.(?:local|lan|home|internal|tailnet)\b", re.I),  # a hostname here
    re.compile(r"\bhttps?://", re.I),  # a link, and the host it names
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),  # an address
    re.compile(r"<[@#][!&]?\d+>"),  # a mention
    re.compile(r"\b\d{17,20}\b"),  # a Discord id
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),  # a UUID
    re.compile(r"\+?\d[\d ().-]{7,}\d"),  # a phone number
)


def shareable(said: str, names: tuple[str, ...] = ()) -> bool:
    """Whether a task's own words can be read in a channel.

    A task is the person's text, and that text can hold a path, an address, an
    id or a name.  `names` are the device names of the settings, which no
    channel message says either.  A task with none of these is shown, and one
    with any of them is shown as `redacted task`, because a redacted line keeps
    its place in the queue while its text would name somebody.
    """
    if any(pattern.search(said) for pattern in PERSONAL):
        return False
    if scrub(said)[0] != said:
        return False
    return not any(re.search(rf"\b{re.escape(name)}\b", said, re.I) for name in names if name)


def subject(said: str, size: int = 80) -> str:
    """The first line of what was asked, short enough to name the work.

    A prompt can be pages long; the acknowledgement needs the thing it starts
    with, and the person's own words are more honest than a summary the bot
    would have to guess at.
    """
    first = clean(said).strip().split("\n", 1)[0].strip()
    return shorten(first.rstrip(".:;,!?"), size)


# What a device's failure means to the person waiting.  The device's own text
# names it, and is never shown: it can quote the question, a path, a key.  The
# classes are the ones a person can act on, and each is tried in turn.
CAUSES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("rate limit", "rate-limit", "ratelimit", "too many requests", "429", "slow down"),
     "the model is rate limited; ask again in a minute"),
    (("quota", "credit", "usage limit", "insufficient_quota", "billing", "payment", "subscription"),
     "the account has no quota left; the plan says when it comes back"),
    (("context length", "context window", "context limit", "maximum context",
      "too many tokens", "token limit"),
     "the session is out of context; `new:true` starts a fresh one"),
    (("unauthorized", "invalid api key", "invalid_api_key", "authentication", "401"),
     "the provider refused the key; check that device's OpenCode settings"),
)


def cause(said: str) -> str:
    """What a device's failure means for the person waiting, in one line."""
    lowered = (said or "").lower()
    for hints, line in CAUSES:
        if any(hint in lowered for hint in hints):
            return line
    return "the agent failed; the log has the device's own words"


def clean(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def chunk(text: str, limit: int = LIMIT) -> list[str]:
    """Split an answer into messages, keeping each code fence closed.

    A split inside a ``` block closes it at the end of one message and opens it
    again in the next, so each message renders on its own.
    """
    text = clean(text)
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    size = 0
    fence = ""  # the opening line of the fence we are inside, "" outside one

    def flush() -> None:
        nonlocal current, size
        body = "\n".join(current).strip("\n")
        if fence:
            body = f"{body}\n```"
        if body.strip():
            parts.append(body)
        current = [fence] if fence else []
        size = len(fence) + 1 if fence else 0

    for line in text.split("\n"):
        stripped = line.strip()
        opening = stripped.startswith("```")

        if size + len(line) + 1 > limit and current:
            flush()

        room = limit - size - 1
        while len(line) > room > 0:
            current.append(line[:room])
            line = line[room:]
            flush()
            room = limit - size - 1
        current.append(line)
        size += len(line) + 1

        if opening:
            fence = "" if fence else stripped

    if current:
        parts.append("\n".join(current).strip("\n"))
    return [part for part in parts if part.strip()]


def messages(body: str, most: int = 4, limit: int = LIMIT,
             more: str = "… (the answer goes on)") -> list[str]:
    """An answer as the messages that carry it, whole unless it runs away.

    Discord takes 2000 characters in a message and no more.  An answer used to
    be cut there with nothing saying so, and it read as the agent stopping in
    the middle of a sentence.  So the rest follows in messages of its own.  A
    runaway answer stops at `most`, and its last message says there is more, so
    no reader is left at the end of a sentence that simply stopped.

    `more` is short on purpose: the last message is at `limit` already, and the
    line must fit Discord's 2000 with it.
    """
    parts = chunk(body, limit)
    if not parts or len(parts) <= most:
        return parts
    kept = parts[:most]
    kept[-1] = f"{kept[-1]}\n\n{more}"
    return kept


def _cut(body: str, size: int) -> str:
    """Cut at the last space or line break in `size`, so a word is not split."""
    if size <= 0:
        return ""
    cut = body[:size]
    at = max(cut.rfind("\n"), cut.rfind(" "))
    if at >= size // 2:
        return cut[:at]
    return cut


def shorten(text: str, size: int) -> str:
    text = clean(text)
    if len(text) <= size:
        return text
    return text[: size - 1] + "…"


def tail(text: str, size: int) -> str:
    text = clean(text)
    if len(text) <= size:
        return text
    return "…" + text[-(size - 1) :]


def duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def money(amount: float) -> str:
    if not amount:
        return "$0"
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def fence(text: str, language: str = "") -> str:
    body = clean(text)
    if "```" in body:
        return body
    return f"```{language}\n{body}\n```"
