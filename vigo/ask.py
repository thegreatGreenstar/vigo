"""One question, asked on a device, answered in Discord.

The deferred message becomes a short "got it" at once, so the person knows the
question arrived.  The answer is a new message when the agent is done: an edit
would carry no notification.  A tool that needs permission, or a question the
agent asks, goes to its owner in a DM, and the run waits for the answer.  Past
``timeout`` the deferred message says the device is still working, and the run
keeps watching: the channel is still busy, so the next question waits.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from . import github, lease as leasing, log, text
from .config import Config, Device
from .discord_api import Discord
from .notify import send_owner
from .opencode_api import EventStream, OpenCode, OpenCodeError, busy_since
from .state import State

FOOTER = {
    "succeeded": "",
    "failed": "the agent failed",
    "interrupted": "stopped",
    "never started": (
        "the device took the question and never started on it. "
        "`opencode serve` there is stuck: restart it, then ask again."
    ),
}

END_EVENTS = {
    "session.execution.failed": "failed",
    "session.execution.interrupted": "interrupted",
}

# What every piece of work is told, a question and a goal alike: the plan is a
# subscription, and a fan-out that ignores it buys failures instead of answers.
# Three is a ceiling, not a target: one subagent is as right as three when one
# is what the work needs.
SUBTASKS = 3
# What every piece of work is told about the thing it is part of: the model has
# no other way to know it is Vigo, and a question like "explain how Vigo works"
# was answered with "which Vigo?" because the prompt held only the question.
SELF = (
    "You are Vigo, a Discord bot that drives OpenCode agents on the owner's own "
    "machines. A daemon runs on each device, and each daemon can reach every "
    "device's `opencode serve`, so one question is answered on one of them; only "
    "the owner may approve a tool. The question arrived from Discord and your "
    "answer is posted there."
)
RULES = (
    f"You may hand parts of this work to subagents, at most {SUBTASKS} at a time, "
    "only where it helps: use fewer when fewer are enough, and none when none is. "
    "Report what they did.\n\n"
    "When the work went to GitHub, report it the way a maintainer reports it there: "
    "the verdict in the first sentence, then the facts that change what the reader "
    "does next, each with its number (a pull request or issue, a commit, a file and "
    "line, a count of tests), then what still waits for a person. That is one short "
    "paragraph, about fifty words, and a list of four facts is the long one. No "
    "thanks, no restating of the request, and no walk through the diff.\n\n"
    "Sign what you write on GitHub as \"From Vigo (an agent)\", once and once "
    "only. A pull request or an issue ends with it, on a line of its own. A "
    "comment starts with it: \"From Vigo (an agent): <the reply>\". Never sign "
    "it with the name of the model that ran you: the model varies, and Vigo "
    "does not.\n\n"
    "Keep the answer to two short paragraphs at most: say what you did and what "
    "matters, and leave the rest out. Be only as long as the answer needs: one "
    "line is right when one line is the answer. The whole answer stays in the "
    "session, and the person asks again when they want more."
)

# One answer may take a few messages: Discord takes 2000 characters in each,
# and an answer that needs more is sent whole instead of cut mid-sentence.  A
# runaway answer stops at this many, and the last one says where the rest is.
ANSWER_PARTS = 4
MORE = "… (the rest of this answer is in the session)"

# The session's metadata key that names the Discord channel it serves.  The
# device keeps it, so both daemons and any restart find the same session
# instead of making another one.
CHANNEL_KEY = "vigoChannel"

# How long to wait between looks at a session another daemon is working on.
POLL = 0.5

# How long an answer of ours counts as the one already in the channel, in
# seconds.  Longer than a retry or a restart takes, shorter than a person
# asking the same thing again.
RECENT = 300.0


def _failure(data: dict) -> str:
    """The device's own words for why a turn failed, from its event.

    The shapes differ with the OpenCode release, so every place the text can be
    is read; none of it goes to Discord unread, and `text.cause` picks the line.
    """
    for key in ("message", "reason", "error"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            for inner in ("message", "reason"):
                said = value.get(inner)
                if isinstance(said, str) and said.strip():
                    return said
    return ""


def _recent(stamp: object, seconds: float) -> bool:
    """Whether Discord's ISO timestamp is within that many seconds of now."""
    if not isinstance(stamp, str) or not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return abs(time.time() - when.timestamp()) <= seconds


