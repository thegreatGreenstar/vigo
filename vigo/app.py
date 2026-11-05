"""The bot: one gateway connection, one worker pool, one answer per question."""

from __future__ import annotations

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ask as asking
from . import commands, goal, log, text
from .allowlist import AllowError, AllowList
from .config import Config
from .discord_api import Discord, DiscordError
from .gateway import Gateway
from . import __version__
from .notify import send_owner
from .opencode_api import OpenCode, OpenCodeError, busy_since
from .peer import PeerClient, PeerServer, build_of, head_of, update_self
from .router import Router
from .state import State

INTENTS = 0  # no gateway events are needed: interactions arrive without intents

WAITING_MAX = 10   # questions a channel may hold behind the one that runs
FINISHED_MAX = 10  # the last questions that ran, which `/vigo status` can name
GOAL_WORDS = ("pause", "resume", "clear")  # `/vigo goal` control, not objectives


def _strip(command: dict) -> dict:
    """The parts of a command that decide whether Discord's copy is ours."""
    return {
        "name": command.get("name"),
        "description": command.get("description"),
        "type": command.get("type", 1),
        "integration_types": command.get("integration_types"),
        "contexts": command.get("contexts"),
        "options": [
            {
                "type": option.get("type"),
                "name": option.get("name"),
                "description": option.get("description"),
                "required": bool(option.get("required", False)),
                **({"choices": option.get("choices")} if option.get("choices") else {}),
                "options": [
                    {
                        "type": inner.get("type"),
                        "name": inner.get("name"),
                        "description": inner.get("description"),
                        "required": bool(inner.get("required", False)),
                        **({"choices": inner.get("choices")} if inner.get("choices") else {}),
                    }
                    for inner in (option.get("options") or [])
                ],
            }
            for option in (command.get("options") or [])
        ],
    }


def _select(custom_id: str, options: list[dict], maximum: int, placeholder: str) -> dict:
    return {
        "type": 3,
        "custom_id": custom_id,
        "placeholder": placeholder,
        "min_values": 1,
        "max_values": max(1, min(maximum, 25)),
        "options": [
            {"label": text.shorten(option["label"], 100), "value": str(position)}
            for position, option in enumerate(options[:25])
        ],
    }


