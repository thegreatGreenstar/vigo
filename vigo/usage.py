"""What approved people spend of the owner's plan, and the cap on it.

Two records make the cap computable, and both come from the devices:

- the plan's own meters (`/api/experimental/usage/limits`): the percent of each
  window that is spent, whether a window is limited, and when it resets.  This
  is what a rate limit actually measures, so the cap needs no guess at a
  limit's size in tokens;
- the tokens of each window (`/api/experimental/session/stats` for the whole
  device, and each guest session's own messages for its part).

A guest session is one marked `vigoGuest` with the id of the person who asked.
A session made before that mark is still found, because the channel key of a
guest carries the asker after a colon, and a subagent counts as its parent's
session: a delegated session has no mark of its own.

The rule: approved people together may not pass `share` of any of the plan's
meters (a third by default).  `percent * guest_tokens / all_tokens` is the
percent of that window which belongs to them, measured over the same window,
so a guest who is the only one using the plan stops at a third of it, and a
guest who arrives after the owner spent two thirds gets what is left of that
third.  The hourly and daily token caps stop one conversation's burst before
the plan's meters can show it.

The windows the meters name are the ones OpenCode Go keeps: a rolling window
of five hours, a week, and thirty days.  The tokens are read over each of those
windows, never over the month alone: a guest who used a week's allowance in one
day passes the weekly meter's third on that day, and a month's token ratio
would hide it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from . import log, text
from .config import Config, Guests
from .opencode_api import OpenCode, OpenCodeError

GUEST_KEY = "vigoGuest"
CHANNEL_KEY = "vigoChannel"
HOUR = 3600.0
DAY = 86400.0
# The window each meter of OpenCode Go measures.  The weekly meter resets at a
# Monday's midnight and the monthly one thirty days after the account started;
# the rolling one is the five-hour window it is named for.
PERIODS = {"rolling": 5 * HOUR, "weekly": 7 * DAY, "monthly": 30 * DAY}
LONGEST = max(PERIODS.values())


class UsageError(Exception):
    """The plan's usage cannot be read, so no cap can be checked."""


@dataclass(frozen=True)
class Spend:
    """What the plan and its guests spent in each window the cap reads."""

    percents: dict[str, float] = field(default_factory=dict)   # meter → percent
    limited: str = ""             # the meter that is already limited, if any
    reset: float = 0.0            # when that meter comes back, ms since the epoch
    all_windows: dict[str, int] = field(default_factory=dict)   # meter → tokens
    guest_windows: dict[str, int] = field(default_factory=dict)  # meter → tokens
    guest_hour: int = 0
    guest_day: int = 0
    users_hour: dict[str, int] = field(default_factory=dict)
    users_day: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """Whether an approved person may ask now, and why not when they may not."""

    allowed: bool
    reason: str = ""


def guest_of(info: dict) -> str:
    """The id of the person a session belongs to, or "" for the owner's.

    The marker is written when the session is made or adopted.  A session from
    before the marker is still a guest's when its channel key holds the asker
    after a colon: only a guest's key carries one.
    """
    metadata = info.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    who = metadata.get(GUEST_KEY)
    if isinstance(who, str) and who:
        return who
    channel = str(metadata.get(CHANNEL_KEY) or "")
    _channel, colon, asker = channel.partition(":")
    return asker if colon else ""


def guest_sessions(listed: list[dict]) -> dict[str, str]:
    """Each session that counts as a guest's, and whose it is.

    A subagent runs in a session of its own with `parentID` naming the session
    that delegated the work, so the walk goes up until it finds a mark.  A
    session with no marked ancestor is the owner's own work, and its tokens
    count on the owner's side of the share.
    """
    by_id = {str(info.get("id") or ""): info for info in listed if isinstance(info, dict)}
    found: dict[str, str] = {}
    for session, info in by_id.items():
        if not session:
            continue
        node = info
        seen: set[str] = set()
        while True:
            who = guest_of(node)
            if who:
                found[session] = who
                break
            parent = node.get("parentID")
            if not isinstance(parent, str) or not parent or parent in seen:
                break
            seen.add(parent)
            node = by_id.get(parent) or {}
    return found


