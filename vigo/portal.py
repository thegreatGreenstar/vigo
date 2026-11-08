"""The Discord application, as links for the Developer Portal and a check.

A person installs Vigo on their own account, not in a server, so the app has
to offer User Install and a shareable install link.  Discord exposes those in
the portal, not in the API, so this module cannot turn them on.  It can say
what the portal currently holds and name the page that changes it, which is
what `vigo --invite` prints.

Nothing here reads a secret: the install link carries the public application
id, and the report is built from the application object the token can already
read.
"""

from __future__ import annotations

from .config import Config

PORTAL = "https://discord.com/developers/applications"


def user_install_link(app_id: str) -> str:
    """The link that adds the app to a person's own account.

    `integration_type=1` is USER_INSTALL and the only scope a user install
    takes is `applications.commands`: no bot user joins anything, so the
    commands work in DMs, group DMs and every server that person is in.
    """
    return (
        f"https://discord.com/oauth2/authorize?client_id={app_id}"
        "&integration_type=1&scope=applications.commands"
    )


def guild_install_link(app_id: str) -> str:
    """The link that adds the bot to a server, for an owner who wants that."""
    return (
        f"https://discord.com/oauth2/authorize?client_id={app_id}"
        "&scope=bot+applications.commands&permissions=68608"
    )


def pages(app_id: str) -> dict[str, str]:
    """The Developer Portal pages this setup changes."""
    return {
        "installation": f"{PORTAL}/{app_id}/installation",
        "bot": f"{PORTAL}/{app_id}/bot",
        "information": f"{PORTAL}/{app_id}/information",
    }


def _supports(application: dict, kind: str) -> bool:
    config = application.get("integration_types_config")
    return isinstance(config, dict) and kind in config


def _scopes(application: dict, kind: str) -> list[str]:
    config = application.get("integration_types_config")
    entry = config.get(kind) if isinstance(config, dict) else None
    params = entry.get("oauth2_install_params") if isinstance(entry, dict) else None
    scopes = params.get("scopes") if isinstance(params, dict) else None
    return [str(scope) for scope in scopes] if isinstance(scopes, list) else []


def report(settings: Config, application: dict) -> tuple[str, bool]:
    """What the portal holds, the link to share, and whether the two agree.

    The second value is false when the app cannot offer the install the
    settings register commands for; each reason names the portal page that
    fixes it.  A false answer is not an error in the bot: it is the setup that
    is unfinished, and `--invite` exits non-zero so a script notices.
    """
    app_id = settings.app_id
    where = pages(app_id)
    user = _supports(application, "1")
    guild = _supports(application, "0")
    public = bool(application.get("bot_public"))
    scopes = _scopes(application, "1")
    scope_ok = "applications.commands" in scopes
    owner = application.get("owner") if isinstance(application.get("owner"), dict) else {}
    owner_id = str(owner.get("id") or "")
    owner_name = str(owner.get("username") or "the application's owner")

    lines = [
        "Vigo's install link for other people:",
        f"  {user_install_link(app_id)}",
        "",
        "Discord reports:",
        f"  Owner               {owner_name} ({owner_id or 'not named'})",
        f"  User Install        {'on' if user else 'off'}",
        f"  Guild Install       {'on' if guild else 'off'}",
        f"  Public Bot          {'on' if public else 'off'}",
        f"  User install scope  {', '.join(scopes) if scopes else 'none'}",
        "",
        "Developer Portal:",
        f"  Installation   {where['installation']}",
        f"  Bot            {where['bot']}",
        f"  Information    {where['information']}",
        "",
        f"Settings: install is {settings.install!r}, "
        f"requests are {'on' if settings.requests else 'off'}.",
    ]

    problems: list[str] = []
    if str(application.get("id") or "") not in ("", app_id):
        problems.append(
            f"The token belongs to application {application.get('id')}, but 'appID' "
            f"is {app_id}; copy the Application ID again"
        )
    if not owner_id:
        problems.append("Discord did not name the application's owner; set 'owner' in the file")
    elif settings.owner and settings.owner != owner_id:
        problems.append(
            f"The file's 'owner' is {settings.owner}, but the application belongs to "
            f"{owner_id}; remove 'owner' or set it to the application's owner"
        )
    if not user:
        problems.append(
            "Turn on User Install, and add the applications.commands scope "
            f"under Default Install Settings, at {where['installation']}"
        )
    elif not scope_ok:
        problems.append(
            "Add the applications.commands scope for User Install at "
            f"{where['installation']}"
        )
    if not public:
        problems.append(
            f"Turn on Public Bot at {where['bot']}; a private app cannot hold "
            "an install link that somebody else can use"
        )
    if settings.install in ("user", "both") and not user:
        problems.append(
            "The commands are registered for a user install the app does not offer"
        )
    if settings.install in ("guild", "both") and not guild:
        problems.append(
            'The commands are registered for a guild install the app does not '
            'offer; set "install": "user" in the settings, or turn on Guild Install'
        )
    if settings.install == "guild":
        problems.append(
            'The settings register only a guild install, so a user install has '
            'no commands; set "install": "user" or "both" to let other people '
            "add it to their account"
        )

    if problems:
        lines.append("")
        lines.append("Finish this in the Developer Portal:")
        lines.extend(f"  - {problem}" for problem in problems)
    elif settings.requests:
        lines.append("")
        lines.append(
            "Ready: share the link above. Anyone who installs it can ask for "
            "access, and nothing runs until you approve."
        )
    else:
        lines.append("")
        lines.append(
            "Ready: share the link above. Requests are off, so a stranger who "
            "installs it is refused until you allow them with `/vigo allow`."
        )
    return "\n".join(lines), not problems
