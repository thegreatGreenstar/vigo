"""The slash command: what Discord shows, and how its options arrive."""

from __future__ import annotations

ASK = "ask"
QUICK = "quick"
GOAL = "goal"
UPGRADE = "upgrade"
STOP = "stop"
STATUS = "status"
QUEUE = "queue"
DEVICES = "devices"
ALLOW = "allow"
DENY = "deny"
ALLOWED = "allowed"
HELP = "help"

TIMES = [
    ("2h", "2 hours"),
    ("1d", "1 day"),
    ("7d", "7 days"),
    ("30d", "1 month"),
    ("180d", "6 months"),
    ("365d", "1 year"),
    ("perm", "permanent"),
]
SECONDS = {
    "2h": 7200,
    "1d": 86400,
    "7d": 604800,
    "30d": 30 * 86400,
    "180d": 180 * 86400,
    "365d": 365 * 86400,
    "perm": None,
}

# Where a command may appear, by how the app is installed.  A user install
# (your own profile) is visible in every server, DM and group DM that person
# opens, and a guild install is visible in the servers the app joined; Discord
# refuses a context that the install cannot reach.  A user install that omits
# GUILD would vanish from the command picker of every server, which is the
# surface its owner installed it for.
INSTALLS = {
    "both": ([0, 1], [0, 1, 2]),
    "user": ([1], [0, 1, 2]),
    "guild": ([0], [0]),
}


def _device_option(devices: list[str] | None, tail: str) -> dict:
    """The device option, naming the devices this daemon knows.

    Discord allows 100 characters for an option description, so a long list is
    counted instead of named.  The names come from the settings, never from
    this file: a device that is down is still known, and one that was removed
    is gone at once.
    """
    names = [name for name in (devices or []) if name]
    if not names:
        text = f"A device from the settings; {tail}"
    elif len(", ".join(names)) + len(tail) + 2 <= 100:
        text = f"{', '.join(names)}; {tail}"
    else:
        text = f"One of the {len(names)} devices in the settings; {tail}"
    return {"type": 3, "name": "device", "description": text, "required": False}