def tokens_of(tokens: object) -> int:
    """The plan's tokens: what the model read, wrote and reasoned.

    Cache reads stay out.  They are an order of magnitude larger than the rest
    and the same context is read again on each step, so counting them would
    make one long conversation look like the whole plan.  The plan's own
    percent is the authority on what a window holds; this number only divides
    that percent between the people who spent it.
    """
    if not isinstance(tokens, dict):
        return 0
    total = 0
    for key in ("input", "output", "reasoning"):
        value = tokens.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total


def _meters(data: dict) -> dict[str, float]:
    """Each meter the plan reports, with the percent of its window spent."""
    go = data.get("go")
    if not isinstance(go, dict):
        raise UsageError("the plan reports no allowance (no `go` meters)")
    found: dict[str, float] = {}
    for name in PERIODS:
        meter = go.get(name)
        if not isinstance(meter, dict):
            continue
        value = meter.get("percent")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found[name] = max(0.0, min(100.0, float(value)))
        else:
            found[name] = 0.0
    if not found:
        raise UsageError("the plan reports no allowance (no `go` meters)")
    return found


def _limited(data: dict) -> tuple[str, float]:
    """The first meter the plan says is limited, and when it comes back."""
    go = data.get("go") if isinstance(data, dict) else None
    if not isinstance(go, dict):
        return "", 0.0
    for name in PERIODS:
        meter = go.get(name)
        if isinstance(meter, dict) and meter.get("limited"):
            when = meter.get("resetsAt")
            reset = float(when) if isinstance(when, (int, float)) and not isinstance(when, bool) else 0.0
            return name, reset
    return "", 0.0


def collect(config: Config, device_name: str = "", now: float | None = None) -> Spend:
    """Read the plan's meters and each window's tokens from the devices.

    The device that will answer the question must answer here too: a cap that
    cannot be checked is refused.  Another device that does not answer is left
    out, which makes the guest share look larger and the cap stricter, never
    looser: its own usage cannot be counted, and the percent is the plan's.
    """
    now = time.time() if now is None else now
    required = device_name or config.device
    device = config.devices.get(required)
    if device is None:
        raise UsageError(f"the device {required!r} is not in the settings")
    try:
        limits = OpenCode(device.url, device.password, timeout=8.0).limits()
    except OpenCodeError as error:
        raise UsageError(f"the plan's allowance cannot be read ({error.message})") from error
    percents = _meters(limits)
    limited, reset = _limited(limits)
    starts = {name: now - PERIODS[name] for name in percents}
    spend = Spend(percents=percents, limited=limited, reset=reset,
                  all_windows={name: 0 for name in percents},
                  guest_windows={name: 0 for name in percents})
    answered = False
    for name, each in config.devices.items():
        # The device that answers the question must answer here as well; a
        # peer that does not is left out after a short wait, because its usage
        # cannot be counted and the stricter answer is the safe one.
        client = OpenCode(each.url, each.password, timeout=8.0 if name == required else 2.0)
        try:
            listed = client.sessions(limit=100)
            every = {window: tokens_of(client.stats(start * 1000.0, now * 1000.0).get("tokens"))
                     for window, start in starts.items()}
        except OpenCodeError as error:
            if name == required:
                raise UsageError(f"the plan's usage cannot be read ({error.message})") from error
            log.info(f"usage: {name} cannot read its usage ({error.message})")
            continue
        answered = True
        spend = replace(spend, all_windows={
            window: spend.all_windows.get(window, 0) + total
            for window, total in every.items()
        })
        for session, who in guest_sessions(listed).items():
            try:
                messages = client.messages_since(session, now * 1000.0 - LONGEST * 1000.0)
            except OpenCodeError as error:
                if name == required:
                    raise UsageError(f"a guest session cannot be read ({error.message})") from error
                log.info(f"usage: {name} cannot read a guest session ({error.message})")
                continue
            for message in messages:
                if message.get("type") != "assistant":
                    continue
                used = tokens_of(message.get("tokens"))
                if not used:
                    continue
                created = float((message.get("time") or {}).get("created") or 0) / 1000.0
                spend = _add(spend, who, used, created, now, starts)
    if not answered:
        raise UsageError("no device answered about the plan's usage")
    for window, guest in spend.guest_windows.items():
        if guest and not spend.all_windows.get(window):
            # The device says approved people spent tokens, and its own record
            # says nothing was spent: the share would be a division by zero
            # dressed up as one, so it is refused rather than allowed.
            raise UsageError(f"the plan reports no total usage for the {window} window")
    return spend