class Host(Protocol):
    """What a run needs from the app: its settings, what it remembers, its Discord."""

    config: Config
    state: State
    api: Discord


def open_session(app: Host, client: OpenCode, device: Device, channel_id: str,
                 new: bool, title: str, agent: str = "",
                 worker: bool = False) -> str:
    """This channel's session on that device, or a new one.

    Shared with `/vigo goal`, which needs a session to hang an objective on
    but never asks a question.

    Only an explicit `new` makes a session the device already holds go unused.
    Restarts and two daemons answering the same channel used to create one
    session each, so a device filled up with "Discord DM": this daemon reads
    its own state first, then asks the device, which marks the session it made
    with the channel it belongs to.

    `worker` makes a session of the task's own, never the channel's: the work
    runs there under `agent`, and OpenCode lists it, so the person can open
    that session and carry on in it directly.  The channel remembers it, so a
    follow-up and `/vigo status` reach the session the work is in.
    """
    if worker:
        created = client.create_session(
            device.directory, title or "Discord", model=app.config.model.as_ref(),
            agent=agent or None, metadata={CHANNEL_KEY: channel_id},
        )
        session = created.get("id")
        if not session:
            raise OpenCodeError(0, "no session id", "POST /api/session")
        app.state.remember_session(channel_id, device.name, session, title)
        log.info(f"session: the task runs in its own session on {device.name}")
        return session
    if not new:
        remembered = app.state.session_on(channel_id, device.name)
        if remembered and _alive(client, remembered["session"]):
            return remembered["session"]
        found = _adopt(client, channel_id)
        if found:
            log.info("session: this channel already has one on the device; using it")
            app.state.remember_session(channel_id, device.name, found, title)
            return found
    created = client.create_session(
        device.directory, title or "Discord", model=app.config.model.as_ref(),
        metadata={CHANNEL_KEY: channel_id},
    )
    session = created.get("id")
    if not session:
        raise OpenCodeError(0, "no session id", "POST /api/session")
    app.state.remember_session(channel_id, device.name, session, title)
    return session


def channel_session(client: OpenCode, channel_id: str) -> str:
    """The session the device says this channel already has, or "".

    A daemon that has just started remembers none of this channel's sessions,
    and the other daemon may hold the goal.  The device's own record is the one
    place both of them read, so `/vigo goal` checks it before it makes a second
    goal for the channel.
    """
    return _adopt(client, channel_id)


def _alive(client: OpenCode, session_id: str) -> bool:
    try:
        client.session(session_id)
        return True
    except OpenCodeError:
        return False


def _adopt(client: OpenCode, channel_id: str) -> str:
    """The session this channel already has on the device, if the device says so.

    A device lists its sessions newest first, so the newest one naming this
    channel wins — the same answer a `new` ask would have left behind.
    """
    try:
        for info in client.sessions(limit=100):
            metadata = info.get("metadata")
            if isinstance(metadata, dict) and str(metadata.get(CHANNEL_KEY) or "") == channel_id:
                return str(info.get("id") or "")
    except OpenCodeError:
        pass  # a device that will not list them gets a new one, and says so
    return ""


# The message stays empty (Discord shows "is thinking...") until the answer is
# ready. No interim text is sent, by choice: the answer or nothing.

# The deferred message says it was received, and the answer is its own message
# so Discord notifies the person who asked.  An answer longer than one message
# goes out in several: a cut one used to stop mid-sentence with nothing to say
# so, and it read as the agent giving up.


