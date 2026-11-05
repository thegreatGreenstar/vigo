"""The slash command: what Discord shows, and how its options arrive."""

from __future__ import annotations

ASK = "ask"
QUICK = "quick"
GOAL = "goal"
UPGRADE = "upgrade"
STOP = "stop"
STATUS = "status"
DEVICES = "devices"
ALLOW = "allow"
DENY = "deny"
ALLOWED = "allowed"
HELP = "help"

TIMES = [("2h", "2 hours"), ("1d", "1 day"), ("7d", "7 days"), ("perm", "permanent")]
SECONDS = {"2h": 7200, "1d": 86400, "7d": 604800, "perm": None}

# Where a command may appear, by how the app is installed.  A user install
# (your own profile) lives in DMs and private channels; a guild install lives
# in servers.  Discord refuses a context that the install cannot reach.
INSTALLS = {
    "both": ([0, 1], [0, 1, 2]),
    "user": ([1], [1, 2]),
    "guild": ([0], [0]),
}


def spec(install: str = "both") -> list[dict]:
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
            "description": "Ask the OpenCode agent on your own devices.",
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
                            "type": 3,
                            "name": "device",
                            "description": "macbook or pc; empty picks where you last worked.",
                            "required": False,
                        },
                        {
                            "type": 5,
                            "name": "goal",
                            "description": (
                                "Work toward it until it is done instead of answering once; "
                                "empty decides from the wording."
                            ),
                            "required": False,
                        },
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
                    "description": "Give this channel's agent a goal to keep working toward.",
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
                        {
                            "type": 3,
                            "name": "device",
                            "description": (
                                "macbook or pc; empty stays on this channel's device."
                            ),
                            "required": False,
                        },
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
                        {
                            "type": 3,
                            "name": "device",
                            "description": "macbook or pc; empty picks where you last worked.",
                            "required": False,
                        },
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
    """The subcommand's name and its options, as Discord sends them."""
    options = data.get("options") or []
    if not options:
        return "", {}
    chosen = options[0]
    name = chosen.get("name", "")
    values: dict = {}
    for option in chosen.get("options") or []:
        values[option.get("name", "")] = option.get("value")
    return name, values


def user_id(value) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return ""


def user_name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("global_name") or value.get("username") or value.get("id") or "they")
    return "they"
