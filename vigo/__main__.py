from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config as configuration, log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vigo", description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="the settings file")
    parser.add_argument("--check", action="store_true", help="read the settings and stop")
    parser.add_argument("--devices", action="store_true", help="probe every device and stop")
    parser.add_argument("--whoami", action="store_true",
                        help="check the bot token and name the bot, then stop")
    parser.add_argument("--log", default="info", choices=["quiet", "error", "info", "debug"])
    parser.add_argument("--version", action="version", version=f"vigo {__version__}")
    args = parser.parse_args(argv)
    log.level(args.log)

    try:
        settings = configuration.load(args.config)
    except configuration.ConfigError as error:
        log.error(str(error))
        return 2

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
        print(f"A question would go to `{pick.name}` — {pick.reason}." if pick.name
              else f"No device answers: {pick.reason}.")
        return 0

    from .app import App

    app = App(settings)
    _report(settings)
    try:
        app.run()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


def _report(settings) -> None:
    devices = ", ".join(sorted(settings.devices))
    print(f"vigo {__version__} · this device is {settings.device}")
    print(f"owner {settings.owner} · devices: {devices}")
    print(f"model {settings.model} · directory {settings.own.directory}")
    if not settings.state_channel:
        print("allowlist: the bot's DM with the owner (no stateChannel in the file)")


if __name__ == "__main__":
    sys.exit(main())
