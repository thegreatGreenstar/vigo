"""One question, asked on a device, answered in Discord.

The message is deferred, stays as Discord's own "thinking" indicator while the
agent works, and is edited into the answer when it is ready.  A tool that needs
permission asks its owner in a DM, and the run waits for the answer.  Past
``timeout`` the message says the device is still working, and the run keeps
watching: the channel is still busy, so the next question waits.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from . import log, text
from .opencode_api import EventStream, OpenCode, OpenCodeError

FOOTER = {
    "succeeded": "",
    "failed": "❌ the agent failed",
    "interrupted": "⏹️ stopped",
}

END_EVENTS = {
    "session.execution.failed": "failed",
    "session.execution.interrupted": "interrupted",
}

# The message stays empty (Discord shows "is thinking...") until the answer is
# ready. No interim text is sent, by choice: the answer or nothing.


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
                 hidden: bool = False):
        self.app = app
        self.token = token
        self.channel_id = channel_id
        self.hidden = hidden
        self.user_id = user_id
        self.device_name = device_name
        self.device = app.config.device_named(device_name)
        self.client = OpenCode(self.device.url, self.device.password)
        self.prompt = prompt
        self.new = new
        self.title = title
        self.run = Run(session="", device=device_name, started=time.monotonic(), prompt=prompt)
        self.buffer = ""
        self.waiting = False
        self.prompted_at = 0.0

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
        content = body.strip() or "_nothing to say_"
        if note:
            content += f"\n\n{note}"
        chunks = text.chunk(content) or ["_nothing to say_"]
        # The deferred response is the message this run owns, so the answer
        # goes into it: an edit keeps it where the question was asked and keeps
        # the ephemeral state Discord deferred it with.  A followup must not be
        # the first thing sent: Discord turns one sent before the deferred
        # message has any content into an edit of it, so a later "delete the
        # original" would delete the answer itself.  Only the overflow becomes
        # followups, and they are ephemeral when the answer is.
        if not self._edit(chunks[0]):
            if self.hidden:
                log.error("ask: the run outlived its ephemeral message; the answer is lost")
            else:
                log.info("ask: the deferred message is gone; posting the answer in the channel")
                for piece in chunks:
                    self._late_answer(piece)
        else:
            extra = {"flags": 64} if self.hidden else {}
            for piece in chunks[1:]:
                try:
                    self.app.api.followup(self.token, {
                        "content": piece,
                        "allowed_mentions": {"parse": []},
                        **extra,
                    })
                except Exception as error:
                    log.error(f"ask: cannot post the rest of the answer ({error})")
                    break
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
        try:
            self.app.api.send(self.channel_id, {
                "content": content[:2000],
                "allowed_mentions": {"parse": []},
            })
        except Exception as error:
            log.error(f"ask: cannot post the answer at all ({error})")

    def _tell(self, content: str) -> None:
        """An error from the run, visible even when the deferred message is gone."""
        if not self._edit(content):
            self._late_answer(content)

    # -- the session ------------------------------------------------------

    def _open_session(self) -> str:
        remembered = self.app.state.session_for(self.channel_id)
        if remembered and not self.new and remembered.get("device") == self.device_name:
            try:
                self.client.session(remembered["session"])
                return remembered["session"]
            except OpenCodeError:
                log.info("ask: the remembered session is gone; making a new one")
        created = self.client.create_session(
            self.device.directory, self.title or "Discord",
            model=self.app.config.model.as_ref(),
        )
        session = created.get("id")
        if not session:
            raise OpenCodeError(0, "no session id", "POST /api/session")
        self.app.state.remember_session(self.channel_id, self.device_name, session, self.title)
        return session

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
            self.app.ask_permission(
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

    # -- the run ----------------------------------------------------------

    def run_it(self) -> None:
        try:
            self._run()
        except Exception as error:  # a run always ends, and always unregisters
            log.error(f"ask: broke on {self.device_name}: {type(error).__name__}: {error}")
            self._tell(f"❌ the run on `{self.device_name}` broke: {text.shorten(str(error), 300)}")
        finally:
            self.app.unregister_run(self.channel_id)

    def _run(self) -> None:
        started = time.monotonic()
        self.run.started = started
        try:
            session = self._open_session()
        except OpenCodeError as error:
            log.info(f"ask: cannot open a session ({error.message})")
            self._tell("❌ the device would not open a session.")
            return
        self.run.session = session
        self.app.register_run(self.channel_id, self.run)
        session_stop = threading.Event()
        stream = EventStream(self.device.url, self.device.password, session_stop).start()
        stream.ready.wait(3.0)

        try:
            self.prompted_at = time.time() * 1000.0
            self.client.prompt(session, self.prompt)
        except OpenCodeError as error:
            session_stop.set()
            log.info(f"ask: the prompt was refused ({error.message})")
            self._tell("❌ the device would not take the question.")
            self.app.unregister_run(self.channel_id)
            return

        deadline = started + self.app.config.timeout
        noted = False
        stream_dead = False
        poll_at = 0.0
        succeeded_at = 0.0
        outcome = ""

        while not self.run.stop.is_set():
            now = time.monotonic()
            if not noted and now > deadline:
                # Say it and keep watching.  The device is still working, so
                # `/vigo status` must not call the channel idle and the next
                # question in the channel must keep waiting for this one.
                noted = True
                self._tell("⏳ still running on the device; `/vigo stop` ends it.")
            if succeeded_at and now - succeeded_at > 3.0:
                outcome = "succeeded"
                break

            event = None
            if stream_dead:
                if now >= poll_at:
                    poll_at = now + 2.0
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
                if data.get("sessionID") == session:
                    kind = event.get("type", "")
                    self._note(event)
                    if kind == "permission.asked":
                        self._permission(session, data)
                    elif kind == "permission.replied":
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
        self.app.unregister_run(self.channel_id)
        log.info(f"ask: done on {self.device_name} ({outcome})")

    def _is_idle(self, session: str) -> bool:
        try:
            for message in self.client.messages(session, limit=6, order="desc"):
                if message.get("type") == "idle":
                    return True
        except OpenCodeError:
            pass
        return False
