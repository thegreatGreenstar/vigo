"""The bot: one gateway connection, one worker pool, one answer per question."""

from __future__ import annotations

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ask as asking
from . import commands, log, text
from .allowlist import AllowError, AllowList
from .config import Config
from .discord_api import Discord, DiscordError
from .gateway import Gateway
from . import __version__
from .opencode_api import OpenCode, OpenCodeError
from .peer import PeerClient, PeerServer, update_self
from .router import Router
from .state import State

INTENTS = 0  # no gateway events are needed: interactions arrive without intents


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


class App:
    def __init__(self, config: Config, api: Discord | None = None, state: State | None = None,
                 checkout: Path | None = None):
        self.checkout = checkout
        self.config = config
        self.api = api or Discord(config.token, config.app_id)
        self.router = Router(config)
        self.state = state or State()
        self.allow = AllowList(config, self.api, self.state)
        self.runs: dict[str, asking.Run] = {}
        self.runs_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="vigo")
        self.started = time.time()

    # -- runs -------------------------------------------------------------

    def register_run(self, channel: str, run: asking.Run) -> None:
        with self.runs_lock:
            self.runs[channel] = run

    def unregister_run(self, channel: str) -> None:
        with self.runs_lock:
            self.runs.pop(channel, None)

    def shutdown(self) -> None:
        """Stop every run and the worker pool; used on the way out and by tests."""
        with self.runs_lock:
            runs = list(self.runs.values())
        for run in runs:
            run.stop.set()
        self.pool.shutdown(wait=False)

    def run_for(self, channel: str) -> asking.Run | None:
        with self.runs_lock:
            return self.runs.get(channel)

    # -- permission cards -------------------------------------------------

    def ask_permission(self, *, device: str, session: str, request: str,
                       action: str, resources: list[str], channel: str) -> None:
        ticket = secrets.token_hex(4)
        self.state.new_ticket(ticket, device, session, request, channel)
        shown = "\n".join(f"> `{text.shorten(item, 180)}`" for item in resources if item) or "> (no path)"
        content = (
            f"**🔐 Permission needed** on `{device}`\n"
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
            return
        except DiscordError as error:
            # Never post a card in a channel: it names the action and the paths
            # it touches.  Say that approval is needed, and keep the rest here.
            log.error(f"perm: no DM with the owner ({error.message}); asking in the channel")
        self.state.close_ticket(ticket)
        self.api.send(channel, {
            "content": "🔐 A tool needs your approval, but I could not send you a DM.",
            "allowed_mentions": {"parse": []},
        })

    def _component(self, payload: dict, who: str) -> None:
        data = payload.get("data") or {}
        custom = str(data.get("custom_id") or "")
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
            commands.ASK, commands.QUICK, commands.UPGRADE, commands.STOP, commands.STATUS,
            commands.DEVICES, commands.ALLOW, commands.DENY, commands.ALLOWED,
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

        pick = self.router.pick(explicit)
        try:
            self._answer(sub, options, token, channel_id, channel_name, who, owner, pick)
        except Exception as error:  # an answer always reaches the person
            log.error(f"command {sub}: {error}")
            self._say(token, "❌ something broke; the log says what.")

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

    def _say(self, token: str, content: str) -> None:
        try:
            self.api.edit_original(token, {
                "content": content, "allowed_mentions": {"parse": []},
            })
        except DiscordError as error:
            log.error(f"cannot say it: {error.message}")

    # -- the commands -----------------------------------------------------

    def _answer(self, sub: str, options: dict, token: str, channel_id: str,
                channel_name: str, who: str, owner: bool, pick) -> None:
        if sub == commands.ASK:
            self._ask(options, token, channel_id, channel_name, who, pick)
        elif sub == commands.QUICK:
            self._quick(options, token, pick)
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
                "❌ no device would take the question.\n"
                "Start OpenCode there with `opencode serve --hostname 0.0.0.0 --port 4096`; "
                "`/vigo devices` (only you see it) says what is there.",
            )
            return
        if self.run_for(channel_id):
            self._say(
                token,
                "Something is already running in this channel; `/vigo stop` ends it.",
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
        )
        threading.Thread(target=worker.run_it, name=f"ask-{channel_id}", daemon=True).start()

    def _quick(self, options: dict, token: str, pick) -> None:
        """One question, answered once: no session, no tools, nothing kept."""
        prompt = str(options.get("prompt") or "").strip()
        if not prompt:
            self._say(token, "The question is empty.")
            return
        if pick.name is None:
            log.info(f"quick: no device ({pick.reason})")
            self._say(
                token,
                "❌ no device would take the question.\n"
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
            self._say(token, "❌ the device would not answer.")
            return
        chunks = text.chunk(answer) or ["_nothing to say_"]
        posted = 0
        for piece in chunks:
            try:
                self.api.followup(token, {
                    "content": piece, "allowed_mentions": {"parse": []},
                })
                posted += 1
            except DiscordError as error:
                log.error(f"quick: cannot post the answer ({error.message})")
                break
        if not posted:
            self._say(token, chunks[0])
            return
        self.api.delete_original(token)
        log.info(f"quick: answered on {pick.name}")

    def peer_password(self) -> str:
        """The password a peer sends to this device: this device's own.

        A peer looks this machine up as `devices[this].password`, so the server
        must check that same value — not the top-level fallback, which is empty
        when every device keeps its own secret.
        """
        own = self.config.device_named(self.config.device)
        return own.password if own else self.config.password

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

    def _behind(self) -> list[str]:
        """Daemons that answer on their peer port with a different build."""
        lines = []
        for name in self.config.devices:
            url = self._peer_url(name)
            if not url:
                continue
            device = self.config.device_named(name)
            try:
                info = PeerClient(url, device.password, timeout=1.5).info()
            except OSError:
                continue  # that port is off or unreachable; say nothing
            version = str(info.get("version") or "?")
            if version != __version__:
                lines.append(f"`{name}` runs vigo {version}, not {__version__} — `/vigo upgrade`")
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
        local = False
        for name in targets:
            if name == self.config.device:
                lines.append(f"`{name}`: this one pulls after this answer")
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
            if info.get("version") == __version__:
                lines.append(f"`{name}`: already on {__version__}")
                continue
            try:
                told = PeerClient(url, device.password, timeout=180.0).update()
            except OSError as error:
                lines.append(f"`{name}`: could not update it ({error})")
                continue
            if not told.get("ok"):
                lines.append(f"`{name}`: git refused ({told.get('said')})")
            elif told.get("restarted"):
                lines.append(f"`{name}`: pulled and restarted")
            else:
                lines.append(f"`{name}`: pulled, restart it there")
        self._say(token, "\n".join(lines) or "Nothing to upgrade.")
        # This one last, after the answer is out: pulling is safe, restarting
        # is only safe once the person has read the list above.
        if local:
            threading.Timer(1.0, lambda: update_self(self.checkout)).start()

    def _status(self, channel_id: str) -> str:
        remembered = self.state.session_for(channel_id)
        running = self.run_for(channel_id)
        if not remembered:
            return "No question has been asked in this channel yet."
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
                lines.append("⚠️ the device does not answer")
        if running:
            lines.append(f"⏳ running for {text.duration(running.elapsed)}")
        else:
            lines.append("idle")
        return "\n".join(lines)

    def _devices(self, pick, detailed: bool = False) -> str:
        states = self.router.states(fresh=True)
        lines = [self.router.describe(states, detailed=detailed)]
        behind = self._behind()
        if behind:
            lines.append("")
            lines.extend(f"⚠️ {line}" for line in behind)
        lines.append("")
        if pick.name:
            lines.append(
                f"A question would go to `{pick.name}`"
                + (f" — {pick.reason}." if detailed else ".")
            )
        elif detailed:
            lines.append(f"No device would take it: {pick.reason}.")
        else:
            lines.append("No device would take it.")
        return "\n".join(lines)

    def _stop(self, channel_id: str) -> str:
        run = self.run_for(channel_id)
        remembered = self.state.session_for(channel_id)
        if run:
            run.stop.set()
        if not remembered:
            return "Nothing is running in this channel." if not run else "Stopping it."
        device = self.config.device_named(remembered["device"])
        if device is None:
            return f"`{remembered['device']}` is not in the file any more."
        client = OpenCode(device.url, device.password)
        try:
            client.interrupt(remembered["session"])
            return "⏹️ Stopped."
        except OpenCodeError as error:
            if run:
                return "⏹️ Stopping the run in this channel."
            log.info(f"stop: nothing to stop ({error.message})")
            return "Nothing to stop."

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
            return f"⚠️ {error}"

    def _deny(self, options: dict) -> str:
        user = options.get("user")
        target = commands.user_id(user)
        if not target:
            return "Name the person."
        try:
            _done, said = self.allow.deny(target)
            return said
        except AllowError as error:
            return f"⚠️ {error}"

    def _help(self, owner: bool, approved: bool) -> str:
        lines = [
            "**Vigo** — an OpenCode agent on your own machines, reached from Discord.",
            "",
            "`/vigo ask prompt:…` asks the device you last worked on. "
            "`device:` names one, `new:true` starts a fresh session.",
            "`/vigo quick prompt:…` answers once, with no session and no tools.",
            "`/vigo devices` shows what is up and where a question would go.",
            "`/vigo status` shows this channel's session; `/vigo stop` ends its run.",
            "Every command that answers a question takes `hidden:true` to keep "
            "its answer to yourself; without it, the channel sees it. "
            "`devices`, `allow`, `deny` and `allowed` are always private.",
        ]
        if owner:
            lines.append("")
            lines.append(
                "Yours: `/vigo allow user:<who> for:<time>`, `/vigo deny user:<who>`, "
                "`/vigo allowed`. Tool permissions come to your DMs as buttons."
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
                           checkout=self.checkout).start()
            except OSError as error:  # the tailnet is not up, or the port is taken
                log.error(f"peer: not listening ({error})")
        gateway = Gateway(url, self.config.token, INTENTS, self.on_event,
                          lambda state: log.debug(f"gateway: {state}"))
        gateway.run()


def info_request(info: dict) -> str:
    return f"{info['device']} · {info['request']}"
