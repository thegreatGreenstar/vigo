from __future__ import annotations

import argparse
import dataclasses
import io
import sys
from pathlib import Path

from . import __version__, config as configuration, log
from .discord_api import Discord, DiscordError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vigo", description=__doc__)
    parser.add_argument("command", nargs="?", default="run", choices=["run", "dm"],
                        help="run the bot (default), or dm the owner a message")
    parser.add_argument("words", nargs="*", help="the message, for `dm`; stdin is read when empty")
    parser.add_argument("--config", type=Path, default=None, help="the settings file")
    parser.add_argument("--check", action="store_true", help="read the settings and stop")
    parser.add_argument("--devices", action="store_true", help="probe every device and stop")
    parser.add_argument("--whoami", action="store_true",
                        help="check the bot token and name the bot, then stop")
    parser.add_argument("--invite", action="store_true",
                        help="print the user install link and the portal checklist, then stop")
    parser.add_argument("--log", default="info", choices=["quiet", "error", "info", "debug"])
    parser.add_argument("--version", action="version", version=f"vigo {__version__}")
    args = parser.parse_args(argv)
    log.level(args.log)

    try:
        settings = configuration.load(args.config)
    except configuration.ConfigError as error:
        log.error(str(error))
        return 2

    if args.command == "dm":
        resolved = _resolve_owner(settings)
        if resolved is None:
            return 1
        return _dm(resolved, args.words)

    if args.check:
        _report(settings)
        return 0

    if args.whoami:
        client = Discord(settings.token, settings.app_id)
        try:
            me = client.me()
            application = client.application()
        except DiscordError as error:
            log.error(str(error))
            return 1
        owner = _application_owner(application)
        print(f"the token is {me.get('username')} ({me.get('id')})")
        print(f"the application belongs to {owner or 'nobody Discord named'}")
        return 0

    if args.invite:
        return _invite(settings)

    if args.devices:
        from .router import Router

        router = Router(settings)
        states = router.states(fresh=True)
        print(router.describe(states))
        pick = router.pick()
        print()
        print(f"A question would go to `{pick.name}`, {pick.reason}." if pick.name
              else f"No device answers: {pick.reason}.")
        return 0

    resolved = _resolve_owner(settings)
    if resolved is None:
        return 1
    from .app import App

    app = App(resolved)
    _report(resolved)
    try:
        app.run()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


def _application_owner(application: dict) -> str:
    """The user id Discord says owns the application, or "" when it names none."""
    owner = application.get("owner")
    return str(owner.get("id") or "") if isinstance(owner, dict) else ""


def _resolve_owner(settings: configuration.Config,
                   api: Discord | None = None) -> configuration.Config | None:
    """Tie this daemon's owner to the Discord application that runs it.

    Every person who makes their own Vigo makes their own application, so the
    application's owner is the only id that is right for owner-only commands.
    A file may still name `owner`, but it has to be that same id: an id typed
    by hand must not turn somebody else's application into their own.

    Returns the settings with the owner filled in, or None when Discord cannot
    name it or the file disagrees.  The caller stops with the log as the reason.
    """
    client = api or Discord(settings.token, settings.app_id)
    try:
        application = client.application()
    except DiscordError as error:
        log.error(f"owner: cannot read the application ({error})")
        return None
    found = _application_owner(application)
    belongs = str(application.get("id") or "")
    if belongs and belongs != settings.app_id:
        log.error(
            f"owner: the token belongs to application {belongs}, but 'appID' is "
            f"{settings.app_id}; copy the Application ID again"
        )
        return None
    if not found:
        log.error("owner: Discord did not name the application's owner, so the "
                  "owner-only commands cannot be decided")
        return None
    if settings.owner and settings.owner != found:
        log.error(
            f"owner: the file names {settings.owner}, but the Discord application "
            f"belongs to {found}; remove 'owner' or set it to the application's owner"
        )
        return None
    if not settings.owner:
        log.info(f"owner: the application belongs to {found}")
        return dataclasses.replace(settings, owner=found)
    return settings


def _invite(settings: configuration.Config,
            api: Discord | None = None) -> int:
    """Print the install link and what the portal still has to change."""
    from . import portal

    client = api or Discord(settings.token, settings.app_id)
    try:
        application = client.application()
    except DiscordError as error:
        log.error(f"invite: {error}")
        return 1
    text, ready = portal.report(settings, application)
    print(text)
    return 0 if ready else 1


def message_of(words: list[str], stdin: io.TextIOBase | None = None) -> str:
    """The words given, or what was piped in when there are none."""
    said = " ".join(word for word in words if word).strip()
    if said:
        return said
    if stdin is None:
        stdin = sys.stdin
    if stdin is None or stdin.isatty():
        return ""
    return stdin.read().strip()


def _dm(settings: configuration.Config, words: list[str]) -> int:
    """DM the owner.  `vigo dm done` is what a session on this machine uses."""
    from .notify import send_owner

    said = message_of(words)
    if not said:
        log.error("dm: nothing to say")
        return 2
    try:
        sent = send_owner(settings, said)
    except DiscordError as error:
        log.error(f"dm: Discord says {error.message}")
        return 1
    except ValueError as error:
        log.error(f"dm: {error}")
        return 2
    print(f"sent {sent} message{'' if sent == 1 else 's'} to the owner's DM")
    return 0


def _report(settings: configuration.Config) -> None:
    devices = ", ".join(sorted(settings.devices))
    print(f"vigo {__version__} · this device is {settings.device}")
    if settings.owner:
        print(f"owner {settings.owner} · devices: {devices}")
    else:
        print(f"owner: the Discord application's owner, read at start · devices: {devices}")
    print(f"model {settings.model} · directory {settings.own.directory}")
    print(f"guests: a {settings.guests.share:.1%} share of the plan · "
          f"{settings.guests.hourly}/hour · {settings.guests.daily}/day")
    print(f"access requests from other people are "
          f"{'on' if settings.requests else 'off'}")
    if not settings.state_channel:
        print("allowlist: the bot's DM with the owner (no stateChannel in the file)")


if __name__ == "__main__":
    sys.exit(main())