def _add(spend: Spend, who: str, used: int, created: float, now: float,
         starts: dict[str, float]) -> Spend:
    """One assistant message's tokens, filed into the windows it belongs to."""
    users_hour = dict(spend.users_hour)
    users_day = dict(spend.users_day)
    if created >= now - HOUR:
        users_hour[who] = users_hour.get(who, 0) + used
    if created >= now - DAY:
        users_day[who] = users_day.get(who, 0) + used
    guest_windows = dict(spend.guest_windows)
    for window, start in starts.items():
        if created >= start:
            guest_windows[window] = guest_windows.get(window, 0) + used
    return replace(
        spend,
        guest_windows=guest_windows,
        guest_hour=spend.guest_hour + (used if created >= now - HOUR else 0),
        guest_day=spend.guest_day + (used if created >= now - DAY else 0),
        users_hour=users_hour,
        users_day=users_day,
    )


def judge(spend: Spend, who: str, guests: Guests) -> Verdict:
    """The decision, from the numbers alone: no Discord text, no device call."""
    if spend.limited:
        return Verdict(False, "limited")
    if any(percent >= 100 for percent in spend.percents.values()):
        return Verdict(False, "limited")
    if guests.hourly and spend.guest_hour >= guests.hourly:
        return Verdict(False, "hourly")
    if guests.daily and spend.guest_day >= guests.daily:
        return Verdict(False, "daily")
    if guests.per_user_hourly and spend.users_hour.get(who, 0) >= guests.per_user_hourly:
        return Verdict(False, "user-hourly")
    if guests.per_user_daily and spend.users_day.get(who, 0) >= guests.per_user_daily:
        return Verdict(False, "user-daily")
    for window, percent in spend.percents.items():
        total = spend.all_windows.get(window, 0)
        guest = spend.guest_windows.get(window, 0)
        if percent > 0 and total > 0 and percent * guest / total >= guests.share * 100:
            return Verdict(False, "share")
    return Verdict(True)


def share_percent(spend: Spend) -> float:
    """The percent of the plan that belongs to approved people: the highest one.

    Each meter is measured over its own window, and the highest answer is the
    one a refusal is made from, because that is the window closest to stopping
    the plan.
    """
    highest = 0.0
    for window, percent in spend.percents.items():
        total = spend.all_windows.get(window, 0)
        if total > 0:
            highest = max(highest, percent * spend.guest_windows.get(window, 0) / total)
    return highest


def screen(config: Config, who: str, device_name: str = "",
           now: float | None = None) -> Verdict:
    """Check an approved person's ask against the plan, and say what it found."""
    if config.guests.share <= 0:
        log.info("usage: approved people are paused by the share of 0")
        return Verdict(False, "paused")
    try:
        spend = collect(config, device_name, now)
    except UsageError as error:
        log.error(f"usage: the cap cannot be checked ({error})")
        return Verdict(False, "unknown")
    verdict = judge(spend, who, config.guests)
    windows = ", ".join(f"{name} {percent:.0f}%"
                        for name, percent in sorted(spend.percents.items()))
    log.info(
        f"usage: guest {who} {'allowed' if verdict.allowed else 'refused: ' + verdict.reason} · "
        f"{windows} · plan {share_percent(spend):.1f}% is theirs · "
        f"{spend.guest_hour} this hour · {spend.guest_day} today"
    )
    return verdict


def summary(config: Config, now: float | None = None) -> str:
    """The owner's line about what approved people have used, or raises.

    Short enough for `/vigo status`, and it names the cap so a refusal is
    understood before it happens.
    """
    spend = collect(config, now=now)
    part = share_percent(spend)
    said = (f"**Guests**: {text.tokens(spend.guest_day)} today of "
            f"{text.tokens(config.guests.daily)} · {text.tokens(spend.guest_hour)} this hour "
            f"· {part:.1f}% of the plan, of {config.guests.share * 100:.1f}% kept for them")
    if spend.limited:
        said += f" · the plan is limited ({spend.limited})"
    return said
