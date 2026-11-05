"""Turning an agent's answer into Discord messages."""

from __future__ import annotations

LIMIT = 1900  # 2000 is Discord's; the margin keeps a fence's closing line safe.


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
