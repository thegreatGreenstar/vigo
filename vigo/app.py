"""The bot: one gateway connection, one worker pool, one answer per question."""

from __future__ import annotations

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ask as asking
from . import commands, goal, lease as leasing, log, presence as showing, text, usage
from .allowlist import AllowError, AllowList
from .config import Config, Device
from .discord_api import Discord, DiscordError
from .gateway import Gateway
from . import __version__
from .notify import send_dm, send_owner
from .opencode_api import OpenCode, OpenCodeError, agents_on, busy_since
from .peer import PeerClient, PeerServer, build_of, built_of, head_of, update_self
from .router import DeviceState, Pick, Router
from .state import State

# Interactions arrive without intents. Direct messages are read only so that a
# reply to one of this bot's messages can be the next question: content is
# delivered in a DM without the privileged intent, so this is the only one
# asked for, and the text is never kept.
DIRECT_MESSAGES = 1 << 12
INTENTS = DIRECT_MESSAGES

WAITING_MAX = 10   # questions a channel may hold behind the one that runs
FINISHED_MAX = 10  # the last questions that ran, which `/vigo status` can name
AGENTS_MAX = 10    # agents at work on the plan at once, across every device
GOAL_WORDS = ("pause", "resume", "clear")  # `/vigo goal` control, not objectives
PEER_BUILT = 5.0   # seconds a peer's commit time is trusted before it is read again


def describe_prompt(prompt: str) -> str:
    """The instruction that asks a device for one short phrase about the work."""
    return (
        "In one short phrase of at most eight words, say what this request is "
        "about, starting with a verb that ends in -ing, with no quotes and no "
        f"full stop:\n\n{prompt}"
    )


def guest_refused(reason: str) -> str:
    """What an approved person is told when the plan's share says no.

    It names the limit and what ends it, and nothing of the machine: no
    device, no path, no id, and no number of the owner's plan beyond the one
    that explains the wait.
    """
    if reason == "limited":
        return ("The owner's plan is at its usage limit right now, so approved "
                "questions wait. Ask again when it resets.")
    if reason == "share":
        return ("Approved questions have used the share of the owner's plan that "
                "is kept for them. Ask again when the plan's window resets.")
    if reason in ("hourly", "user-hourly"):
        return "Approved questions have used their allowance for this hour. Ask again soon."
    if reason in ("daily", "user-daily"):
        return "Approved questions have used their allowance for today. Ask again tomorrow."
    if reason == "paused":
        return "The owner has paused questions from approved people."
    return ("The plan's usage cannot be read right now, so approved questions wait. "
            "Ask the owner to check the machine.")


def ask_note(owner: bool, hidden: bool, where: str, private: bool = False) -> str:
    """What the device is told of who asked and who will read the answer.

    The device sees no Discord, so without this line an answer written for one
    person can land in a channel where everyone reads it, with nothing saying
    that is the wrong place for a key or for someone else's details.  An
    answer only the asker sees says nothing more: the person asking may see
    what they asked about.  A group DM is not one of those: everyone in it
    reads the answer, and the line says so.
    """
    who = "the owner" if owner else "someone the owner allowed"
    if hidden or private:
        return f"[Discord] {who} asked this, and only the asker sees the answer."
    place = f"#{where}" if where else "this conversation"
    return (
        f"[Discord] {who} asked this, and everyone in {place} sees the answer. "
        "Keep credentials, keys and other people's private details out of it."
    )


def _first_file(items: object) -> dict | None:
    """The first file on a message: what a question about it needs.

    A message can carry several; a log and its screenshot are one thing to
    read, and the first is the one the person meant.
    """
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("url"):
            return item
    return None


def _attached(prompt: str, attached: object) -> str:
    """The question with the file the person uploaded, and how to reach it.

    A file is never in the question: Discord gives a link, signed and short
    lived, and the agent fetches it on the device it works on.  A log that a
    person drops is often megabytes, so the instruction says how a log is read
    instead of inviting a `cat`.  The link expires and stays out of the answer.
    """
    if not isinstance(attached, dict):
        return prompt
    url = str(attached.get("url") or "")
    if not url:
        return prompt
    name = text.shorten(str(attached.get("filename") or "a file"), 60)
    size = attached.get("size")
    said = f"The question has a file: “{name}”"
    if isinstance(size, int) and size >= 1024:
        said += f", {size // 1024} KB"
    said += (
        f". Fetch it with `curl -L -o \"$(mktemp)\" \"{url}\"`, then read it the way a "
        "log is read: `wc -l` first, then `grep -n` for the error, the address or the "
        "service, and `head` and `tail` around the hit. Never `cat` a file whole. The link "
        "expires, and it is not part of the answer."
    )
    return f"{prompt}\n\n{said}" if prompt.strip() else said


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


def _holds_goal(client: OpenCode, session: str) -> bool | None:
    """Whether that session already holds a goal, or None when the device will not say.

    A goal the channel already has is steered, never doubled: the session that
    holds it keeps the work, and a second session would leave the first one
    running with nobody watching it.
    """
    try:
        held = client.goal(session)
    except OpenCodeError as error:
        log.info(f"goal: cannot read the goal ({error.message})")
        return None
    if held is None:
        return False  # the session has no goal
    if not held:
        return None  # an empty answer is a device that did not say
    return True