def _answer_text(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "(nothing)"
    return str(value)


class App:
    def __init__(self, config: Config, api: Discord | None = None, state: State | None = None,
                 checkout: Path | None = None):
        self.checkout = checkout
        self._build: str | None = None
        self.config = config
        self.api = api or Discord(config.token, config.app_id)
        self.router = Router(config)
        self.state = state or State()
        self.allow = AllowList(config, self.api, self.state)
        self.runs: dict[str, asking.Run] = {}
        self.waiting: dict[str, list[asking.Ask]] = {}
        self.finished: dict[str, list[str]] = {}
        self.runs_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="vigo")
        self.started = time.time()

    # -- runs -------------------------------------------------------------

    def register_run(self, channel: str, run: asking.Run) -> None:
        with self.runs_lock:
            self.runs[channel] = run

    def unregister_run(self, channel: str, run: asking.Run | None = None) -> None:
        """The run ended: remember its question, and start the next one.

        Only the run that holds the channel may give it up.  A run calls this
        from its own thread and again from its `finally`, and the second call
        must not take the *next* run's place: doing that left one run invisible
        while it worked, started another beside it, and posted the same answer
        twice.
        """
        with self.runs_lock:
            mine = run is None or self.runs.get(channel) is run
            if not mine:
                return
            ended = self.runs.pop(channel, None)
            if ended is not None and ended.prompt:
                done = self.finished.setdefault(channel, [])
                done.append(ended.prompt)
                del done[:-FINISHED_MAX]
        if ended is not None:
            self.start_next(channel)

    def start_next(self, channel: str) -> None:
        """Start the first waiting question, when nothing runs in the channel."""
        with self.runs_lock:
            line = self.waiting.get(channel) or []
            if channel in self.runs or not line:
                return
            worker = line.pop(0)
            if not line:
                del self.waiting[channel]
            self.runs[channel] = worker.run
            left = len(line)
        log.debug(f"ask: starting the next question in this channel ({left} left)")
        threading.Thread(target=worker.run_it, name=f"ask-{channel}", daemon=True).start()

    def add_waiting(self, channel: str, worker: asking.Ask) -> int | None:
        """Queue a question behind the run; answer how many run before it.

        ``None`` means the channel's line is full: the question is not queued
        and the caller says so.
        """
        with self.runs_lock:
            line = self.waiting.setdefault(channel, [])
            if len(line) >= WAITING_MAX:
                return None
            line.append(worker)
            return len(line) - 1 + (1 if channel in self.runs else 0)

    def shutdown(self) -> None:
        """Stop every run and the worker pool; used on the way out and by tests."""
        with self.runs_lock:
            runs = list(self.runs.values())
            self.waiting.clear()  # a stopping run must not start the next one
        for run in runs:
            run.stop.set()
        self.pool.shutdown(wait=False)

    def run_for(self, channel: str) -> asking.Run | None:
        with self.runs_lock:
            return self.runs.get(channel)

    def _device_busy(self, channel: str, device_name: str, new: bool) -> bool:
        """Whether the device still works on this channel's last question.

        The run is not always this daemon's: any daemon may answer the command
        that started it, and a run outlives a timeout and a restart.  The
        device's own session is the one record they share, so ask it.
        """
        if new:
            return False  # a fresh session, whatever the old one does
        remembered = self.state.session_on(channel, device_name)
        if not remembered:
            return False
        device = self.config.device_named(device_name)
        if device is None:
            return False
        try:
            info = OpenCode(device.url, device.password).session(remembered["session"])
        except OpenCodeError:
            return False  # a device that does not answer says so when asked
        return busy_since(info) is not None

    # -- questions the agent asks -----------------------------------------

    def ask_question(self, *, device: str, session: str, form: str,
                     title: str, fields: list[dict], channel: str) -> bool:
        """Relay a form to the owner's DM, one message when it fits.

        The question tool asks one or more fields and waits for every answer
        before it returns, so the answers stay here until the last field is
        answered, and then one reply carries all of them.
        """
        prepared: list[dict] = []
        for index, field in enumerate(fields):
            if not isinstance(field, dict):
                continue
            key = str(field.get("key") or "")
            if not key:
                continue
            options = []
            for item in field.get("options") or []:
                if not isinstance(item, dict):
                    continue
                value = item.get("value")
                if value is None:
                    continue
                options.append({"value": str(value),
                                "label": str(item.get("label") or value)})
            prepared.append({
                "index": index,
                "key": key,
                "title": str(field.get("title") or title or "Question"),
                "description": str(field.get("description") or ""),
                "kind": str(field.get("type") or "string"),
                "options": options,
                "custom": bool(field.get("custom", True)),
            })
        if not prepared:
            log.error("question: the form has no field that can be shown")
            return False
        ticket = secrets.token_hex(4)
        self.state.new_form(ticket, device, session, form, prepared, channel)
        try:
            dm = self.api.open_dm(self.config.owner)
        except DiscordError as error:
            log.error(f"question: no DM with the owner ({error.message})")
            self.state.close_form(ticket)
            return False
        messages: list[dict] = []
        failed = False
        for chunk in self._form_chunks(ticket, prepared):
            try:
                sent = self.api.send(dm, self._form_payload(ticket, chunk, {}))
            except DiscordError as error:
                log.error(f"question: cannot send the card ({error.message})")
                failed = True
                continue
            messages.append({
                "channel": dm,
                "id": str(sent.get("id") or ""),
                "fields": [field["index"] for field in chunk],
            })
        if failed or not messages:
            self.state.close_form(ticket)
            return False
        self.state.form_messages(ticket, messages)
        log.info(f"question: card {ticket} sent to the owner's DM")
        return True

    def _form_chunks(self, ticket: str, fields: list[dict]) -> list[list[dict]]:
        """Split fields into messages of at most five rows and one message of text."""
        chunks: list[list[dict]] = []
        current: list[dict] = []
        rows = 0
        length = len("**Question from the agent**")
        for field in fields:
            height = len(self._field_rows(ticket, field))
            cost = (len(text.shorten(field["title"], 200))
                    + len(text.shorten(field["description"], 700)) + 40)
            if current and (rows + height > 5 or length + cost > 1500):
                chunks.append(current)
                current = []
                rows = 0
                length = len("**Question from the agent**")
            current.append(field)
            rows += height
            length += cost
        if current:
            chunks.append(current)
        return chunks

    def _field_rows(self, ticket: str, field: dict) -> list[dict]:
        """The components for one field: buttons for a few, a select for many."""
        index = field["index"]
        options = field["options"]
        custom = field["custom"]
        other = {
            "type": 1,
            "components": [{
                "type": 2, "style": 1, "label": "Other",
                "custom_id": f"q:{ticket}:c:{index}",
            }],
        }
        if field["kind"] == "boolean":
            buttons = [
                {"type": 2, "style": 1, "label": "Yes",
                 "custom_id": f"q:{ticket}:b:{index}:1"},
                {"type": 2, "style": 1, "label": "No",
                 "custom_id": f"q:{ticket}:b:{index}:0"},
            ]
            if custom:
                buttons.append({"type": 2, "style": 1, "label": "Other",
                                "custom_id": f"q:{ticket}:c:{index}"})
            return [{"type": 1, "components": buttons}]
        if options and field["kind"] == "multiselect":
            rows = [{"type": 1, "components": [_select(
                f"q:{ticket}:s:{index}", options, len(options), "Choose one or more")]}]
            return rows + ([other] if custom else [])
        if options and len(options) <= 4:
            buttons = [
                {"type": 2, "style": 2, "label": text.shorten(option["label"], 80),
                 "custom_id": f"q:{ticket}:o:{index}:{position}"}
                for position, option in enumerate(options)
            ]
            if custom:
                buttons.append({"type": 2, "style": 1, "label": "Other",
                                "custom_id": f"q:{ticket}:c:{index}"})
            return [{"type": 1, "components": buttons}]
        if options:
            rows = [{"type": 1, "components": [_select(
                f"q:{ticket}:s:{index}", options, 1, "Choose one")]}]
            return rows + ([other] if custom else [])
        return [other] if custom else []

    def _form_payload(self, ticket: str, fields: list[dict],
                      answers: dict) -> dict:
        lines = ["**Question from the agent**"]
        for field in fields:
            lines.append(f"**{field['index'] + 1}. {text.shorten(field['title'], 200)}**")
            if field["description"]:
                lines.append(text.shorten(field["description"], 700))
            if field["key"] in answers:
                lines.append(f"answered: {_answer_text(answers[field['key']])}")
        rows: list[dict] = []
        for field in fields:
            if field["key"] not in answers:
                rows.extend(self._field_rows(ticket, field))
        return {
            "content": "\n".join(lines)[:1900],
            "components": rows[:5],
            "allowed_mentions": {"parse": []},
        }

    # -- permission cards -------------------------------------------------

    def ask_permission(self, *, device: str, session: str, request: str,
                       action: str, resources: list[str], channel: str) -> bool:
        ticket = secrets.token_hex(4)
        self.state.new_ticket(ticket, device, session, request, channel)
        shown = "\n".join(f"> `{text.shorten(item, 180)}`" for item in resources if item) or "> (no path)"
        content = (
            f"**Permission needed** on `{device}`\n"
            f"The agent asks to run `{text.shorten(action, 120)}`:\n{shown}"
        )
        components = [{
            "type": 1,
            "components": [
                {"type": 2, "style": 1, "label": "Allow once", "custom_id": f"pw:{ticket}:a"},
                {"type": 2, "style": 3, "label": "Always allow", "custom_id": f"pw:{ticket}:w"},
                {"type": 2, "style": 4, "label": "Deny", "custom_id": f"pw:{ticket}:d"},
            ],
        }]
        payload = {
            "content": content,
            "components": components,
            "allowed_mentions": {"parse": []},
        }
        try:
            dm = self.api.open_dm(self.config.owner)
            self.api.send(dm, payload)
            log.info(f"perm: card {ticket} sent to the owner's DM")
            return True
        except DiscordError as error:
            # Never post a card in a channel: it names the action and the paths
            # it touches.  The run says approval is needed, and keeps the rest here.
            log.error(f"perm: no DM with the owner ({error.message})")
        self.state.close_ticket(ticket)
        return False

    def _component(self, payload: dict, who: str) -> None:
        data = payload.get("data") or {}
        custom = str(data.get("custom_id") or "")
        if custom.startswith("q:"):
            self._question_component(payload, who, data)
            return
        if not custom.startswith("pw:"):
            return
        parts = custom.split(":")
        if len(parts) != 3:
            return
        _, ticket, choice = parts
        decision = {"a": "once", "w": "always", "d": "reject"}.get(choice)
        interaction_id = payload["id"]
        token = payload["token"]
        if who != self.config.owner or decision is None:
            self.api.respond(interaction_id, token, {
                "type": 4,
                "data": {"content": "Only the bot's owner answers this card.", "flags": 64},
            })
            return
        info = self.state.ticket(ticket)
        if not info:
            self.api.respond(interaction_id, token, {
                "type": 4,
                "data": {"content": "That card is old; the request is gone.", "flags": 64},
            })
            return
        device = self.config.device_named(info["device"])
        if device is None:
            self.api.respond(interaction_id, token, {
                "type": 4,
                "data": {"content": f"`{info['device']}` is not in the file any more.", "flags": 64},
            })
            return
        client = OpenCode(device.url, device.password)
        try:
            client.reply_permission(info["session"], info["request"], decision)
        except OpenCodeError as error:
            log.error(f"perm: {ticket} could not be answered ({error.message})")
            self.api.respond(interaction_id, token, {
                "type": 4,
                "data": {"content": f"OpenCode would not take it: {error.message}", "flags": 64},
            })
            return
        self.state.close_ticket(ticket)
        said = {"once": "Allowed once.", "always": "Allowed, and saved.", "reject": "Denied."}[decision]
        self.api.respond(interaction_id, token, {
            "type": 7,
            "data": {
                "content": f"{said} `{text.shorten(info_request(info), 120)}`",
                "components": [],
                "allowed_mentions": {"parse": []},
            },
        })
        log.info(f"perm: {ticket} {decision} on {info['device']}")

    def _question_component(self, payload: dict, who: str, data: dict) -> None:
        parts = str(data.get("custom_id") or "").split(":")
        if len(parts) < 4:
            return
        _, ticket, action, field_index = parts[:4]
        interaction_id = payload["id"]
        token = payload["token"]
        if who != self.config.owner:
            self._answer_now(interaction_id, token,
                             "Only the bot's owner answers this question.", ephemeral=True)
            return
        info = self.state.form(ticket)
        if not info:
            self._answer_now(interaction_id, token,
                             "That question is old; it is no longer waiting.", ephemeral=True)
            return
        field = next((item for item in info["fields"]
                      if str(item["index"]) == field_index), None)
        if field is None:
            self._answer_now(interaction_id, token,
                             "That question is no longer waiting.", ephemeral=True)
            return
        if action == "c":
            self._question_modal(interaction_id, token, ticket, field)
            return
        if action == "b":
            value = (parts[4] if len(parts) > 4 else "") == "1"
        elif action == "s":
            chosen = []
            for item in data.get("values") or []:
                try:
                    chosen.append(field["options"][int(str(item))]["value"])
                except (IndexError, ValueError):
                    continue
            if not chosen:
                self._answer_now(interaction_id, token,
                                 "No choice was sent.", ephemeral=True)
                return
            value = chosen if field["kind"] == "multiselect" else chosen[0]
        elif action == "o":
            try:
                value = field["options"][int(parts[4])]["value"]
            except (IndexError, ValueError):
                self._answer_now(interaction_id, token,
                                 "That choice is not there any more.", ephemeral=True)
                return
        else:
            return
        self._question_answer(payload, ticket, field, value)

    def _question_answer(self, payload: dict, ticket: str, field: dict, value) -> None:
        interaction_id = payload["id"]
        token = payload["token"]
        entry = self.state.answer_form(ticket, field["key"], value)
        if entry is None:
            self._answer_now(interaction_id, token,
                             "That question is old; it is no longer waiting.", ephemeral=True)
            return
        if len(entry["answers"]) < len(entry["fields"]):
            self._update_question_message(payload, ticket, entry)
            return
        device = self.config.device_named(entry["device"])
        if device is None:
            self.state.close_form(ticket)
            self._answer_now(interaction_id, token,
                             f"`{entry['device']}` is not in the file any more.", ephemeral=True)
            return
        try:
            OpenCode(device.url, device.password).reply_form(
                entry["session"], entry["form"], entry["answers"])
        except OpenCodeError as error:
            log.error(f"question: {ticket} could not be answered ({error.message})")
            if error.status in (404, 409):
                self.state.close_form(ticket)
                self._answer_now(interaction_id, token,
                                 "That question is no longer waiting.", ephemeral=True)
            else:
                # Keep the answers: the card is still there, so another try is possible.
                self._answer_now(interaction_id, token,
                                 f"OpenCode would not take it: {error.message}", ephemeral=True)
            return
        log.info(f"question: {ticket} answered on {entry['device']}")
        self.state.close_form(ticket)
        self._update_question_message(payload, ticket, entry, done=True)

    def _update_question_message(self, payload: dict, ticket: str, entry: dict,
                                 done: bool = False) -> None:
        message = payload.get("message") or {}
        message_id = str(message.get("id") or "")
        group = next((item for item in entry["messages"]
                      if message_id and item.get("id") == message_id), None)
        if group is None:
            group = entry["messages"][0] if entry["messages"] else {"fields": []}
        indexes = set(group.get("fields") or [])
        fields = [item for item in entry["fields"] if item["index"] in indexes]
        data = self._form_payload(ticket, fields, entry["answers"])
        if done:
            data["components"] = []
            data["content"] += "\n\nAll answered. The agent is continuing."
        self.api.respond(payload["id"], payload["token"], {"type": 7, "data": data})
        if not done:
            return
        for other in entry["messages"]:
            if other is group:
                continue
            try:
                self.api.edit(other["channel"], other["id"], {
                    "content": "All answered. The agent is continuing.",
                    "components": [],
                    "allowed_mentions": {"parse": []},
                })
            except DiscordError as error:
                log.debug(f"question: cannot clear an older card ({error.message})")

    def _question_modal(self, interaction_id: str, token: str, ticket: str,
                        field: dict) -> None:
        label = text.shorten(field["title"] or "Answer", 45)
        self.api.respond(interaction_id, token, {
            "type": 9,
            "data": {
                "custom_id": f"qm:{ticket}:{field['index']}",
                "title": label,
                "components": [{
                    "type": 1,
                    "components": [{
                        "type": 4,
                        "custom_id": "answer",
                        "label": label,
                        "style": 2,
                        "max_length": 4000,
                        "required": True,
                    }],
                }],
            },
        })

    def _modal(self, payload: dict, who: str) -> None:
        data = payload.get("data") or {}
        parts = str(data.get("custom_id") or "").split(":")
        if len(parts) != 3 or parts[0] != "qm":
            return
        _, ticket, field_index = parts
        interaction_id = payload["id"]
        token = payload["token"]
        if who != self.config.owner:
            self._answer_now(interaction_id, token,
                             "Only the bot's owner answers this question.", ephemeral=True)
            return
        info = self.state.form(ticket)
        if not info:
            self._answer_now(interaction_id, token,
                             "That question is old; it is no longer waiting.", ephemeral=True)
            return
        field = next((item for item in info["fields"]
                      if str(item["index"]) == field_index), None)
        if field is None:
            self._answer_now(interaction_id, token,
                             "That question is no longer waiting.", ephemeral=True)
            return
        typed = ""
        for row in data.get("components") or []:
            for component in row.get("components") or []:
                if component.get("custom_id") == "answer":
                    typed = str(component.get("value") or "").strip()
        if not typed:
            self._answer_now(interaction_id, token, "The answer is empty.", ephemeral=True)
            return
        value = ([piece.strip() for piece in typed.split(",") if piece.strip()]
                 if field["kind"] == "multiselect" else typed)
        self._question_answer(payload, ticket, field, value)

    # -- gateway events ---------------------------------------------------

    def on_event(self, event: str, data: dict) -> None:
        if event == "INTERACTION_CREATE":
            self.pool.submit(self.handle_interaction, data)

    def register_commands(self) -> None:
        """Put the command on the application itself.

        Global commands are the only kind that reach a user install (your own
        profile, DMs), and they work in servers too, so there is one place to
        register them and no per-server call at all.
        """
        try:
            existing = self.api.global_commands()
        except DiscordError as error:
            log.error(f"commands: cannot read them ({error.message})")
            return
        mine = commands.spec(self.config.install)
        wanted = _strip(mine[0])
        others = [command for command in existing if command.get("name") != "vigo"]
        found = next((command for command in existing if command.get("name") == "vigo"), None)
        if found is not None and _strip(found) == wanted and len(existing) == len(others) + 1:
            log.debug("commands: Discord already has them")
            return
        try:
            self.api.put_global_commands(others + mine)
            log.info(f"commands: registered for {self.config.install} install")
        except DiscordError as error:
            if error.status == 403:
                log.error(
                    f"commands: Discord refuses them ({error.message}); the bot needs the "
                    "applications.commands scope: "
                    f"https://discord.com/oauth2/authorize?client_id={self.config.app_id}"
                    "&integration_type=1&scope=applications.commands"
                )
            else:
                log.error(f"commands: cannot register them ({error.message})")

    # -- interactions -----------------------------------------------------

    def handle_interaction(self, payload: dict) -> None:
        token = str(payload.get("token") or "")
        interaction_id = str(payload.get("id") or "")
        kind = payload.get("type")

        member = payload.get("member") or {}
        user = member.get("user") or payload.get("user") or {}
        who = str(user.get("id") or "")
        if not who or not token:
            return

        if kind == 3:
            self._component(payload, who)
            return
        if kind == 4:
            self._autocomplete(payload, who)
            return
        if kind == 5:
            self._modal(payload, who)
            return
        if kind != 2:
            return

        data = payload.get("data") or {}
        sub, options = commands.flatten(data)
        channel_id = str(payload.get("channel_id") or "")
        channel_name = str((payload.get("channel") or {}).get("name") or "")
        owner = who == self.config.owner
        approved = owner or self.allow.may_ask(who)

        if sub == commands.HELP:
            self._answer_now(
                interaction_id, token, self._help(owner, approved),
                ephemeral=not approved or bool(options.get("hidden")),
            )
            return
        if not approved:
            self._answer_now(
                interaction_id,
                token,
                "Only this bot's owner and the people it approved may use it. "
                "`/vigo help` says what it does.",
                ephemeral=True,
            )
            log.info(f"denied {sub or 'a use'} from {who}")
            return
        if sub in (commands.STOP, commands.UPGRADE, commands.STATUS, commands.ALLOW,
                   commands.DENY, commands.ALLOWED, commands.DEVICES) and not owner:
            self._answer_now(
                interaction_id, token,
                f"Only this bot's owner can use `/vigo {sub}`.",
                ephemeral=True,
            )
            return
        if sub not in (
            commands.ASK, commands.QUICK, commands.GOAL, commands.UPGRADE,
            commands.STOP, commands.STATUS, commands.DEVICES, commands.ALLOW,
            commands.DENY, commands.ALLOWED,
        ):
            self._answer_now(interaction_id, token, "`/vigo help` lists what it does.", ephemeral=True)
            return

        pick = None
        explicit = None
        if sub in (commands.ASK, commands.QUICK):
            explicit = (options.get("device") or "").strip() or None
            if explicit and explicit not in self.config.devices:
                self._answer_now(
                    interaction_id, token,
                    f"No device is called `{explicit}`.",
                    ephemeral=True,
                )
                return

        # Acknowledge before touching a device: Discord gives three seconds, and
        # a probe does not fit in them.  Two daemons both try to acknowledge and
        # Discord takes the first; the other is told it was already answered and
        # stops.  Whichever wins can drive every device, so it does not matter
        # which one it is.
        if not self._defer(interaction_id, token, ephemeral=self._ephemeral(sub, options)):
            return

        # Say it was received before probing a device: a fresh probe can take
        # seconds, and the person should not wait on a silent "thinking" line.
        if sub in (commands.ASK, commands.QUICK):
            self._say(token, "Got it, working on it. I'll follow up here.")

        pick = self.router.pick(explicit)
        try:
            self._answer(sub, options, token, channel_id, channel_name, who, owner, pick)
        except Exception as error:  # an answer always reaches the person
            log.error(f"command {sub}: {error}")
            self._say(token, "Something broke. The log says what.")

    def _autocomplete(self, payload: dict, who: str) -> None:
        """Offer the device names as they are typed, to whoever may ask."""
        data = payload.get("data") or {}
        focused = None
        for sub in data.get("options") or []:
            for option in sub.get("options") or []:
                if option.get("focused"):
                    focused = option
        choices: list[dict] = []
        if focused and focused.get("name") == "device":
            try:
                allowed = who == self.config.owner or self.allow.may_ask(who)
            except Exception:  # a slow read must not stall the three seconds
                allowed = who == self.config.owner
            if allowed:
                typed = str(focused.get("value") or "").strip().lower()
                choices = [
                    {"name": name, "value": name}
                    for name in self.config.devices
                    if typed in name.lower()
                ][:25]
        self.api.respond(payload["id"], payload["token"],
                         {"type": 8, "data": {"choices": choices}})

    def _ephemeral(self, sub: str, options: dict) -> bool:
        """Everyone sees the answer unless the asker asked to hide it.

        `devices` names the machines, `status` names the session and its cost,
        and the three allowlist commands name other people, so those are always
        private: no option could show them to a channel by accident.
        """
        if sub in (commands.DEVICES, commands.STATUS, commands.UPGRADE, commands.ALLOW,
                   commands.DENY, commands.ALLOWED):
            return True
        return bool(options.get("hidden"))

    def _defer(self, interaction_id: str, token: str, ephemeral: bool) -> bool:
        payload: dict = {"type": 5}
        if ephemeral:
            payload["data"] = {"flags": 64}
        reply = self.api.respond(interaction_id, token, payload)
        if reply.ok:
            return True
        code = reply.body.get("code") if isinstance(reply.body, dict) else None
        if code == 40060:
            log.debug("interaction already answered by another daemon")
        else:
            log.error(f"interaction: not acknowledged ({reply.status} {code})")
        return False

    def _answer_now(self, interaction_id: str, token: str, content: str, ephemeral: bool) -> None:
        payload: dict = {
            "type": 4,
            "data": {"content": content, "allowed_mentions": {"parse": []}},
        }
        if ephemeral:
            payload["data"]["flags"] = 64
        reply = self.api.respond(interaction_id, token, payload)
        if not reply.ok:
            log.debug("interaction already answered")

    def _say(self, token: str, content: str) -> bool:
        try:
            self.api.edit_original(token, {
                "content": content, "allowed_mentions": {"parse": []},
            })
            return True
        except DiscordError as error:
            log.error(f"cannot say it: {error.message}")
            return False

    # -- the commands -----------------------------------------------------

    def _answer(self, sub: str, options: dict, token: str, channel_id: str,
                channel_name: str, who: str, owner: bool, pick) -> None:
        if sub == commands.ASK:
            self._ask(options, token, channel_id, channel_name, who, pick)
        elif sub == commands.QUICK:
            self._quick(options, token, channel_id, pick)
        elif sub == commands.GOAL:
            self._goal(options, token, channel_id, channel_name, pick)
        elif sub == commands.STATUS:
            self._say(token, self._status(channel_id))
        elif sub == commands.DEVICES:
            self._say(token, self._devices(pick, detailed=True))
        elif sub == commands.UPGRADE:
            self._upgrade(options, token)
        elif sub == commands.STOP:
            self._say(token, self._stop(channel_id))
        elif sub == commands.ALLOW:
            self._say(token, self._allow(options))
        elif sub == commands.DENY:
            self._say(token, self._deny(options))
        elif sub == commands.ALLOWED:
            self._say(token, self.allow.describe())

    def _ask(self, options: dict, token: str, channel_id: str, channel_name: str,
             who: str, pick) -> None:
        prompt = str(options.get("prompt") or "").strip()
        if not prompt:
            self._say(token, "The question is empty.")
            return
        if pick.name is None:
            log.info(f"ask: no device ({pick.reason})")
            self._say(
                token,
                "No device would take the question.\n"
                "Start OpenCode there with `opencode serve --hostname 0.0.0.0 --port 4096`; "
                "`/vigo devices` (only you see it) says what is there.",
            )
            return
        if not self.run_for(channel_id) and self._device_busy(
                channel_id, pick.name, new=bool(options.get("new"))):
            # Nothing runs here, yet the device still works: another daemon
            # answered the last command, or this one restarted under it.  The
            # question cannot wait for a run this daemon does not hold, and a
            # prompt now would steer the agent that is already working.
            log.info("ask: the device is still working on this channel's last question")
            self._say(
                token,
                "The device is still working on the last question here. "
                "`/vigo status` shows it, `/vigo stop` ends it.",
            )
            return
        title = f"Discord #{channel_name}" if channel_name else "Discord DM"
        worker = asking.Ask(
            self,
            token=token,
            channel_id=channel_id,
            user_id=who,
            device_name=pick.name,
            prompt=prompt,
            new=bool(options.get("new")),
            title=title,
            hidden=bool(options.get("hidden")),
            goal=self._objective(options, prompt),
        )
        ahead = self.add_waiting(channel_id, worker)
        if ahead is None:
            log.info(f"ask: the line in this channel is full ({WAITING_MAX})")
            self._say(
                token,
                f"The line is full: {WAITING_MAX} questions wait in this channel. "
                "Wait for them, or `/vigo stop` clears them.",
            )
            return
        if ahead:
            log.info(f"ask: queued in this channel ({ahead} ahead)")
            self._say(
                token,
                f"Queued behind {ahead} question{'' if ahead == 1 else 's'}. "
                "I'll follow up here.",
            )
        self.start_next(channel_id)

    def _objective(self, options: dict, prompt: str) -> str:
        """The goal an ask becomes, or "" when it is a question.

        `goal:true` says this is work even though it reads as a question;
        `goal:false` says it is a question even though it reads as work.  Left
        out, the wording decides, and `vigo/goal.py` says how.
        """
        wanted = options.get("goal")
        if wanted is None:
            return prompt if goal.is_task(prompt) else ""
        return prompt if bool(wanted) else ""

    def _goal(self, options: dict, token: str, channel_id: str, channel_name: str,
              pick) -> None:
        """Set, pause, resume or clear the goal this channel's session holds."""
        objective = str(options.get("objective") or "").strip()
        if not objective:
            self._say(token, "The goal is empty.")
            return
        control = objective.lower() in GOAL_WORDS
        remembered = self.state.session_for(channel_id)
        if control and not remembered:
            self._say(token, "There is no goal in this channel yet.")
            return
        device = None
        if remembered:
            device = self.config.device_named(str(remembered.get("device") or ""))
            if device is None:
                self._say(token, f"`{remembered['device']}` is not in the file any more.")
                return
        elif pick.name is not None:
            device = self.config.device_named(pick.name)
        if device is None:
            log.info(f"goal: no device ({pick.reason})")
            self._say(
                token,
                "No device would take the goal.\n"
                "Start OpenCode there with `opencode serve --hostname 0.0.0.0 --port 4096`; "
                "`/vigo devices` (only you see it) says what is there.",
            )
            return
        if not control and (self.run_for(channel_id)
                            or self._device_busy(channel_id, device.name, new=False)):
            # A goal would steer the turn that is already running, and this
            # channel runs one thing at a time.  `pause` and `clear` are how
            # the person stops it, so those are never held back.
            log.info("goal: a question is running in this channel")
            self._say(
                token,
                "A question is running in this channel. `/vigo stop` ends it, or "
                "wait for it. `/vigo goal pause` and `/vigo goal clear` work now.",
            )
            return
        client = OpenCode(device.url, device.password)
        title = f"Discord #{channel_name}" if channel_name else "Discord DM"
        try:
            session = asking.open_session(self, client, device, channel_id, False, title)
            client.set_goal(session, objective)
        except OpenCodeError as error:
            log.info(f"goal: the device refused ({error.message})")
            self._say(token, "The device would not take the goal.")
            return
        log.info(f"goal: settled on {device.name}")
        said = {
            "pause": "The goal is paused.",
            "resume": "The goal is running again.",
            "clear": "The goal is cleared.",
        }.get(objective.lower())
        if said is None:
            said = "The goal is set: the agent works toward it until it is met."
        else:
            said += " `/vigo status` says what the device holds."
        self._say(token, said)

    def _quick(self, options: dict, token: str, channel_id: str, pick) -> None:
        """One question, answered once: no session, no tools, nothing kept."""
        prompt = str(options.get("prompt") or "").strip()
        if not prompt:
            self._say(token, "The question is empty.")
            return
        if pick.name is None:
            log.info(f"quick: no device ({pick.reason})")
            self._say(
                token,
                "No device would take the question.\n"
                "Start OpenCode there with `opencode serve --hostname 0.0.0.0 --port 4096`; "
                "`/vigo devices` (only you see it) says what is there.",
            )
            return
        device = self.config.device_named(pick.name)
        client = OpenCode(device.url, device.password)
        try:
            answer = client.generate_text(
                prompt, model=self.config.model.as_ref(), timeout=float(self.config.timeout)
            )
        except OpenCodeError as error:
            log.info(f"quick: the device refused ({error.message})")
            self._say(token, "The device would not answer.")
            return
        content = text.brief(answer) or "_nothing to say_"
        hidden = bool(options.get("hidden"))
        extra = {"flags": 64} if hidden else {}
        # The answer is its own message, after the "got it": a new message
        # notifies, an edit does not.  A hidden answer stays ephemeral.
        try:
            self.api.followup(token, {
                "content": content[:2000],
                "allowed_mentions": {"parse": []},
                **extra,
            })
        except DiscordError as error:
            log.error(f"quick: cannot post the answer ({error.message})")
            self._late_quick(channel_id, content[:2000], hidden)
            return
        log.info(f"quick: answered on {pick.name}")

    def _late_quick(self, channel_id: str, content: str, hidden: bool) -> None:
        """The interaction's 15 minutes ran out: post it where the question was."""
        if hidden:
            log.error("quick: the answer is too late for an ephemeral message")
            return
        try:
            self.api.send(channel_id, {
                "content": content[:2000],
                "allowed_mentions": {"parse": []},
            })
        except DiscordError as error:
            log.error(f"quick: cannot post the answer at all ({error.message})")

    def peer_password(self) -> str:
        """The password a peer sends to this device: this device's own.

        A peer looks this machine up as `devices[this].password`, so the server
        must check that same value — not the top-level fallback, which is empty
        when every device keeps its own secret.
        """
        own = self.config.device_named(self.config.device)
        return own.password if own else self.config.password

    @property
    def build(self) -> str:
        """The commit this daemon runs, short; empty when the checkout is not git.

        Read once, on the way up: the process runs the code it imported, so the
        value must not follow a later pull or commit in the checkout.
        """
        if self._build is None:
            self._build = build_of(self.checkout)
        return self._build

    def _same_build(self, info: dict) -> bool:
        """Whether a peer runs what this one runs.

        A peer on older code does not report a build, so fall back to the
        version string, which is all it can tell us.
        """
        theirs = str(info.get("build") or "")
        if theirs and self.build:
            return theirs == self.build
        return str(info.get("version") or "") == __version__

    def _peer_url(self, name: str) -> str | None:
        device = self.config.device_named(name)
        if device is None:
            return None
        if device.peer:
            return device.peer
        if not self.config.peer_port:
            return None
        host = device.url.split("//", 1)[1].split("/")[0].split(":")[0]
        return f"http://{host}:{self.config.peer_port}"

    def _peer_infos(self) -> dict[str, dict]:
        """What the other daemons say about themselves, when their peer port answers."""
        found: dict[str, dict] = {}
        for name in self.config.devices:
            if name == self.config.device:
                continue
            url = self._peer_url(name)
            if not url:
                continue
            device = self.config.device_named(name)
            try:
                found[name] = PeerClient(url, device.password, timeout=1.5).info()
            except OSError:
                continue  # that port is off or unreachable; say nothing
        return found

    def _builds(self, infos: dict[str, dict]) -> dict[str, str]:
        """One label per device: the vigo version it runs, and the commit when known."""
        labels: dict[str, str] = {}
        for name in self.config.devices:
            if name == self.config.device:
                version, build = __version__, self.build
            else:
                info = infos.get(name)
                if not info:
                    continue
                version = str(info.get("version") or "")
                build = str(info.get("build") or "")
            if version:
                labels[name] = f"vigo {version}" + (f" ({build})" if build else "")
        return labels

    def _behind(self, infos: dict[str, dict] | None = None) -> list[str]:
        """Daemons that answer on their peer port with a different build."""
        seen = self._peer_infos() if infos is None else infos
        lines = []
        for name, info in seen.items():
            if not self._same_build(info):
                theirs = str(info.get("build") or info.get("version") or "?")
                lines.append(
                    f"`{name}` runs {theirs}, not {self.build or __version__}. Use `/vigo upgrade`."
                )
        return lines

    def _upgrade(self, options: dict, token: str) -> None:
        """Pull one daemon, or every one behind this build, and restart it."""
        wanted = str(options.get("device") or "").strip()
        if wanted and wanted not in self.config.devices:
            self._say(token, f"No device is called `{wanted}`.")
            return
        targets = [wanted] if wanted else [
            name for name in self.config.devices if name != self.config.device
        ]
        lines = []
        moved: list[str] = []   # what the owner is told, once, in a DM
        local = False
        for name in targets:
            if name == self.config.device:
                lines.append(f"`{name}`: this one pulls after this answer")
                moved.append(f"`{name}` (this one): pulls now and restarts itself")
                local = True
                continue
            url = self._peer_url(name)
            if not url:
                lines.append(f"`{name}`: no `peerPort` to ask on")
                continue
            device = self.config.device_named(name)
            try:
                info = PeerClient(url, device.password, timeout=2.0).info()
            except OSError:
                lines.append(f"`{name}`: not answering on its peer port")
                continue
            if self._same_build(info):
                theirs = str(info.get("build") or info.get("version") or __version__)
                lines.append(f"`{name}`: already on {theirs}")
                continue
            was = str(info.get("build") or "?")
            try:
                told = PeerClient(url, device.password, timeout=180.0).update()
            except OSError as error:
                lines.append(f"`{name}`: could not update it ({error})")
                continue
            if not told.get("ok"):
                lines.append(f"`{name}`: git refused ({told.get('said')})")
                continue
            now = str(told.get("after") or told.get("before") or "?")
            note = "restarted" if told.get("restarted") else "restart it there"
            lines.append(f"`{name}`: pulled and {note}")
            moved.append(f"`{name}`: {was} → {now} · {note}")
        self._say(token, "\n".join(lines) or "Nothing to upgrade.")
        if moved:
            self._tell_owner("**Vigo updated**\n" + "\n".join(moved))
        # This one last, after the answer is out: pulling is safe, restarting
        # is only safe once the person has read the list above.
        if local:
            threading.Timer(1.0, lambda: update_self(self.checkout)).start()

    def _tell_owner(self, content: str) -> None:
        """A DM to the owner: the update that happened, or that it failed."""
        try:
            send_owner(self.config, content, api=self.api)
        except DiscordError as error:
            log.error(f"dm: cannot reach the owner ({error.message})")

    def _status(self, channel_id: str) -> str:
        remembered = self.state.session_for(channel_id)
        running = self.run_for(channel_id)
        device = None
        info: dict = {}
        lines: list[str] = []
        if remembered:
            device = self.config.device_named(remembered["device"])
            lines = [
                "**This channel** has an OpenCode session",
                f"“{remembered.get('title') or 'untitled'}”",
            ]
            if device:
                client = OpenCode(device.url, device.password)
                try:
                    info = client.session(remembered["session"])
                    tokens = info.get("tokens") or {}
                    lines.append(
                        f"{text.tokens(int(tokens.get('input', 0)))} in / "
                        f"{text.tokens(int(tokens.get('output', 0)))} out · "
                        f"{text.money(float(info.get('cost') or 0))}"
                    )
                except OpenCodeError as error:
                    log.info(f"status: the device does not answer ({error.message})")
                    lines.append("the device does not answer")
        else:
            lines = ["No question has been asked in this channel yet."]
        busy = busy_since(info)
        if running:
            elapsed = f"running for {text.duration(running.elapsed)}"
            if running.elapsed > self.config.timeout:
                elapsed += " · still running on the device"
            lines.append(elapsed)
        elif busy is not None:
            # No run here: another daemon answered the command that started
            # it, or this one restarted under it.  The device is still working,
            # so "idle" would be a lie it can be asked about.
            working = time.time() - busy / 1000.0
            lines.append(
                f"still running on `{device.name if device else 'the device'}` "
                f"for {text.duration(working)}. `/vigo stop` ends it"
            )
        else:
            lines.append("idle")
        goal_line = self._goal_text(device, remembered)
        if goal_line:
            lines.append(goal_line)
        line = self._line_text(channel_id)
        if line:
            lines.append("")
            lines.extend(line)
        return "\n".join(lines)

    def _goal_text(self, device, remembered: dict | None) -> str:
        """What the device says about this channel's goal, or "" for none.

        A goal is the device's, not this daemon's, so it is read from there:
        paused is a state the person must see, because a paused goal that says
        it is running would be a promise nothing keeps.
        """
        if not remembered or device is None:
            return ""
        try:
            held = OpenCode(device.url, device.password).goal(remembered["session"])
        except OpenCodeError as error:
            log.info(f"status: cannot read the goal ({error.message})")
            return "The device does not answer about its goal"
        if held is None:
            return ""
        if not held:
            return "The device did not say what its goal is"
        status = str(held.get("status") or "")
        objective = text.shorten(str(held.get("objective") or ""), 80)
        reason = str(held.get("reason") or "")
        said = f"**Goal** ({status}): “{objective}”"
        if reason:
            said += f" · {reason}"
        return said

    def _line_text(self, channel_id: str) -> list[str]:
        """The next three questions and the last three that ran, for status."""
        with self.runs_lock:
            waiting = list(self.waiting.get(channel_id) or [])
            finished = list(self.finished.get(channel_id) or [])
        lines: list[str] = []
        if waiting:
            lines.append(f"**Next up** ({len(waiting)} waiting):")
            for number, worker in enumerate(waiting[:3], 1):
                lines.append(f"{number}. “{text.shorten(worker.prompt, 80)}”")
        if finished:
            lines.append("**Finished:**")
            lines.extend(f"“{text.shorten(prompt, 80)}”" for prompt in finished[-3:])
        return lines

    def _devices(self, pick, detailed: bool = False) -> str:
        states = self.router.states(fresh=True)
        infos = self._peer_infos() if detailed else {}
        # Read the checkout every time.  A commit moves it without a restart,
        # so a value held from the first command would name the commit this
        # daemon started on, not the newest one in the checkout.
        latest = head_of(self.checkout) if detailed else ""
        lines = [self.router.describe(
            states, detailed=detailed, builds=self._builds(infos) if detailed else None,
        )]
        behind = self._behind(infos) if detailed else []
        if behind:
            lines.append("")
            lines.extend(behind)
        if latest:
            lines.append("")
            lines.append(f"latest commit: {latest}")
            short = latest.split(" ", 1)[0]
            if self.build and short != self.build:
                lines.append(f"this daemon runs {self.build}. Restart it to run {short}.")
        lines.append("")
        if pick.name:
            lines.append(
                f"A question would go to `{pick.name}`"
                + (f", {pick.reason}." if detailed else ".")
            )
        elif detailed:
            lines.append(f"No device would take it: {pick.reason}.")
        else:
            lines.append("No device would take it.")
        return "\n".join(lines)

    def _stop(self, channel_id: str) -> str:
        # One lock for the run and the line: a run that ends here must not
        # start the next question while this command drops it.
        with self.runs_lock:
            run = self.runs.get(channel_id)
            dropped = len(self.waiting.pop(channel_id, []))
            if run:
                run.stop.set()
        note = f" Dropped {dropped} queued question{'s' if dropped != 1 else ''}." if dropped else ""
        # The session to stop is the one the run is on; with no run here it is
        # the newest this channel has, which is where another daemon works.
        remembered = (self.state.session_on(channel_id, run.device) if run else None) \
            or self.state.session_for(channel_id)
        if not remembered:
            said = "Nothing is running in this channel." if not run else "Stopping it."
            return said + note
        device = self.config.device_named(remembered["device"])
        if device is None:
            return f"`{remembered['device']}` is not in the file any more." + note
        client = OpenCode(device.url, device.password)
        try:
            client.interrupt(remembered["session"])
            return "Stopped." + note
        except OpenCodeError as error:
            if run:
                return "Stopping the run in this channel." + note
            log.info(f"stop: nothing to stop ({error.message})")
            return "Nothing to stop." + note

    def _allow(self, options: dict) -> str:
        user = options.get("user")
        target = commands.user_id(user)
        if not target:
            return "Name the person to allow."
        chosen = str(options.get("for") or "1d")
        if chosen not in commands.SECONDS:
            return f"`{chosen}` is not one of the times."
        try:
            return self.allow.allow(target, commands.user_name(user), commands.SECONDS[chosen])
        except AllowError as error:
            return f"{error}"

    def _deny(self, options: dict) -> str:
        user = options.get("user")
        target = commands.user_id(user)
        if not target:
            return "Name the person."
        try:
            _done, said = self.allow.deny(target)
            return said
        except AllowError as error:
            return f"{error}"

    def _help(self, owner: bool, approved: bool) -> str:
        lines = [
            "**Vigo**, an OpenCode agent on your own machines, reached from Discord.",
            "",
            "`/vigo ask prompt:…` asks the device you last worked on. "
            "`device:` names one, `new:true` starts a fresh session. A question "
            "asked while one runs waits its turn.",
            "When the ask is a task, the agent gets a goal and works toward it "
            "until it is done; `goal:true` or `goal:false` says which it is.",
            "`/vigo goal objective:…` sets that goal yourself, with no question; "
            "`pause`, `resume` and `clear` control the one this channel has.",
            "`/vigo quick prompt:…` answers once, with no session and no tools.",
            "`/vigo devices` shows what is up and where a question would go.",
            "`/vigo status` shows this channel's session, what waits in it, and "
            "what ran. `/vigo stop` ends its run and drops what waits.",
            "Every command that answers a question takes `hidden:true` to keep "
            "its answer to yourself. Without it, the channel sees it. "
            "`devices`, `allow`, `deny` and `allowed` are always private.",
        ]
        if owner:
            lines.append("")
            lines.append(
                "Yours: `/vigo allow user:<who> for:<time>`, `/vigo deny user:<who>`, "
                "`/vigo allowed`, `/vigo upgrade`. Tool permissions come to your DMs as buttons."
            )
        elif not approved:
            lines.append("")
            lines.append("Only the bot's owner may ask.")
        return "\n".join(lines)

    # -- running ----------------------------------------------------------

    def run(self) -> None:
        url = self.api.gateway_url()
        log.info(f"up: {self.config.device}, {len(self.config.devices)} devices, model {self.config.model}")
        self.register_commands()
        if self.config.peer_port:
            try:
                PeerServer(self.config.peer_host, self.config.peer_port,
                           self.config.device, self.peer_password(),
                           checkout=self.checkout, build=self.build).start()
            except OSError as error:  # the tailnet is not up, or the port is taken
                log.error(f"peer: not listening ({error})")
        gateway = Gateway(url, self.config.token, INTENTS, self.on_event,
                          lambda state: log.debug(f"gateway: {state}"))
        gateway.run()


def info_request(info: dict) -> str:
    return f"{info['device']} · {info['request']}"
