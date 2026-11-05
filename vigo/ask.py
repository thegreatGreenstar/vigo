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

from . import log, text
from .opencode_api import EventStream, OpenCode, OpenCodeError, busy_since

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


def _recent(stamp, seconds: float) -> bool:
    """Whether Discord's ISO timestamp is within that many seconds of now."""
    if not isinstance(stamp, str) or not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return abs(time.time() - when.timestamp()) <= seconds


def open_session(app, client: OpenCode, device, channel_id: str, new: bool,
                 title: str) -> str:
    """This channel's session on that device, or a new one.

    Shared with `/vigo goal`, which needs a session to hang an objective on
    but never asks a question.

    Only an explicit `new` makes a session the device already holds go unused.
    Restarts and two daemons answering the same channel used to create one
    session each, so a device filled up with "Discord DM": this daemon reads
    its own state first, then asks the device, which marks the session it made
    with the channel it belongs to.
    """
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
# so Discord notifies the person who asked.  The answer is cut to two
# paragraphs: the channel stays readable, and a follow-up reaches the rest.


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

    def __init__(self, app, *, token: str, channel_id: str, user_id: str,
                 device_name: str, prompt: str, new: bool, title: str,
                 hidden: bool = False, goal: str = ""):
        self.app = app
        self.token = token
        self.channel_id = channel_id
        self.hidden = hidden
        self.user_id = user_id
        self.device_name = device_name
        self.device = app.config.device_named(device_name)
        self.client = OpenCode(self.device.url, self.device.password)
        self.prompt = prompt
        self.goal = goal
        self.new = new
        self.title = title
        self.run = Run(session="", device=device_name, started=time.monotonic(), prompt=prompt)
        self.buffer = ""
        self.waiting = False
        self.prompted_at = 0.0
        self.before = ""   # the session's newest message when we asked
        self.forms: set[str] = set()

    # -- the message ------------------------------------------------------

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
        content = text.brief(body.strip()) or "_nothing to say_"
        if note:
            content += f"\n\n{note}"
        content = content[:2000]
        # The answer is a new message, after the "got it": Discord notifies on
        # a new message but not on an edit, and the "got it" stays as the
        # record that the question arrived.  A hidden answer is ephemeral, so
        # it cannot be posted in the channel when the interaction has expired.
        extra = {"flags": 64} if self.hidden else {}
        if self._already_answered(content):
            # Two runs can watch one device turn, and both would post it.
            log.info("ask: the channel already has that answer; not posting it twice")
        else:
            try:
                self.app.api.followup(self.token, {
                    "content": content,
                    "allowed_mentions": {"parse": []},
                    **extra,
                })
            except Exception as error:
                log.error(f"ask: cannot post the answer ({error})")
                self._late_answer(content)
        log.info(
            f"ask: {self.device_name} · {text.duration(self.run.elapsed)} · "
            f"{steps} step(s) · context {text.tokens(context)} · "
            f"{text.tokens(int((tokens or {}).get('output', 0)))} out · "
            f"{text.money(cost)}"
        )

    def _late_answer(self, content: str) -> None:
        """The interaction's 15 minutes ran out: post it where the question was."""
        if self.hidden:
            log.error("ask: the answer is too late for an ephemeral message")
            return
        if self._already_answered(content):
            log.info("ask: the channel already has that answer; not posting it twice")
            return
        try:
            self.app.api.send(self.channel_id, {
                "content": content[:2000],
                "allowed_mentions": {"parse": []},
            })
        except Exception as error:
            log.error(f"ask: cannot post the answer at all ({error})")

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
        if not self._edit(content):
            self._late_answer(content)

    # -- the session ------------------------------------------------------

    def _open_session(self) -> str:
        return open_session(
            self.app, self.client, self.device, self.channel_id, self.new, self.title,
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
        if not self._wait_for_idle(session, time.monotonic() + self.app.config.timeout):
            if self.run.stop.is_set():
                self._tell("⏹️ stopped.")
            else:
                log.info("ask: the device is busy with another question; this one did not run")
                self._tell(
                    "The device is still working on another question in this channel, "
                    "so this one did not run. Ask again when it is free.",
                )
            return
        session_stop = threading.Event()
        stream = EventStream(self.device.url, self.device.password, session_stop).start()
        stream.ready.wait(3.0)

        try:
            self.prompted_at = time.time() * 1000.0
            self.before = self._newest_id(session)
            if self.goal:
                self.client.set_goal(session, self.goal)
            else:
                self.client.prompt(session, self.prompt)
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
        log.info(f"ask: done on {self.device_name} ({outcome})")

    def _is_idle(self, session: str) -> bool:
        try:
            for message in self.client.messages(session, limit=6, order="desc"):
                if message.get("type") == "idle":
                    return True
        except OpenCodeError:
            pass
        return False

    def _wait_for_idle(self, session: str, until: float) -> bool:
        """Wait for the device to be free, so two daemons never share one turn.

        Both daemons receive every command and each keeps its own queue, so two
        questions for one channel can arrive at one session at the same time.
        The device then merges them into a single turn and one answer answers
        both.  It says which session it is working on, so this one waits for it
        rather than steers into it.  `until` bounds the wait: a question that
        waited `timeout` for a free device is one that did not run.
        """
        announced = False
        while not self.run.stop.is_set():
            try:
                if busy_since(self.client.session(session)) is None:
                    return True
            except OpenCodeError:
                return True  # cannot tell: ask, and the run reports what happens
            if not announced:
                log.info("ask: the device is busy with another question; waiting")
                announced = True
            if time.monotonic() >= until:
                return False
            time.sleep(POLL)
        return False

    def _newest_id(self, session: str) -> str:
        """The id of the session's newest message, or "" if it has none."""
        try:
            newest = self.client.messages(session, limit=1, order="desc")
        except OpenCodeError:
            return ""
        return str((newest[0].get("id") or "")) if newest else ""

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