def spec(install: str = "both", devices: list[str] | None = None) -> list[dict]:
    integration_types, contexts = INSTALLS.get(install, INSTALLS["both"])
    hidden = {
        "type": 5,
        "name": "hidden",
        "description": "Only you see this; off means everyone in the channel sees it.",
        "required": False,
    }
    return [
        {
            "name": "vigo",
            "description": "Ask the OpenCode agent behind this app.",
            "type": 1,
            "integration_types": integration_types,
            "contexts": contexts,
            "options": [
                {
                    "type": 1,
                    "name": ASK,
                    "description": "Ask a device's OpenCode a question.",
                    "options": [
                        {
                            "type": 3,
                            "name": "prompt",
                            "description": "What to ask; a follow-up continues this channel's session.",
                            "required": True,
                        },
                        {
                            "type": 11,
                            "name": "file",
                            "description": (
                                "Any file to read with the question: a log, dump, image, "
                                "PDF, archive or source file."
                            ),
                            "required": False,
                        },
                        _device_option(devices, "empty picks where you last worked."),
                        {
                            "type": 5,
                            "name": "hidden",
                            "description": "Only you see this question and its answer.",
                            "required": False,
                        },
                        {
                            "type": 5,
                            "name": "new",
                            "description": "Start a fresh session instead of continuing this channel's.",
                            "required": False,
                        },
                    ],
                },
                {
                    "type": 1,
                    "name": GOAL,
                    "description": "Give the device a goal to keep working toward, in a session of its own.",
                    "options": [
                        {
                            "type": 3,
                            "name": "objective",
                            "description": (
                                "What to achieve; pause, resume or clear control the "
                                "one it already has."
                            ),
                            "required": True,
                        },
                        _device_option(
                            devices, "empty stays on this channel's device."),
                        dict(hidden),
                    ],
                },
                {
                    "type": 1,
                    "name": QUICK,
                    "description": "One answer, no session and no tools: it forgets the question.",
                    "options": [
                        {
                            "type": 3,
                            "name": "prompt",
                            "description": "What to ask.",
                            "required": True,
                        },
                        _device_option(devices, "empty picks where you last worked."),
                        dict(hidden),
                    ],
                },
                {
                    "type": 1,
                    "name": UPGRADE,
                    "description": "Pull and restart a daemon that is behind. Only you.",
                    "options": [
                        {
                            "type": 3,
                            "name": "device",
                            "description": "Which one; empty upgrades every daemon that is behind.",
                            "required": False,
                        },
                    ],
                },
                {
                    "type": 1,
                    "name": STOP,
                    "description": "Stop what is running in this channel, and drop what waits.",
                    "options": [dict(hidden)],
                },
                {
                    "type": 1,
                    "name": STATUS,
                    "description": "What this channel is doing and what waits in it. Only you ever see this.",
                },
                {
                    "type": 1,
                    "name": QUEUE,
                    "description": "What runs and what waits, in every channel and on every daemon.",
                    "options": [
                        {
                            "type": 5,
                            "name": "public",
                            "description": "The channel sees it too, with every task that names a person, a path or a machine redacted.",
                            "required": False,
                        },
                    ],
                },
                {
                    "type": 1,
                    "name": DEVICES,
                    "description": "Which devices answer, and which one a question would go to. Only you ever see this.",
                },
                {
                    "type": 1,
                    "name": ALLOW,
                    "description": "Let someone ask, for a time. Only you ever see this.",
                    "options": [
                        {"type": 6, "name": "user", "description": "Who.", "required": True},
                        {
                            "type": 3,
                            "name": "for",
                            "description": "How long; 1 day when empty.",
                            "required": False,
                            "choices": [
                                {"name": label, "value": value} for value, label in TIMES
                            ],
                        },
                    ],
                },
                {
                    "type": 1,
                    "name": DENY,
                    "description": "Take a time allow back. Only you ever see this.",
                    "options": [
                        {"type": 6, "name": "user", "description": "Who.", "required": True},
                    ],
                },
                {
                    "type": 1,
                    "name": ALLOWED,
                    "description": "Who may ask right now. Only you ever see this.",
                },
                {
                    "type": 1,
                    "name": HELP,
                    "description": "What this bot does, and how to use it.",
                    "options": [dict(hidden)],
                },
            ],
        }
    ]


def relevant(commands: list[dict]) -> list[dict]:
    others = [
        command for command in commands
        if command.get("name") != "vigo"
    ]
    return others


def flatten(data: dict) -> tuple[str, dict]:
    """The subcommand's name and its options, as Discord sends them.

    A file arrives as the id of an attachment, and a person arrives as the id
    of a user, and their names, size and link are in the payload's `resolved`
    part.  The id alone names nothing anyone can read, so both are resolved
    here and the option carries what a reader needs.
    """
    options = data.get("options") or []
    if not options:
        return "", {}
    chosen = options[0]
    name = chosen.get("name", "")
    resolved = data.get("resolved") or {}
    files = resolved.get("attachments") or {}
    users = resolved.get("users") or {}
    values: dict = {}
    for option in chosen.get("options") or []:
        value = option.get("value")
        if option.get("type") == 11:
            found = files.get(str(value)) or {}
            value = {
                "id": str(found.get("id") or ""),
                "filename": found.get("filename"),
                "url": found.get("url"),
                "size": found.get("size"),
                "content_type": found.get("content_type"),
            }
        elif option.get("type") == 6 and isinstance(value, str):
            # Discord sends the id for a user option, not the user object: the
            # object is under `resolved.users`.  Replacing the id with what it
            # names keeps the person's name in what the bot says.
            value = users.get(value) or value
        values[option.get("name", "")] = value
    return name, values


def user_id(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    if isinstance(value, str) and value.isdigit():
        return value
    return ""


def user_name(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("global_name") or value.get("username") or value.get("id") or "they")
    return "they"