@dataclass
class Run:
    session: str
    device: str
    started: float
    prompt: str = ""
    stop: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class Ask:
    """Ask one device one question, and keep its Discord message current."""

    def __init__(self, app: Host, *, token: str, channel_id: str, user_id: str,
                 device_name: str, prompt: str, new: bool, title: str,
                 hidden: bool = False, goal: str = "", reply_in_channel: bool = False,
                 note: str = "") -> None:
        self.app = app
        self.token = token
        self.channel_id = channel_id
        self.hidden = hidden
        self.user_id = user_id
        self.device_name = device_name
        self.device = app.config.device_named(device_name)
        self.client = OpenCode(self.device.url, self.device.password)
        self.prompt = prompt
        self.note = note
        self.failure = ""
        self.task_title = ""
        self.goal = goal
        self.reply_in_channel = reply_in_channel
        self.new = new
        self.title = title
        self.run = Run(session="", device=device_name, started=time.monotonic(), prompt=prompt)
        self.buffer = ""
        self.lease: leasing.Lease | None = None
        self.waiting = False
        self.prompted_at = 0.0
        self.before = ""   # the session's newest message when we asked
        self.forms: set[str] = set()

    # -- the message ------------------------------------------------------

    def question(self) -> str:
        """The question, with what the device should know of who reads the answer.

        The device never hears of Discord otherwise: without this line an
        answer written for the person who asked can end up in a channel where
        everyone reads it, and the message cannot tell that it is the wrong
        place for a key or for someone else's details.
        """
        return "\n\n".join(part for part in (self.note, SELF, self.prompt, RULES) if part)

    def _edit(self, content: str) -> bool:
        try:
            self.app.api.edit_original(self.token, {
                "content": content[:2000],
                "allowed_mentions": {"parse": []},
            })
            return True
        except Exception as error:
            log.debug(f"ask: cannot edit the message ({error})")
            return False

    def _final(self, body: str, outcome: str, cost: float, tokens: dict,
               steps: int = 0, context: int = 0) -> None:
        note = FOOTER.get(outcome, "")
        if outcome == "failed":
            # A failure the person can act on: a limit, a quota, a context, a
            # key.  The device's own text stays in the log, never in Discord.
            note = "❌ " + text.cause(self.failure)
        # Everyone in a channel reads this answer, and the agent can read a
        # key or a token while it works: a value shaped like one is replaced
        # before it goes out.  A hidden answer, or one in a DM, is read by the
        # person who asked, who may see what they asked about.
        removed = ""
        if not self.hidden and not self.reply_in_channel:
            body, removed = text.scrub(body)
        body = body.strip()
        # GitHub work goes first, so a reader meets the pull request before the
        # prose about it.  Only work the answer names is here, and a link the
        # answer already put on its first line is not said again.
        front = github.mark(body)
        if front:
            body = f"{front}\n\n{body}" if body else front
        if not body and not note and steps == 0:
            # A turn that produced nothing at all: the machine slept under it,
            # or the device took the question and never ran it.  That is not an
            # empty answer, and it must not read like one.
            body = "the device returned no text for this question"
        content = body or "_nothing to say_"
        if note:
            content += f"\n\n{note}"
        goal_line = self._goal_line()
        if goal_line:
            content += f"\n\n{goal_line}"
        if self.task_title:
            content += (f"\n\nThis ran in its own OpenCode session, “{self.task_title}”; "
                        "open it there to carry on with the work.")
        if removed:
            content += (
                f"\n\n🔒 a {removed} was removed from this answer; "
                "`hidden:true` shows the answer whole."
            )
        # An answer longer than one message goes out in several, in order, so
        # nobody reads a sentence that stops because Discord's limit was hit.
        parts = text.messages(content, ANSWER_PARTS, more=MORE) or ["_nothing to say_"]
        # The answer is a new message, after the "got it": Discord notifies on
        # a new message but not on an edit, and the "got it" stays as the
        # record that the question arrived.  A hidden answer is ephemeral, so
        # it cannot be posted in the channel when the interaction has expired.
        if self._already_answered(parts[0]):
            # Two runs can watch one device turn, and both would post it.
            log.info("ask: the channel already has that answer; not posting it twice")
        elif self.reply_in_channel:
            # The question came as a message, so the answer is one: there is no
            # interaction to edit and nothing that expires.
            for part in parts:
                self._late_answer(part, check=False)
        else:
            self._followup(parts, {"flags": 64} if self.hidden else {})
        log.info(
            f"ask: {self.device_name} · {text.duration(self.run.elapsed)} · "
            f"{steps} step(s) · context {text.tokens(context)} · "
            f"{text.tokens(int((tokens or {}).get('output', 0)))} out · "
            f"{text.money(cost)}"
        )

    def _followup(self, parts: list[str], extra: dict) -> None:
        """Post the answer after the "got it", in as many messages as it takes.

        The same interaction token carries each one, so a channel where this bot
        is not a member still gets the whole answer.  A message that Discord
        refuses sends that part, and the ones after it, where the question was.
        """
        for number, part in enumerate(parts):
            try:
                posted = self.app.api.followup(self.token, {
                    "content": part[:2000],
                    "allowed_mentions": {"parse": []},
                    **extra,
                })
                self._record(posted)
            except Exception as error:
                log.error(f"ask: cannot post the answer ({error})")
                for rest in parts[number:]:
                    self._late_answer(rest, check=False)
                return

    def _goal_line(self) -> str:
        """Where the goal stands now, for the person who set it.

        A goal outlives a turn, so "the answer is in" does not say whether the
        work is finished.  The device knows: `complete` is the line the person
        waits for, and a paused one must not look like a running one.
        """
        if not self.goal or not self.run.session:
            return ""
        try:
            held = self.client.goal(self.run.session)
        except OpenCodeError as error:
            log.info(f"ask: cannot read the goal ({error.message})")
            return ""
        if not isinstance(held, dict) or not held:
            return ""
        status = str(held.get("status") or "")
        if status == "complete":
            return "**the goal is met**"
        if status == "paused":
            reason = str(held.get("reason") or "").strip()
            return f"the goal is paused{': ' + reason if reason else ''}"
        rounds, most = held.get("rounds"), held.get("maxRounds")
        if status == "active" and isinstance(rounds, int):
            return f"the goal is still active (round {rounds} of {most})"
        if status == "active":
            return "the goal is still active"
        return ""

    def _record(self, message: object) -> None:
        """Remember a message this run posted, so a reply to it is a question."""
        if not isinstance(message, dict):
            return
        message_id = str(message.get("id") or "")
        if message_id:
            try:
                self.app.state.remember_answer(message_id, self.channel_id)
            except OSError as error:
                # The answer is already out.  Remembering where it landed is a
                # convenience for a reply, not a reason to post it a second
                # time: a state file that cannot be written must not duplicate
                # the answer in the channel.
                log.error(f"ask: cannot remember the answer ({error})")

    def _late_answer(self, content: str, check: bool = True) -> bool:
        """The interaction's 15 minutes ran out: post it where the question was.

        An answer that cannot go there is not thrown away: a hidden one has an
        ephemeral message that expired, and a channel where this bot is not a
        member refuses the post.  The owner's DM takes it then, so a goal that
        ran for an hour still reaches the person who asked for it.

        `check` is off for the parts after the first of an answer in several
        messages: the run looked for the answer once, and a part that happens to
        match an earlier message is not a reason to drop the rest.
        """
        if self.hidden:
            log.info("ask: the answer is too late for an ephemeral message; sending it to the owner")
            return self._last_resort(content)
        if check and self._already_answered(content):
            log.info("ask: the channel already has that answer; not posting it twice")
            return True
        try:
            posted = self.app.api.send(self.channel_id, {
                "content": content[:2000],
                "allowed_mentions": {"parse": []},
            })
            self._record(posted)
            return True
        except Exception as error:
            log.error(f"ask: cannot post the answer at all ({error})")
            return self._last_resort(content)

    def _last_resort(self, content: str) -> bool:
        """The answer reaches the person when Discord will not take it there.

        The owner's DM is the one place this bot can always open, because the
        bot is a member of it whatever the channel is.  The line above says it
        is late, so it does not read like a new question's answer.
        """
        try:
            send_owner(self.app.config,
                       f"(an answer that arrived too late for its message)\n\n{content}",
                       api=self.app.api)
            log.info("ask: the answer went to the owner's DM")
            return True
        except Exception as error:  # noqa: BLE001 - any failure means it did not arrive
            log.error(f"ask: the owner's DM did not take the answer ({error})")
            return False

    def _already_answered(self, content: str) -> bool:
        """Whether this exact answer is already in the channel, from us, just now.

        Two runs can watch one device turn — the run beside it in this daemon
        before the queue was fixed, or the other daemon answering the same
        channel — and each posts the answer to a message that has expired.  The
        channel is the record they share, so the second one stays quiet.
        """
        said = content.strip()[:2000]
        if not said:
            return False
        try:
            recent = self.app.api.messages(self.channel_id, limit=10)
        except Exception as error:  # a channel that cannot be read proves nothing
            log.debug(f"ask: cannot read the channel to check ({error})")
            return False
        for message in recent:
            if not (message.get("author") or {}).get("bot"):
                continue
            if (message.get("content") or "").strip() != said:
                continue
            if _recent(message.get("timestamp"), RECENT):
                return True
        return False

    def _tell(self, content: str) -> None:
        """An error from the run, visible even when the deferred message is gone."""
        if self.reply_in_channel:
            self._late_answer(content)
            return
        if not self._edit(content):
            self._late_answer(content)

    # -- the session ------------------------------------------------------

    def _open_session(self) -> str:
        if not self.goal:
            return open_session(
                self.app, self.client, self.device, self.channel_id, self.new, self.title,
            )
        # A task is work, not a question: it runs in a session of its own, with
        # a subagent, so the conversation stays a conversation and the person
        # can open the work where it happens.
        self.task_title = f"{self.title} · {text.subject(self.prompt, 40)}"
        return open_session(
            self.app, self.client, self.device, self.channel_id, self.new,
            self.task_title, agent=self.client.subagent_agent(), worker=True,
        )

    # -- the answer -------------------------------------------------------

    def _collect(self, session: str) -> tuple[str, float, dict, int, int]:
        """The answer, its cost, the tokens moved, the steps, and the last context."""
        try:
            messages = self.client.messages(session, limit=40, order="desc")
        except OpenCodeError as error:
            log.error(f"ask: cannot read the answer ({error})")
            return self.buffer.strip(), 0.0, {}, 0, 0
        body: list[str] = []
        cost = 0.0
        tokens = {"input": 0, "output": 0, "reasoning": 0}
        steps = 0
        context = 0
        for message in reversed(messages):
            if message.get("type") != "assistant":
                continue
            created = float((message.get("time") or {}).get("created") or 0)
            if created and created < self.prompted_at - 1500:
                continue
            steps += 1
            for part in message.get("content") or []:
                if part.get("type") == "text" and part.get("text"):
                    body.append(part["text"])
            cost += float(message.get("cost") or 0.0)
            usage = message.get("tokens") or {}
            for key in ("input", "output", "reasoning"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    tokens[key] += int(value)
            # Every step re-sends the whole context, so the totals above are
            # what was billed; this is how big the context actually got.
            if isinstance(usage.get("input"), (int, float)):
                context = max(context, int(usage["input"]))
        answer = "\n\n".join(piece.strip() for piece in body if piece.strip()).strip()
        if not (tokens["input"] or tokens["output"]):
            tokens = {}
        return answer, cost, tokens, steps, context

    def _note(self, event: dict) -> None:
        """Keep the streamed text, which is the answer if the messages cannot be read."""
        kind = event.get("type", "")
        data = event.get("data") or {}
        if kind == "session.text.delta":
            self.buffer += str(data.get("delta") or "")
        elif kind == "session.text.ended":
            piece = data.get("text")
            if piece:
                self.buffer = str(piece)

    def _permission(self, session: str, data: dict) -> None:
        request_id = data.get("id")
        if not request_id:
            return
        self.waiting = True
        try:
            sent = self.app.ask_permission(
                device=self.device_name,
                session=session,
                request=str(request_id),
                action=str(data.get("action") or "a tool"),
                resources=[str(item) for item in (data.get("resources") or [])][:4],
                channel=self.channel_id,
            )
        except Exception as error:
            log.error(f"ask: cannot ask for permission ({error})")
            self.waiting = False
            return
        self._tell(
            "A tool needs your approval. Check your DMs."
            if sent else
            "A tool needs your approval, but I could not send you a DM."
        )

    def _form(self, session: str, form: dict) -> None:
        """Relay a question the agent asked, once."""
        form_id = str(form.get("id") or "")
        if not form_id or form_id in self.forms:
            return
        fields = [item for item in (form.get("fields") or []) if isinstance(item, dict)]
        if not fields:
            log.error("ask: the agent asked a form with no fields")
            return
        self.forms.add(form_id)
        self.waiting = True
        try:
            sent = self.app.ask_question(
                device=self.device_name,
                session=session,
                form=form_id,
                title=str(form.get("title") or "Question"),
                fields=fields,
                channel=self.channel_id,
            )
        except Exception as error:
            log.error(f"ask: cannot ask the question ({error})")
            self.waiting = False
            return
        if not sent:
            told = "The agent asked a question, but I could not send it to you."
        elif self.user_id == self.app.config.owner:
            told = "The agent asked a question. Check your DMs."
        else:
            told = "The agent asked a question. It went to the bot's owner."
        self._tell(told)

    def _pending_forms(self, session: str) -> None:
        """The stream is gone, so ask the device what it still waits on."""
        try:
            pending = self.client.forms(session)
        except OpenCodeError:
            return
        for form in pending:
            if isinstance(form, dict) and form.get("sessionID") == session:
                self._form(session, form)

    # -- the run ----------------------------------------------------------

    def run_it(self) -> None:
        try:
            self._run()
        except Exception as error:  # a run always ends, and always unregisters
            log.error(f"ask: broke on {self.device_name}: {type(error).__name__}: {error}")
            self._tell("The run broke. The log says what.")
        finally:
            # The only place that gives the channel up, and it gives up *this*
            # run: a second call from `_run`'s own paths used to take the next
            # run's place and start another beside it.
            if self.lease is not None:
                self.lease.release()
            self.app.unregister_run(self.channel_id, self.run)

    def _run(self) -> None:
        started = time.monotonic()
        self.run.started = started
        try:
            session = self._open_session()
        except OpenCodeError as error:
            log.info(f"ask: cannot open a session ({error.message})")
            self._tell("The device would not open a session.")
            return
        self.run.session = session
        self.app.register_run(self.channel_id, self.run)
        self.lease = leasing.Lease(self.client, session, self.device_name)
        if not self._wait_for_turn(session, time.monotonic() + self.app.config.timeout):
            if self.run.stop.is_set():
                self._tell("Stopped.")
            else:
                log.info("ask: the channel is taken; this one did not run")
                self._tell(
                    "Another vigo is asking in this channel, so this one did not run. "
                    "Ask again when it is free.",
                )
            return
        session_stop = threading.Event()
        stream = EventStream(self.device.url, self.device.password, session_stop).start()
        stream.ready.wait(3.0)

        try:
            self.prompted_at = time.time() * 1000.0
            self.before = self._newest_id(session)
            if self.goal:
                self.client.set_goal(session, f"{SELF}\n\n{self.goal}\n\n{RULES}")
            else:
                self.client.prompt(session, self.question())
        except OpenCodeError as error:
            session_stop.set()
            log.info(f"ask: the prompt was refused ({error.message})")
            self._tell("The device would not take the question.")
            return

        # The wait for a free device is not time on the turn, so the deadline
        # starts here, not when the run did.
        deadline = time.monotonic() + self.app.config.timeout
        noted = False
        stream_dead = False
        poll_at = 0.0
        succeeded_at = 0.0
        outcome = ""

        while not self.run.stop.is_set():
            now = time.monotonic()
            if self.lease is not None:
                self.lease.renew()
            if not noted and now > deadline:
                # Past `timeout`, say where it is and keep watching while the
                # device works: `/vigo status` must not call the channel idle,
                # and the next question in the channel must keep waiting.  But
                # a device that took the question and never started on it will
                # never say anything, and this run would hold the channel for
                # ever with every later question behind it: end, and say why.
                noted = True
                verdict = self._verdict(session)
                if verdict == "finished":
                    outcome = "succeeded"
                    break
                if verdict == "never started":
                    outcome = "never started"
                    break
                self._tell("Still running on the device. `/vigo stop` ends it.")
            if succeeded_at and now - succeeded_at > 3.0:
                outcome = "succeeded"
                break

            event = None
            if stream_dead:
                if now >= poll_at:
                    poll_at = now + 2.0
                    self._pending_forms(session)
                    if self._is_idle(session):
                        outcome = "succeeded"
                        break
                else:
                    time.sleep(min(0.4, poll_at - now))
            else:
                try:
                    event = stream.events.get(timeout=0.5)
                except queue.Empty:
                    event = False
                if event is None:  # the stream's end marker
                    log.info("ask: the event stream ended; polling instead")
                    stream_dead = True
                    poll_at = 0.0
                    continue
                if event is False:
                    event = None

            if event:
                data = event.get("data") or {}
                kind = event.get("type", "")
                form = data.get("form")
                if isinstance(form, dict) and form.get("sessionID") == session:
                    self._form(session, form)
                elif data.get("sessionID") == session:
                    self._note(event)
                    if kind == "permission.asked":
                        self._permission(session, data)
                    elif kind == "permission.replied":
                        self.waiting = False
                    elif kind in ("form.replied", "form.cancelled"):
                        self.waiting = False
                    elif kind == "session.execution.succeeded":
                        succeeded_at = now
                    elif kind in END_EVENTS:
                        outcome = END_EVENTS[kind]
                        if kind == "session.execution.failed":
                            self.failure = _failure(data)
                        break
                    elif kind == "session.idle":
                        outcome = "succeeded"
                        break


        if not outcome:
            outcome = "interrupted" if self.run.stop.is_set() else "succeeded"
        session_stop.set()
        stream.close()

        answer, cost, tokens, steps, context = self._collect(session)
        self._final(answer, outcome, cost, tokens, steps, context)
        if self.failure:
            log.info(f"ask: {outcome}: {self.failure}")
        log.info(f"ask: done on {self.device_name} ({outcome})")

    def _is_idle(self, session: str) -> bool:
        try:
            for message in self.client.messages(session, limit=6, order="desc"):
                if message.get("type") == "idle":
                    return True
        except OpenCodeError:
            pass
        return False

    def _wait_for_turn(self, session: str, until: float) -> bool:
        """Wait until the device is free and this run holds the channel.

        Two things have to be true before a prompt, and either alone leaves a
        way for two questions to meet in one turn. The device says when it is
        working on the session; the lease says which daemon owns the channel,
        which the device cannot know. `until` bounds the wait, because a
        question that waited `timeout` is one that did not run.
        """
        announced = False
        while not self.run.stop.is_set():
            # Once ours, it stays ours until we release it or somebody takes a
            # stale one, so the claim is made once and the wait is for the
            # device to finish the turn it is on.
            if self.lease is None:
                held = "unknown"
            elif self.lease.mine:
                held = "mine"
            else:
                held = self.lease.claim()
            if held == "mine":
                try:
                    if busy_since(self.client.session(session)) is None:
                        return True
                except OpenCodeError:
                    return True  # cannot tell: ask, and the run reports what happens
            elif held == "unknown":
                return True  # the device keeps no lease: ask, and the log says so
            if not announced:
                log.info("ask: the channel is taken or the device is busy; waiting")
                announced = True
            left = until - time.monotonic()
            if left <= 0:
                return False
            # Never sleep past the deadline: a short `timeout` should be short.
            time.sleep(min(POLL, left))
        return False

    def _newest_id(self, session: str) -> str:
        """The id of the session's newest message, or "" if it has none."""
        try:
            newest = self.client.messages(session, limit=1, order="desc")
        except OpenCodeError:
            return ""
        return str(newest[0].get("id") or "") if newest else ""

    def _verdict(self, session: str) -> str:
        """What the device says about this question, read once past `timeout`.

        The test is the session's own newest message, not a clock and not the
        device's `busy` stamp: a device that took the question and hung writes
        nothing, and it may still stamp itself busy doing it.  Vigo learned
        both shapes the hard way, so neither is trusted here.

        - `finished`: the newest message is an idle that was not there when we
          asked, so the answer is on the device even though no event carried it.
        - `never started`: the session has not changed since we asked.  Nothing
          is going to run the question, and watching further would hold this
          channel for ever with every later question behind it.
        - `running` or `waiting`: the device took it and is working, or will not
          answer at all.  Both keep the old behaviour: keep watching.
        """
        try:
            newest = self.client.messages(session, limit=1, order="desc")
        except OpenCodeError as error:
            # A session that is gone will never run it; a device that does not
            # answer at all proves nothing, and keeps the old behaviour.
            return "never started" if error.status == 404 else "waiting"
        if not newest:
            # Nothing on the session, now or when we asked: it wrote nothing.
            return "never started"
        message = newest[0]
        if str(message.get("id") or "") == self.before:
            return "never started"
        if message.get("type") == "idle":
            return "finished"
        return "running"
