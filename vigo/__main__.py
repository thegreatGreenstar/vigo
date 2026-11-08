from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

from . import __version__, config as configuration, log, watchdog
from .gateway import FatalGatewayError


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
    parser.add_argument("--log", default="info", choices=["quiet", "error", "info", "debug"])
    parser.add_argument("--version", action="version", version=f"vigo {__version__}")
    args = parser.parse_args(argv)
    log.level(args.log)
    log.install()

    try:
        settings = configuration.load(args.config)
    except configuration.ConfigError as error:
        log.error(str(error))
        return 2

    if args.command == "dm":
        return _dm(settings, args.words)

    if args.check:
        _report(settings)
        return 0

    if args.whoami:
        from .discord_api import Discord, DiscordError

        try:
            me = Discord(settings.token, settings.app_id).me()
        except DiscordError as error:
            log.error(str(error))
            return 1
        print(f"the token is {me.get('username')} ({me.get('id')})")
        return 0

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

    from .app import App

    app = App(settings)
    _report(settings)
    began = time.monotonic()
    log.info(f"start: vigo {__version__}, python {sys.version.split()[0]}, "
             f"pid {os.getpid()}, {sys.platform}, log {args.log}")
    try:
        app.run()
    except KeyboardInterrupt:
        log.info("stopping")
    except FatalGatewayError as error:
        log.error(f"vigo: {error}")
        return 1
    except Exception as error:
        log.failure("vigo: the daemon stopped on an error", error)
        return 1
    finally:
        watchdog.stop()
        log.info(f"stopped after {time.monotonic() - began:.1f}s")
    return 0


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
    from .discord_api import DiscordError
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
    print(f"owner {settings.owner} · devices: {devices}")
    print(f"model {settings.model} · directory {settings.own.directory}")
    print(f"guests: a {settings.guests.share:.1%} share of the plan · "
          f"{settings.guests.hourly}/hour · {settings.guests.daily}/day")
    if not settings.state_channel:
        print("allowlist: the bot's DM with the owner (no stateChannel in the file)")


if __name__ == "__main__":
    sys.exit(main())