def _answer_text(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "(nothing)"
    return str(value)


def _inline(value: object, size: int) -> str:
    """One line of a card's own text, with the markdown backticks removed.

    The action and the paths are the model's words.  A backtick in them would
    end the code span around them and let the rest style the owner's card,
    which is the one place an approval click happens.
    """
    return text.shorten(" ".join(str(value).split()), size).replace("`", "'")


class App:
    def __init__(self, config: Config, api: Discord | None = None, state: State | None = None,
                 checkout: Path | None = None) -> None:
        self.checkout = checkout
        self._build: str | None = None
        self._built: int | None = None
        self._peer_built_at: float | None = None
        self._peer_built_newer = False
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
        # The status Discord shows, and the handlers that are still writing
        # an answer.  A quick answer or a status read is work too, and it is
        # not in `runs`: the counter makes it visible to the presence.
        self.presence = showing.Presence(self._show_presence)
        self.gateway: Gateway | None = None
        self.in_flight = 0
        self.in_flight_lock = threading.Lock()
        self.busy_at = 0.0        # when the servers' busy sessions were read
        self.busy_up = 0          # how many answered then
        self.busy_working = False  # whether this daemon was working then
        self.busy_servers = 0     # how many of those had a session at work
        # A DM reply is read for the next question, and a group DM has more
        # readers than the asker: its kind is looked up once and kept briefly.
        self.channel_lock = threading.Lock()
        self.channel_kinds: dict[str, tuple[float, bool]] = {}

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
            self.presence.touch()
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

    def _suspend_runs(self) -> None:
        """Write down the work the upgrade's restart will cut off.

        This runs in the second before the supervisor takes the process down,
        and it is the only place a run is carried over a start: a crash or a
        stop ends its runs for good, and a question that came back hours later
        would answer a channel that has moved on.

        Only a question whose prompt is already on the device is carried, with
        the session it went to and the marks that name its turn.  One that
        waits, or that the device has not taken yet, keeps nothing: it never
        ran, and the state file keeps no message text.
        """
        with self.runs_lock:
            running = dict(self.runs)
        carried: dict[str, dict] = {}
        for channel, run in running.items():
            worker = run.ask
            if worker is None or not worker.prompted_at or not run.session:
                continue
            carried[channel] = {"at": time.time(), "run": worker.record()}
        self.state.suspend_runs(carried)
        log.info(f"upgrade: {len(carried)} turn(s) in flight carry over the restart")

    def _resume_suspended(self) -> None:
        """Carry on the work that an upgrade's restart cut off, once.

        It is read and cleared in one step, so a run that is lost again — a
        crash of the new process — is lost as any other crash loses one, and
        nothing comes back twice.
        """
        carried = self.state.take_suspended()
        if not carried:
            return
        resumed = 0
        for channel, entry in carried.items():
            record = entry.get("run") if isinstance(entry, dict) else None
            worker = self._restore(channel, record) if isinstance(record, dict) else None
            if worker is None:
                continue
            with self.runs_lock:
                self.runs[channel] = worker.run
            threading.Thread(target=worker.run_it, name=f"ask-{channel}", daemon=True).start()
            resumed += 1
        log.info(f"resume: {resumed} turn(s) carried over the restart")

    def _restore(self, channel: str, record: dict) -> asking.Ask | None:
        """One carried-over turn, or None for one that cannot be picked up here."""
        device = str(record.get("device") or "")
        if device not in self.config.devices:
            log.info("resume: a turn on a device that is not in the file was dropped")
            return None
        session = str(record.get("session") or "")
        try:
            prompted_at = float(record.get("prompted_at") or 0.0)
        except (TypeError, ValueError):
            log.info("resume: a carried-over turn names no time; it is dropped")
            return None
        if not session or not prompted_at:
            return None
        remembered = self.state.session_on(channel, device) or {}
        title = (str(remembered.get("title") or "")
                 if str(remembered.get("session") or "") == session else "")
        worker = asking.Ask(
            self,
            token="",  # the interaction ended with the old process
            channel_id=channel,
            user_id=str(record.get("user_id") or ""),
            device_name=device,
            prompt="",
            new=False,
            title="",
            hidden=bool(record.get("hidden")),
            # Only whether it had one matters: the goal itself is read from
            # the device, which kept it across the restart.
            goal="resumed" if record.get("goal") else "",
            note="",
            resume=asking.Resume(session, prompted_at, str(record.get("before") or "")),
        )
        worker.task_title = title
        return worker

    def shutdown(self) -> None:
        """Stop every run and the worker pool; used on the way out and by tests."""
        self.presence.stopped = True
        with self.runs_lock:
            runs = list(self.runs.values())
            self.waiting.clear()  # a stopping run must not start the next one
        for run in runs:
            run.stop.set()
        self.pool.shutdown(wait=False)

    def run_for(self, channel: str) -> asking.Run | None:
        with self.runs_lock:
            return self.runs.get(channel)

    def _guest_busy(self, user_id: str) -> bool:
        """Whether an approved person already has a question running or waiting.

        The owner pays for every turn on the plan and every device holds one
        conversation for that person, so an approved asker gets one at a time.
        """
        with self.runs_lock:
            if any(run.user == user_id for run in self.runs.values()):
                return True
            return any(worker.user_id == user_id
                       for line in self.waiting.values() for worker in line)

    # -- the status Discord shows ----------------------------------------

    def _begin_work(self) -> None:
        with self.in_flight_lock:
            self.in_flight += 1

    def _end_work(self) -> None:
        """The answer is out: count the work as done, and stay online a while."""
        with self.in_flight_lock:
            self.in_flight -= 1
        self.presence.touch()

    def _working(self) -> bool:
        """Whether this daemon answers something now, or holds a line of work."""
        with self.in_flight_lock:
            if self.in_flight:
                return True
        with self.runs_lock:
            return bool(self.runs or self.waiting)

    def _show_presence(self, status: str) -> None:
        """Put the status on the gateway, which sends it again after a reconnect."""
        gateway = self.gateway
        if gateway is not None:
            gateway.set_presence(status)

    def _busy_servers(self, states: dict[str, DeviceState], up: int,
                      working: bool, now: float) -> int:
        """How many answering servers have a session at work.

        A listing of each server's sessions is the only record of a turn that
        another daemon started, and it is not free: the answer is kept for
        `BUSY_EVERY` seconds, and thrown away when the count of servers that
        answer changes or this daemon starts or stops thinking.  That last part
        is what turns the light red as soon as an ask runs.
        """
        if (up == self.busy_up and working == self.busy_working
                and now - self.busy_at < (showing.BUSY_WORKING if working
                                          else showing.BUSY_EVERY)):
            return self.busy_servers
        busy = 0
        for state in states.values():
            if not state.up:
                continue
            device = state.device
            try:
                sessions = OpenCode(
                    device.url, device.password, timeout=2.0,
                ).sessions(limit=100)
            except OpenCodeError as error:
                log.debug(f"presence: {device.name}: busy is not known ({error.message})")
                continue
            if any(busy_since(info) is not None for info in sessions):
                busy += 1
        self.busy_up = up
        self.busy_working = working
        self.busy_at = now
        self.busy_servers = busy
        return busy

    def _refresh_presence(self) -> None:
        """Look at the daemon and the servers, and show the fit status."""
        states = self.router.states()
        up = sum(1 for state in states.values() if state.up)
        working = self._working()
        last = self.presence.last
        now = self.presence.now()
        busy = 0
        if up and (working or last is None or now - last > showing.ONLINE_FOR):
            busy = self._busy_servers(states, up, working, now)
        self.presence.show(showing.status_for(
            working=working, up=up, busy=busy, last=last, now=now,
        ))

    def _watch_presence(self) -> None:
        """Keep the status in step, without letting a probe stop the bot."""
        while not self.presence.stopped:
            try:
                self._refresh_presence()
            except Exception as error:  # a status never takes the bot down
                log.debug(f"presence: {type(error).__name__}: {error}")
            self.presence.wait(showing.TICK)

    def _running_agents(self) -> int:
        """Agents at work on the plan: a busy session on any device is one.

        One subscription pays for both machines, so the count is over both: ten
        at once is the concurrency this keeps to.  A device that does not answer
        cannot be counted, and its work is the ask's problem, not this guard's.
        """
        total = 0
        for device in self.config.devices.values():
            try:
                sessions = OpenCode(device.url, device.password).sessions(limit=100)
            except OpenCodeError:
                continue
            total += sum(1 for info in sessions if busy_since(info) is not None)
        return total

    def _describe(self, prompt: str, device_name: str) -> str:
        """A short phrase for what was taken on, written by the device.

        The acknowledgement used to quote the question, which told a channel
        what it had just read.  A phrase written for the request says what vigo
        understood, in words that fit it, and differs as the work does.  A
        device that will not answer leaves the line saying only that the
        question arrived.
        """
        device = self.config.device_named(device_name)
        if device is None:
            return ""
        try:
            said = OpenCode(device.url, device.password).generate_text(
                describe_prompt(prompt), model=self.config.model.as_ref(), timeout=8.0,
            )
        except OpenCodeError as error:
            log.info(f"ack: the device did not describe it ({error.message})")
            return ""
        phrase = " ".join(str(said or "").split()).strip("\"'` ")
        phrase = phrase.rstrip(".").strip()
        if not phrase or len(phrase) > 90:
            return ""
        # The phrase is posted in the channel, so it gets the same check an
        # answer does: a model that echoes a key, a path or an address gets no
        # line at all, rather than a redacted one that still says a secret was
        # there.
        if text.scrub(phrase)[1] or not text.shareable(phrase, tuple(self.config.devices)):
            return ""
        return phrase[0].lower() + phrase[1:]

    def _ack(self, phrase: str) -> str:
        """The line that says the question arrived, naming the work when known."""
        return f"Got it, {phrase}. I'll follow up here." if phrase else "Got it. I'll follow up here."

    def _asked(self, phrase: str, ahead: int) -> str:
        """The line for a question that waits its turn, naming the work."""
        count = f"{ahead} question{'' if ahead == 1 else 's'}"
        if phrase:
            return f"Queued behind {count}: {phrase}."
        return f"Queued behind {count}. I'll follow up here."

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
                     title: str, fields: list[dict], channel: str,
                     guest: bool = False) -> bool:
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
        self.state.new_form(ticket, device, session, form, prepared, channel, guest)
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
        entry = self.state.form(ticket) or {}
        lines = ["**Question from the agent**"]
        if entry.get("guest"):
            # The owner answers for somebody else's question too, so the card
            # says whose work it is before the owner approves what it wants.
            lines.append("It came from an approved person's question.")
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
                       action: str, resources: list[str], channel: str,
                       owner: bool = True) -> bool:
        ticket = secrets.token_hex(4)
        self.state.new_ticket(ticket, device, session, request, channel, owner=owner)
        shown = "\n".join(f"> `{_inline(item, 180)}`" for item in resources if item) or "> (no path)"
        whose = "" if owner else " (for an approved person's question)"
        content = (
            f"**Permission needed** on `{device}`{whose}\n"
            f"The agent asks to run `{_inline(action, 120)}`:\n{shown}"
        )
        buttons = [{"type": 2, "style": 1, "label": "Allow once", "custom_id": f"pw:{ticket}:a"}]
        if owner:
            # A saved rule outlives the question that asked for it.  An
            # approved person's question may earn one use of a tool, never a
            # standing grant that the next question inherits.
            buttons.append({"type": 2, "style": 3, "label": "Always allow",
                            "custom_id": f"pw:{ticket}:w"})
        buttons.append({"type": 2, "style": 4, "label": "Deny",
                        "custom_id": f"pw:{ticket}:d"})
        components = [{"type": 1, "components": buttons}]
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
        if decision == "always" and not info.get("owner", False):
            self.api.respond(interaction_id, token, {
                "type": 4,
                "data": {
                    "content": ("An approved person's tool is allowed once, "
                                "not saved. Allow it once, or run the question yourself."),
                    "flags": 64,
                },
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

    def _question_answer(self, payload: dict, ticket: str, field: dict,
                         value: object) -> None:
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
        elif event == "MESSAGE_CREATE":
            # A reply to one of this bot's answers is a question. Everything
            # else is dropped in `handle_message` before its text is looked at.
            self.pool.submit(self.handle_message, data)

    def handle_message(self, data: dict) -> None:
        """A message, with the status kept in step around the answer."""
        self._begin_work()
        try:
            self._handle_message(data)
        finally:
            self._end_work()

    def _private_channel(self, channel_id: str) -> bool:
        """Whether this channel is a one-to-one DM, so only the asker reads it.

        Discord's channel object says what it is: type 1 is a DM with one
        person, type 3 is a group DM with several.  A reply in a group DM is
        read by everyone in it, so its answer is scrubbed like a channel
        answer.  A lookup that fails says no, because the safe side is the one
        that removes a key.
        """
        now = time.monotonic()
        with self.channel_lock:
            cached = self.channel_kinds.get(channel_id)
        if cached and now - cached[0] < 300.0:
            return cached[1]
        private = False
        try:
            kind = self.api.channel(channel_id).get("type")
            private = int(kind) == 1
        except (DiscordError, TypeError, ValueError) as error:
            log.debug(f"reply: cannot tell what this channel is ({error})")
        with self.channel_lock:
            self.channel_kinds[channel_id] = (now, private)
        return private

    def _handle_message(self, data: dict) -> None:
        """A reply to a message vigo posted in a DM is that person's next question.

        The answer to a question is a message, so replying to it carries the
        conversation on: the reply runs as an ask in the same DM session, and
        its answer is a message too, so a reply to that works in turn.

        Only direct messages are read, and only a reply to a message this
        daemon posted in this conversation is a question. A plain message and
        a reply to anybody else's message are ignored, and the text of either
        is never kept. A message from somebody who may not ask is refused in
        words.
        """
        if data.get("guild_id"):
            return  # a guild channel is never read
        author = data.get("author") or {}
        if author.get("bot"):
            return
        who = str(author.get("id") or "")
        reference = str((data.get("message_reference") or {}).get("message_id") or "")
        channel_id = str(data.get("channel_id") or "")
        prompt = str(data.get("content") or "").strip()
        attached = _first_file(data.get("attachments"))
        if not who or not reference or not channel_id or (not prompt and not attached):
            return  # a plain message is not a question
        try:
            allowed = who == self.config.owner or self.allow.may_ask(who)
        except Exception:  # a slow read must not grant anything
            allowed = who == self.config.owner
        if not allowed:
            log.info(f"denied a message from {who}")
            self._post(channel_id, "Only this bot's owner and the people it approved may use "
                                   "it. `/vigo help` says what it does.")
            return
        answered = self.state.answer_for(reference)
        if answered is None or str(answered.get("channel") or "") != channel_id:
            return  # not a message this daemon posted here
        pick = self.router.pick()
        if pick.name is None:
            log.info(f"reply: no device ({pick.reason})")
            self._post(channel_id, "No device would take the question. Start OpenCode there "
                                   "with `opencode serve --hostname 0.0.0.0 --port 4096`.")
            return
        private = self._private_channel(channel_id)
        session_key = channel_id if who == self.config.owner else f"{channel_id}:{who}"
        if not self.run_for(channel_id) and self._device_busy(session_key, pick.name, new=False):
            log.info("reply: the device is still working on this channel's last question")
            self._post(channel_id, "The device is still working on the last question here. "
                                   "`/vigo status` shows it, `/vigo stop` ends it."
                       if who == self.config.owner else
                       "The device is still working on your last question. Wait for its answer.")
            return
        if who != self.config.owner and self._guest_busy(who):
            log.info("reply: an approved user already has work in flight")
            self._post(channel_id, "You already have a question in flight. Wait for its "
                                   "answer, then reply to it with the next one.")
            return
        if who != self.config.owner:
            # A reply is a question too, so it meets the same share of the plan:
            # otherwise the cap only slows a guest down in the channel.
            verdict = usage.screen(self.config, who, pick.name or "")
            if not verdict.allowed:
                log.info(f"reply: the guest's plan share refuses this question ({verdict.reason})")
                self._post(channel_id, guest_refused(verdict.reason))
                return
        worker = asking.Ask(
            self,
            token="",
            channel_id=channel_id,
            user_id=who,
            device_name=pick.name,
            prompt=_attached(prompt, attached),
            new=False,
            title="Discord DM",
            reply_in_channel=True,
            private=private,
            note=ask_note(who == self.config.owner, False, "", private),
            session_key=session_key,
        )
        ahead = self.add_waiting(channel_id, worker)
        if ahead is None:
            log.info(f"reply: the line in this channel is full ({WAITING_MAX})")
            self._post(channel_id, f"The line is full: {WAITING_MAX} questions wait in this "
                                   "channel. Wait for them, or `/vigo stop` clears them.")
            return
        phrase = self._describe(prompt, pick.name)
        line = self._asked(phrase, ahead) if ahead else self._ack(phrase)
        if ahead:
            log.info(f"reply: queued in this channel ({ahead} ahead)")
        else:
            log.info("reply: a question from a reply")
        # The line is the question's own: a note about the run past `timeout`
        # goes at its end, so the run edits this message rather than posting a
        # second one beside it.
        worker.message_id = self._post(channel_id, line, record=True)
        worker.shown = line if worker.message_id else ""
        self.start_next(channel_id)

    def _post(self, channel_id: str, content: str, record: bool = False) -> str:
        """Say something where there is no interaction to edit.

        With `record`, a reply to it is the next question, and the daemon that
        posted it is the only one that will act on that reply.  The answer is
        the message's id, or "" when Discord would not take it, so the caller
        can edit that message later.
        """
        try:
            posted = self.api.send(channel_id, {
                "content": content[:2000],
                "allowed_mentions": {"parse": []},
            })
        except DiscordError as error:
            log.error(f"reply: cannot say it ({error.message})")
            return ""
        message_id = str(posted.get("id") or "") if isinstance(posted, dict) else ""
        if record and message_id:
            self.state.remember_answer(message_id, channel_id)
        return message_id

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
        mine = commands.spec(self.config.install, list(self.config.devices))
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
        """A command, with the status kept in step around the answer."""
        self._begin_work()
        try:
            self._handle_interaction(payload)
        finally:
            self._end_work()

    def _handle_interaction(self, payload: dict) -> None:
        token = str(payload.get("token") or "")
        interaction_id = str(payload.get("id") or "")
        kind = payload.get("type")
        # Discord keeps the token for 15 minutes, and the clock starts here,
        # not when a run of the queue gets its turn.
        asked_at = time.monotonic()

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
                ephemeral=not owner or bool(options.get("hidden")),
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
        if sub in (commands.STOP, commands.UPGRADE, commands.STATUS, commands.QUEUE,
                   commands.ALLOW, commands.DENY, commands.ALLOWED, commands.DEVICES,
                   commands.GOAL) and not owner:
            self._answer_now(
                interaction_id, token,
                f"Only this bot's owner can use `/vigo {sub}`.",
                ephemeral=True,
            )
            return
        if sub not in (
            commands.ASK, commands.QUICK, commands.GOAL, commands.UPGRADE,
            commands.STOP, commands.STATUS, commands.QUEUE, commands.DEVICES,
            commands.ALLOW, commands.DENY, commands.ALLOWED,
        ):
            self._answer_now(interaction_id, token, "`/vigo help` lists what it does.", ephemeral=True)
            return

        pick = None
        explicit = None
        if sub in (commands.ASK, commands.QUICK):
            explicit = (options.get("device") or "").strip() or None
            if explicit and not owner:
                self._answer_now(
                    interaction_id, token,
                    "Only this bot's owner chooses the device.",
                    ephemeral=True,
                )
                return
            if explicit and explicit not in self.config.devices:
                self._answer_now(
                    interaction_id, token,
                    f"No device is called `{explicit}`.",
                    ephemeral=True,
                )
                return

        # Acknowledge before touching a device: Discord gives three seconds, and
        # a probe does not fit in them.  Two daemons both try to acknowledge and
        # Discord takes the first.  For a question, the one whose checkout is
        # older stays out of the way here, before that, so the newer code
        # answers: it queues where the older one refuses, and it does not put a
        # second prompt into a session the other is already working.
        if sub in (commands.ASK, commands.QUICK, commands.GOAL) and self._peer_ahead():
            log.info(f"{sub}: left to the newer daemon")
            return
        if not self._defer(interaction_id, token, ephemeral=self._ephemeral(sub, options, owner)):
            return

        pick = self.router.pick(explicit)
        try:
            self._answer(sub, options, token, channel_id, channel_name, who, owner,
                         pick, asked_at)
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
            # An approved person does not choose the device: its name would be
            # told to them, and the owner decides where their question runs.
            if allowed and who == self.config.owner:
                typed = str(focused.get("value") or "").strip().lower()
                choices = [
                    {"name": name, "value": name}
                    for name in self.config.devices
                    if typed in name.lower()
                ][:25]
        self.api.respond(payload["id"], payload["token"],
                         {"type": 8, "data": {"choices": choices}})

    def _ephemeral(self, sub: str, options: dict, owner: bool = True) -> bool:
        """Who sees the reply: the asker alone, or the channel too.

        The owner's answer is public unless they hide it.  An approved
        asker's question is always private: the device holds the owner's
        files, and an answer about them must not land in a channel.  `devices`
        names the machines, `status` names the session and its cost, and the
        three allowlist commands name other people, so those are always
        private: no option could show them to a channel by accident.  `queue`
        is private too, unless the owner asks for the public form, which names
        no channel, no task and no device.
        """
        if sub == commands.QUEUE:
            return not bool(options.get("public"))
        if not owner:
            return True
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

    def _say(self, token: str, content: str) -> dict | None:
        """Put this text on the message Discord made for the interaction.

        Discord returns that message, and its id is what keeps the question's
        line current after the interaction's token expires: a question that
        waited in the channel's queue can pass the token's fifteen minutes
        before its run even starts.
        """
        try:
            return self.api.edit_original(token, {
                "content": content, "allowed_mentions": {"parse": []},
            })
        except DiscordError as error:
            log.error(f"cannot say it: {error.message}")
            return None

    # -- the commands -----------------------------------------------------

    def _answer(self, sub: str, options: dict, token: str, channel_id: str,
                channel_name: str, who: str, owner: bool, pick: Pick,
                asked_at: float) -> None:
        if sub == commands.ASK:
            self._ask(options, token, channel_id, channel_name, who, pick, asked_at)
        elif sub == commands.QUICK:
            self._quick(options, token, channel_id, pick, who)
        elif sub == commands.GOAL:
            self._goal(options, token, channel_id, channel_name, pick)
        elif sub == commands.STATUS:
            self._say(token, self._status(channel_id))
        elif sub == commands.QUEUE:
            self._say(token, self._queue_text(public=bool(options.get("public"))))
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
             who: str, pick: Pick, asked_at: float) -> None:
        prompt = str(options.get("prompt") or "").strip()
        if not prompt:
            self._say(token, "The question is empty.")
            return
        owner = who == self.config.owner
        # A fresh session is the owner's to ask for: an approved person's
        # questions continue in their own conversation, not a new one per ask.
        start_new = bool(options.get("new")) if owner else False
        # Work that runs on after the answer is the owner's: an approved
        # person's ask is one question in a session of their own, never a goal
        # with a subagent that keeps changing the machine.
        objective = self._objective(prompt) if owner else ""
        if not owner and self._guest_busy(who):
            # Each question is a turn the owner's plan pays for, and the
            # device holds one conversation per person.  One at a time keeps
            # a list of approved people from becoming a queue on the machine.
            log.info(f"ask: an approved user already has work in flight ({who})")
            self._say(
                token,
                "You already have a question in flight. Wait for its answer, "
                "then ask the next one.",
            )
            return
        if pick.name is None:
            log.info(f"ask: no device ({pick.reason})")
            self._say(
                token,
                "No device would take the question.\n"
                "Start OpenCode there with `opencode serve --hostname 0.0.0.0 --port 4096`; "
                "`/vigo devices` (only you see it) says what is there."
                if owner else
                "No device would take the question. Ask the owner to check the machine.",
            )
            return
        session_key = channel_id if owner else f"{channel_id}:{who}"
        if (not objective and not self.run_for(channel_id)
                and self._device_busy(session_key, pick.name, new=start_new)):
            # Nothing runs here, yet the device still works: another daemon
            # answered the last command, or this one restarted under it.  A
            # question would steer the agent that is already working, so it is
            # refused; a task is not, because it runs in a session of its own.
            log.info("ask: the device is still working on this channel's last question")
            self._say(
                token,
                "The device is still working on the last question here. "
                "`/vigo status` shows it, `/vigo stop` ends it."
                if owner else
                "The device is still working on your last question. Wait for its answer.",
            )
            return
        running = self._running_agents()
        if running >= AGENTS_MAX:
            # One OpenCode plan pays for both machines, so its limits are
            # shared: a concurrency past them buys a failed turn for every
            # agent, and no answer at all.
            log.info(f"ask: {running} agents already run on the plan; this one waits")
            self._say(
                token,
                f"Your OpenCode plan already runs {running} agents, the most vigo starts "
                f"at once ({AGENTS_MAX}, on every device it drives). `/vigo queue` shows "
                "what waits; ask again when one finishes."
                if owner else
                "The owner's OpenCode plan is busy with the most work it starts at once. "
                "Ask again when it finishes.",
            )
            return
        if not owner:
            # An approved person spends the owner's plan, so the plan's own
            # meters decide whether this question runs at all: a third of the
            # spent allowance, and an hourly and daily cap on the way there.
            # The check is here, before the question is queued or a session is
            # opened, so a refusal costs the plan nothing.
            verdict = usage.screen(self.config, who, pick.name or "")
            if not verdict.allowed:
                log.info(f"ask: the guest's plan share refuses this question ({verdict.reason})")
                self._say(token, guest_refused(verdict.reason))
                return
        title = f"Discord #{channel_name}" if channel_name else "Discord DM"
        # An approved person's answer is private whatever the option says: the
        # device holds the owner's files, and nothing about them belongs in a
        # channel.  Their session is their own, so no history is shared either.
        hidden = bool(options.get("hidden")) or not owner
        worker = asking.Ask(
            self,
            token=token,
            channel_id=channel_id,
            user_id=who,
            device_name=pick.name,
            prompt=_attached(prompt, options.get("file")),
            new=start_new,
            title=title,
            hidden=hidden,
            goal=objective,
            note=ask_note(owner, hidden, channel_name),
            asked_at=asked_at,
            session_key=session_key,
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
        # The acknowledgement goes out before the run starts.  Discord folds
        # the first follow-up after a deferral into the loading message, so an
        # answer that beat the acknowledgement would be overwritten by it: the
        # person would see "Got it" and never the answer.  The phrase costs a
        # second, and a lost answer costs the whole question.
        phrase = self._describe(prompt, pick.name)
        line = self._asked(phrase, ahead) if ahead else self._ack(phrase)
        if ahead:
            log.info(f"ask: queued in this channel ({ahead} ahead)")
        # The message is the question's own line: the run past `timeout` adds
        # its note to the end of it instead of posting a message beside it.
        # Discord returns that message, so its id keeps the line editable after
        # the interaction's token expires; when the edit does not land there is
        # no line to grow, and the note goes out on its own.
        posted = self._say(token, line)
        if posted is not None:
            worker.shown = line
            worker.message_id = str(posted.get("id") or "")
        self.start_next(channel_id)

    def _objective(self, prompt: str) -> str:
        """The goal an ask becomes, or "" when it is a question.

        No option says which it is, so the wording decides, and `vigo/goal.py`
        holds the rule.  When the wording is read wrong, the person says it
        with `/vigo goal`, which takes the goal directly rather than guessing.
        """
        return prompt if goal.is_task(prompt) else ""

    def _goal(self, options: dict, token: str, channel_id: str, channel_name: str,
              pick: Pick) -> None:
        """Set, pause, resume or clear the goal this channel has.

        A new objective is work of its own: it opens a session under the
        device's coding agent, so the channel's conversation keeps answering
        questions while the goal runs.  A channel that already holds a goal
        keeps that session, so a second objective steers the work it is doing
        instead of starting a second one, and `pause`, `resume` and `clear`
        reach the same session.
        """
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
        client = OpenCode(device.url, device.password)
        title = f"Discord #{channel_name}" if channel_name else "Discord DM"
        # The channel's session on this device, which the other daemon may have
        # made.  A channel that already holds a goal keeps that session, so a
        # second objective steers that work instead of starting another.
        session = str((remembered or {}).get("session") or "")
        adopted = False
        if not session:
            session = asking.channel_session(client, channel_id)
            adopted = bool(session)
        steer = False
        if session and not control:
            held = _holds_goal(client, session)
            if held is None:
                self._say(
                    token,
                    "The device would not say what goal this channel has, so "
                    "nothing changed.",
                )
                return
            steer = held
        if not control and (self.run_for(channel_id)
                            or self._device_busy(channel_id, device.name, new=False)):
            # A channel runs one thing at a time, and a goal is a turn of its
            # own: it waits for the question that is running.  `pause` and
            # `clear` are how the person stops it, so those never wait.
            log.info("goal: a question is running in this channel")
            self._say(
                token,
                "A question is running in this channel. `/vigo stop` ends it, or "
                "wait for it. `/vigo goal pause` and `/vigo goal clear` work now.",
            )
            return
        try:
            if session and leasing.held_by_other(client, session, self.config.device):
                # A goal starts a turn of its own, so it cannot land in one
                # another daemon is running. It takes no lease, because it
                # would have nothing to release it with.
                log.info("goal: another daemon holds this channel's session")
                self._say(
                    token,
                    "Another vigo is asking in this channel. "
                    "`/vigo status` shows it, and `/vigo stop` ends it.",
                )
                return
            if not control and not steer:
                # Work of its own, under the device's coding agent: the
                # conversation stays a conversation.  `open_session` remembers
                # it, so a follow-up and `/vigo status` reach the work.
                session = asking.open_session(
                    self, client, device, channel_id, False,
                    f"{title} · {text.subject(objective, 40)}",
                    agent=client.worker_agent(), worker=True,
                )
            elif adopted:
                self.state.remember_session(channel_id, device.name, session, title)
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
            said = ("The goal is set in a session of its own: the agent works "
                    "toward it until it is met.")
        else:
            said += " `/vigo status` says what the device holds."
        self._say(token, said)

    def _quick(self, options: dict, token: str, channel_id: str, pick: Pick,
               who: str) -> None:
        """One question, answered once: no session, no tools, nothing kept."""
        prompt = str(options.get("prompt") or "").strip()
        if not prompt:
            self._say(token, "The question is empty.")
            return
        if who != self.config.owner:
            # A one-off leaves no session and the device reports no tokens for
            # it, so a share of the plan cannot be counted against the person
            # who asked.  A question keeps the work in a session of its own,
            # where the accounting sees it.
            log.info("quick: an approved person asked for a one-off")
            self._say(
                token,
                "`/vigo quick` is the owner's: it leaves no session, so its use of "
                "the plan cannot be counted. Ask a question instead, and it runs in "
                "a session of your own.",
            )
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
        # No "got it" line: a one-off has nothing to follow up on, so the
        # deferred message stays as "thinking" and becomes the answer itself.
        # It is told how short to stay, as a question is: the channel reads it
        # too, and a one-off is the answer people read most often.
        try:
            answer = client.generate_text(
                f"{asking.SELF}\n\n{asking.BRIEF}\n\n{prompt}",
                model=self.config.model.as_ref(), timeout=float(self.config.timeout),
            )
        except OpenCodeError as error:
            log.info(f"quick: the device refused ({error.message})")
            self._say(token, "The device would not answer.")
            return
        hidden = bool(options.get("hidden")) or who != self.config.owner
        removed = ""
        if not hidden:
            answer, removed = text.scrub(answer)
        content = answer or "_nothing to say_"
        if removed:
            content += (
                f"\n\n🔒 a {removed} was removed from this answer; "
                "`hidden:true` shows the answer whole."
            )
        # Quick keeps no session, so a runaway answer cannot point anywhere but
        # here: it says it stopped.  The messages before it are the answer.
        parts = text.messages(
            content, asking.ANSWER_PARTS, more="… (the answer was longer than this)",
        ) or ["_nothing to say_"]
        extra = {"flags": 64} if hidden else {}
        # The first part goes into the message the command already deferred: it
        # is the one response, with no line before it.  Discord treats the next
        # followup as an edit of that message, so the first part must be in it
        # before a long answer continues.  A hidden answer stays ephemeral.
        try:
            self.api.edit_original(token, {
                "content": parts[0][:2000],
                "allowed_mentions": {"parse": []},
            })
        except DiscordError as error:
            # The usual cause is the interaction's 15 minutes, which a long
            # one-off can pass: the answer is delivered below, so this is not
            # an error.  A post that fails as well says so there.
            log.info(f"quick: the deferred message would not take the answer "
                     f"({error.message}); posting it where the question was")
            for part in parts:
                self._late_quick(channel_id, part, hidden, prompt, who)
            return
        for number, part in enumerate(parts[1:], start=1):
            try:
                self.api.followup(token, {
                    "content": part[:2000],
                    "allowed_mentions": {"parse": []},
                    **extra,
                })
            except DiscordError as error:
                log.info(f"quick: the interaction would not take the rest of the "
                         f"answer ({error.message}); posting it where the question was")
                for rest in parts[number:]:
                    self._late_quick(channel_id, rest, hidden, prompt, who)
                return
        log.info(f"quick: answered on {pick.name}")

    def _late_quick(self, channel_id: str, content: str, hidden: bool,
                    prompt: str, who: str) -> None:
        """Post a one-off where the question was, without the token.

        A public answer goes to the channel.  A hidden one has no ephemeral
        message left to go in, so a DM takes it, to the person who asked: the
        owner's DM would show them an approved person's answer.  The
        alternative is to drop it, which is what used to happen here.
        """
        if hidden:
            said = (f"**the answer to “{text.subject(prompt, 60)}”**\n\n"
                    f"{content}")
            try:
                send_dm(self.config, who, said, api=self.api)
                log.info("quick: the answer went to the asker's DM")
            except Exception as error:  # noqa: BLE001 - any failure means it did not arrive
                log.error(f"quick: the DM did not take the answer ({error})")
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

        A peer sends the file's `peerPassword` when it has one, otherwise the
        device password.  The server checks the same choice, so the OpenCode
        password the agent holds is not enough to restart the daemon.
        """
        if self.config.peer_password:
            return self.config.peer_password
        own = self.config.device_named(self.config.device)
        return own.password if own else self.config.password

    def _peer_secret(self, device: Device) -> str:
        return self.config.peer_password or device.password

    @property
    def build(self) -> str:
        """The commit this daemon runs, short; empty when the checkout is not git.

        Read once, on the way up: the process runs the code it imported, so the
        value must not follow a later pull or commit in the checkout.
        """
        if self._build is None:
            self._build = build_of(self.checkout)
        return self._build

    @property
    def built(self) -> int:
        """When the commit this daemon runs was made, in seconds; 0 when not git.

        Read once, like `build`, because the process keeps the code it
        imported.  Two checkouts cannot be ordered by their hashes, so this is
        what says which of them is the newer code.
        """
        if self._built is None:
            self._built = built_of(self.checkout)
        return self._built

    def _peer_ahead(self) -> bool:
        """Whether a peer runs a newer commit, which then takes every question.

        Both daemons receive every interaction and Discord accepts the first
        acknowledgement, so nothing in the protocol decides which of them
        answers.  Left alone, the older one can win, and it may refuse where
        the newer one queues, or send a prompt into a session the newer one's
        run is working on.  A daemon that sees a newer commit on a peer leaves
        the question to it, before the acknowledgement, so the work and the
        tokens happen once.

        The answer is kept for a few seconds: the read happens before the
        acknowledgement, and Discord allows three seconds for that.
        """
        now = time.monotonic()
        if self._peer_built_at is not None and now - self._peer_built_at < PEER_BUILT:
            return self._peer_built_newer
        newer = False
        mine = self.built
        if mine:
            for name in self.config.devices:
                if name == self.config.device:
                    continue
                url = self._peer_url(name)
                if not url:
                    continue
                device = self.config.device_named(name)
                try:
                    info = PeerClient(url, device.password, timeout=1.0).info()
                except OSError:
                    continue  # a peer that does not answer cannot take the use
                try:
                    theirs = int(info.get("built") or 0)
                except (TypeError, ValueError):
                    theirs = 0
                if theirs > mine:
                    log.debug(f"peer: {name} runs a newer commit; it takes this use")
                    newer = True
                    break
        self._peer_built_at = now
        self._peer_built_newer = newer
        return newer

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
                found[name] = PeerClient(url, self._peer_secret(device), timeout=1.5).info()
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
                info = PeerClient(url, self._peer_secret(device), timeout=2.0).info()
            except OSError:
                lines.append(f"`{name}`: not answering on its peer port")
                continue
            if self._same_build(info):
                theirs = str(info.get("build") or info.get("version") or __version__)
                lines.append(f"`{name}`: already on {theirs}")
                continue
            was = str(info.get("build") or "?")
            try:
                told = PeerClient(url, self._peer_secret(device), timeout=180.0).update()
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
            threading.Timer(1.0, lambda: update_self(
                self.checkout, before_restart=self._suspend_runs)).start()

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
                working = self._running_subagents(device, remembered["session"])
                if working:
                    named = ", ".join(f"“{text.shorten(title, 40)}”" for title in working[:3])
                    more = f", and {len(working) - 3} more" if len(working) > 3 else ""
                    count = f"{len(working)} subagent{'s' if len(working) != 1 else ''}"
                    lines.append(f"{count} at work: {named}{more}")
        else:
            lines = ["No question has been asked in this channel yet."]
        if device:
            agents = self._running_agents()
            if agents > 1:
                # The owner pays the plan's concurrency: say how many are at
                # work, so a burst is visible before it hits a limit.
                lines.append(f"{agents} agents are running on your devices")
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
        if self.config.guests.share > 0:
            # What approved people spend is the owner's to see: the plan is
            # the owner's subscription, and a refusal is understood before it
            # happens when the number is in front of them.
            try:
                lines.append(usage.summary(self.config))
            except usage.UsageError as error:
                log.info(f"status: the guest share cannot be read ({error})")
                lines.append("**Guests**: the plan's usage cannot be read")
        line = self._line_text(channel_id)
        if line:
            lines.append("")
            lines.extend(line)
        return "\n".join(lines)

    @staticmethod
    def _running_subagents(device: Device, session: str) -> list[str]:
        """The titles of the subagents at work under a session, its own excluded.

        A subagent is a session whose `parentID` names the session that
        delegated the work, and the device says which sessions run now.  A
        device that does not answer leaves the line out: a status that guesses
        would say a subagent works when nothing does.
        """
        if not session:
            return []
        client = OpenCode(device.url, device.password, timeout=2.0)
        try:
            active = client.active()
            listed = client.sessions(limit=100)
        except OpenCodeError as error:
            log.info(f"status: cannot read the subagents ({error.message})")
            return []
        return [str(info.get("title") or "untitled") for info in listed
                if info.get("parentID") == session
                and active.get(str(info.get("id") or "")) == "running"]

    def _goal_text(self, device: Device | None, remembered: dict | None) -> str:
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

    def queue_snapshot(self) -> list[dict]:
        """This daemon's channels that run or wait, with every task in line.

        Titles, not ids: the session's title names the channel the way the
        person knows it.  A task is its own text and nothing else of the person
        is here; the peer link carries this between daemons, and only the
        public form of the queue redacts it for Discord.

        Each entry says how many agents work on its run, because the queue
        that names a task should say what is on it.
        """
        with self.runs_lock:
            entries = [
                (channel, self.runs.get(channel), list(self.waiting.get(channel) or []))
                for channel in sorted(set(self.runs) | set(self.waiting))
            ]
        agents = self._agents_by_session(
            [(run.session, run.device) for _, run, _ in entries if run])
        found: list[dict] = []
        for channel, run, waiting in entries:
            remembered = self.state.session_for(channel) or {}
            found.append({
                "channel": channel,
                "title": str(remembered.get("title") or "untitled"),
                "running": round(run.elapsed, 1) if run else None,
                "waiting": [worker.prompt for worker in waiting],
                "agents": agents.get(run.session, 0) if run else 0,
            })
        return found

    def _agents_by_session(self, tasks: list[tuple[str, str]]) -> dict[str, int]:
        """How many agents work on each task, by the session the task runs in.

        `tasks` is (session id, device name) for each run.  One answer about
        what runs, and one listing of the sessions, says it for every task on a
        device: a subagent is a session of its own under the task's session.
        A device that does not answer leaves its counts out rather than wrong.
        """
        found: dict[str, int] = {}
        by_device: dict[str, list[str]] = {}
        for session, device_name in tasks:
            if session:
                by_device.setdefault(device_name, []).append(session)
        for device_name, session_ids in by_device.items():
            device = self.config.device_named(device_name)
            if device is None:
                continue
            client = OpenCode(device.url, device.password, timeout=2.0)
            try:
                active = client.active()
                listed = client.sessions(limit=100)
            except OpenCodeError as error:
                log.info(f"queue: the agent count is not known ({error.message})")
                continue
            for session in session_ids:
                found[session] = agents_on(active, listed, session)
        return found

    def _queue_text(self, public: bool = False) -> str:
        """Every channel that runs or waits, here and on the other daemons.

        A queue that this daemon cannot see is still a queue: a channel's line
        lives with whichever daemon acknowledged its questions, and saying that
        nothing waits would be the lie `/vigo status` used to tell.  A peer that
        does not answer is said to be silent, never left out.

        `public` is the form everyone in the channel may read: no channel's
        name, no device's name, and no task that names a person, a path or a
        machine.  It keeps the shape of the queue, which is what the owner
        shows when the work is the point and the people are not.
        """
        # (a device's name, its rows or None when it has none, and why) here first
        groups: list[tuple[str, list[dict] | None, str]] = [("", self.queue_snapshot(), "")]
        for name in self.config.devices:
            if name == self.config.device:
                continue
            url = self._peer_url(name)
            if not url:
                continue
            device = self.config.device_named(name)
            try:
                info = PeerClient(url, self._peer_secret(device), timeout=1.5).info()
            except OSError:
                groups.append((name, None, "does not answer on its peer port"))
                continue
            queue = info.get("queue")
            if not isinstance(queue, list):
                groups.append((name, None, "runs an older vigo, which reports no queue"))
                continue
            groups.append((name, queue, ""))
        if public:
            said = self._queue_public(groups)
            said = self._fit(said, more="… (the rest does not fit here)")
        else:
            said = self._fit("\n\n".join(
                self._queue_block(entries, name) if entries is not None
                else f"⚠️ `{name}` {why}"
                for name, entries, why in groups
            ))
        return said or "Nothing runs and nothing waits."

    @staticmethod
    def _fit(said: str, limit: int = 1800,
             more: str = "… (the rest does not fit here; `/vigo status` shows one channel's line)") -> str:
        """One message holds it, and what does not fit says where the rest is.

        A Discord edit refuses more than 2000 characters, and a queue view that
        the channel refuses says nothing at all.
        """
        if len(said) <= limit:
            return said
        cut = said[:limit]
        at = cut.rfind("\n")
        if at > limit // 2:
            cut = cut[:at]
        return cut + "\n" + more

    def _queue_public(self, groups: list[tuple[str, list[dict] | None, str]]) -> str:
        """The queue with nothing that names a channel, a device or a task.

        The rows of every daemon come in one list, because which daemon holds a
        line is not something a channel may read.  A task that names anything
        keeps its place as `redacted task`, so the count in each line stays the
        one the owner sees.
        """
        rows = [entry for _, entries, _ in groups
                for entry in (entries or []) if isinstance(entry, dict)]
        lines = ["**Queue**"]
        if not rows:
            lines.append("nothing runs and nothing waits")
        for entry in rows:
            running = entry.get("running")
            waiting = [str(prompt) for prompt in (entry.get("waiting") or [])]
            doing = (f"running {text.duration(float(running))}"
                     if isinstance(running, (int, float)) else "idle")
            count = f" · {len(waiting)} waiting" if waiting else " · nothing waits"
            agents = entry.get("agents")
            at_work = (f" · {agents} agent{'' if agents == 1 else 's'}"
                       if isinstance(agents, int) and agents > 0 else "")
            lines.append(f"{doing}{at_work}{count}")
            for number, prompt in enumerate(waiting, 1):
                lines.append(f"{number}. “{self._shareable(prompt)}”")
        listed: list[str] = []
        for _, entries, why in groups:
            if entries is None and why not in listed:
                listed.append(why)
        lines.extend(f"⚠️ another daemon {why}" for why in listed)
        return "\n".join(lines)

    def _shareable(self, prompt: str) -> str:
        """A task as a channel may read it: its words, or `redacted task`.

        The text that is checked is the text that is shown, so a task keeps its
        own words only when the shortened line holds nothing of a person.
        """
        said = text.shorten(prompt, 60)
        return said if text.shareable(said, tuple(self.config.devices)) else "redacted task"

    @staticmethod
    def _queue_block(entries: list, device: str) -> str:
        """One daemon's channels: what runs there, and every task that waits."""
        lines = [f"**{device}**" if device else "**This daemon**"]
        rows = [entry for entry in entries if isinstance(entry, dict)]
        if not rows:
            lines.append("nothing runs and nothing waits")
            return "\n".join(lines)
        for entry in rows:
            running = entry.get("running")
            waiting = [str(prompt) for prompt in (entry.get("waiting") or [])]
            doing = (f"running {text.duration(float(running))}"
                     if isinstance(running, (int, float)) else "idle")
            count = f" · {len(waiting)} waiting" if waiting else " · nothing waits"
            agents = entry.get("agents")
            at_work = (f" · {agents} agent{'' if agents == 1 else 's'}"
                       if isinstance(agents, int) and agents > 0 else "")
            lines.append(f"“{entry.get('title') or 'untitled'}”: {doing}{at_work}{count}")
            for number, prompt in enumerate(waiting, 1):
                lines.append(f"{number}. “{text.shorten(prompt, 60)}”")
        return "\n".join(lines)

    def _line_text(self, channel_id: str) -> list[str]:
        """Every task that waits in this channel, and the last three that ran.

        The whole line, not a sample: it holds ten at most, and a queue that
        does not name a task cannot be asked about, only waited on.
        """
        with self.runs_lock:
            waiting = list(self.waiting.get(channel_id) or [])
            finished = list(self.finished.get(channel_id) or [])
        lines: list[str] = []
        if waiting:
            lines.append(f"**Next up** ({len(waiting)} waiting):")
            for number, worker in enumerate(waiting, 1):
                lines.append(f"{number}. “{text.shorten(worker.prompt, 80)}”")
        if finished:
            lines.append("**Finished:**")
            lines.extend(f"“{text.shorten(prompt, 80)}”" for prompt in finished[-3:])
        return lines

    def _devices(self, pick: Pick, detailed: bool = False) -> str:
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

    def _bot_id(self) -> str:
        """This bot's own user id, or "" when Discord will not say.

        It is asked only when a person is allowed or denied: one request, and
        a failure leaves the check out rather than refusing a valid allow.
        """
        try:
            return str(self.api.me().get("id") or "")
        except (DiscordError, OSError):
            return ""

    def _allow(self, options: dict) -> str:
        user = options.get("user")
        target = commands.user_id(user)
        if not target:
            return "Name the person to allow."
        if target == self.config.owner:
            return "That is the owner, who may always ask."
        if target == self._bot_id():
            return "That is this bot. Only a person can ask."
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
        if target == self.config.owner:
            return "That is the owner, who may always ask."
        if target == self._bot_id():
            return "That is this bot. Only a person can ask."
        try:
            _done, said = self.allow.deny(target)
            return said
        except AllowError as error:
            return f"{error}"

    def _help(self, owner: bool, approved: bool) -> str:
        lines = [
            "**Vigo**, an OpenCode agent on your own machines, reached from Discord.",
            "",
        ]
        if owner:
            lines.extend([
                "`/vigo ask prompt:…` asks the device you last worked on. "
                "`device:` names one, `new:true` starts a fresh session. A question "
                "asked while one runs waits its turn. Attach a file, or reply to an "
                "answer with one, and the device reads it there.",
                "When the ask is a task, it gets a goal and works toward it until "
                "it is done, in a session of its own; the wording decides which it "
                "is. `/vigo goal objective:…` gives a goal yourself, with no "
                "question, in a session of its own; `pause`, `resume` and `clear` "
                "control the one this channel has.",
                "`/vigo quick prompt:…` answers once, with no session and no tools.",
                "`/vigo devices` shows what is up and where a question would go.",
                "`/vigo status` shows this channel's session, what waits in it, and "
                "what ran. `/vigo stop` ends its run and drops what waits.",
                "Every command that answers a question takes `hidden:true` to keep "
                "its answer to yourself. Without it, the channel sees it. "
                "`devices`, `allow`, `deny` and `allowed` are always private.",
            ])
        elif approved:
            lines.extend([
                "`/vigo ask prompt:…` asks the owner's device a question. It is "
                "private to you, even without `hidden`: the owner's files and "
                "history stay out of the channel, and your session is your own. "
                "Approved users ask questions. Work on the device, and the commands "
                "that name it, belong to the owner.",
            ])
        else:
            lines.extend([
                "`/vigo ask prompt:…` asks a device a question. It is for the "
                "owner and the people the owner allows; anyone else is refused.",
                "Only the bot's owner may ask.",
            ])
        if owner:
            lines.append("")
            lines.append(
                "Yours: `/vigo allow user:<who> for:<time>`, `/vigo deny user:<who>`, "
                "`/vigo allowed`, `/vigo queue`, `/vigo upgrade`. `/vigo queue public:true` "
                "shows the channel a copy with the names and the private tasks redacted. "
                "Tool permissions come to your DMs as buttons."
            )
        elif not approved:
            lines.append("")
            lines.append("Only the bot's owner may ask.")
        else:
            lines.append("")
            lines.append(
                "You may ask questions, up to the share of the owner's plan that is "
                "kept for approved people. Your question and its answer are private "
                "to you: the owner's files and history stay out of the channel, and "
                "your session is your own. Work on the device, and every command "
                "that names it, is the owner's."
            )
        return "\n".join(lines)

    # -- running ----------------------------------------------------------

    def run(self) -> None:
        # Cards that nobody answered are kept for a day, then dropped: a
        # state file that grows without a bound is a record of questions the
        # agent asked, and no reader needs it after that.
        self.state.prune_tickets()
        self.state.prune_forms()
        url = self.api.gateway_url()
        log.info(f"up: {self.config.device}, {len(self.config.devices)} devices, model {self.config.model}")
        self.register_commands()
        if self.config.peer_port:
            try:
                PeerServer(self.config.peer_host, self.config.peer_port,
                           self.config.device, self.peer_password(),
                           checkout=self.checkout, build=self.build,
                           built=self.built,
                           queue=self.queue_snapshot,
                           suspend=self._suspend_runs).start()
            except OSError as error:  # the tailnet is not up, or the port is taken
                log.error(f"peer: not listening ({error})")
        gateway = Gateway(url, self.config.token, INTENTS, self.on_event,
                          lambda state: log.debug(f"gateway: {state}"))
        self.gateway = gateway
        threading.Thread(target=self._watch_presence, name="presence", daemon=True).start()
        self._resume_suspended()
        gateway.run()


def info_request(info: dict) -> str:
    return f"{info['device']} · {info['request']}"
