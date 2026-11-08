"""Which device answers a question.

The rule: the device where a person last typed in OpenCode, because that is
where they are sitting.  Otherwise the first device that answers at all.  A
command may name a device, and then nothing is guessed.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import log
from .config import Config, Device
from .opencode_api import Health, OpenCode

ACTIVITY_SESSIONS = 4  # how many recent sessions to look at for a person's last message


@dataclass
class DeviceState:
    device: Device
    health: Health | None = None
    activity_ms: float = 0.0
    error: str = ""
    probed_at: float = 0.0

    @property
    def up(self) -> bool:
        return self.health is not None

    def activity_age(self) -> float | None:
        if not self.activity_ms:
            return None
        return max(0.0, time.time() - self.activity_ms / 1000.0)


@dataclass
class Pick:
    name: str | None
    reason: str
    states: dict[str, DeviceState] = field(default_factory=dict)

    @property
    def device(self) -> Device | None:
        return self.states[self.name].device if self.name else None

    def client(self, password: str) -> OpenCode | None:
        if not self.name:
            return None
        device = self.states[self.name].device
        return OpenCode(device.url, device.password or password)


class Router:
    def __init__(self, config: Config, probe_timeout: float = 2.5,
                 cache: float = 8.0) -> None:
        self.config = config
        self.probe_timeout = probe_timeout
        self.cache_seconds = cache
        self._cache: dict[str, DeviceState] = {}
        self._cache_at = 0.0

    def states(self, fresh: bool = False) -> dict[str, DeviceState]:
        now = time.monotonic()
        if not fresh and self._cache and now - self._cache_at < self.cache_seconds:
            return {name: state for name, state in self._cache.items()}
        devices = list(self.config.devices.values())
        result: dict[str, DeviceState] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(devices))) as pool:
            futures = {pool.submit(self._probe, device): device for device in devices}
            for future, device in futures.items():
                try:
                    result[device.name] = future.result()
                except Exception as error:  # a probe never takes the router down
                    result[device.name] = DeviceState(device=device, error=str(error))
        self._cache = result
        self._cache_at = now
        return result

    def _probe(self, device: Device) -> DeviceState:
        state = DeviceState(device=device, probed_at=time.time())
        client = OpenCode(device.url, device.password, timeout=self.probe_timeout)
        started = time.monotonic()
        try:
            state.health = client.info()
        except Exception as error:
            state.error = str(error)
            return state
        try:
            sessions = client.sessions(limit=ACTIVITY_SESSIONS)
            newest = 0.0
            for session in sessions:
                if time.monotonic() - started > self.probe_timeout * 2:
                    break
                try:
                    newest = max(newest, client.user_activity(session["id"]))
                except Exception:
                    continue
            state.activity_ms = newest
        except Exception as error:
            log.debug(f"router: {device.name}: no activity ({error})")
        return state

    def pick(self, explicit: str | None = None, fresh: bool = False) -> Pick:
        states = self.states(fresh=fresh)
        if explicit:
            if explicit not in states:
                return Pick(None, f"no device is called {explicit}", states)
            if not states[explicit].up:
                reason = states[explicit].error or "it does not answer"
                return Pick(None, f"{explicit} is down: {reason}", states)
            return Pick(explicit, "you named it", states)

        alive = [state for state in states.values() if state.up]
        if not alive:
            return Pick(None, "no device answers", states)
        if len(alive) == 1:
            return Pick(alive[0].device.name, "the only device up", states)

        now_ms = time.time() * 1000.0
        window_ms = self.config.activity_window * 1000.0
        recent = [
            state for state in alive if state.activity_ms and now_ms - state.activity_ms <= window_ms
        ]
        if recent:
            chosen = max(
                recent,
                key=lambda state: (state.activity_ms, -list(states).index(state.device.name)),
            )
            age = chosen.activity_age() or 0.0
            minutes = int(age // 60)
            ago = f"{minutes}m ago" if minutes else f"{int(age)}s ago"
            return Pick(chosen.device.name, f"you last used it {ago}", states)
        first = alive[0]
        return Pick(first.device.name, "the first device up", states)

    def describe(self, states: dict[str, DeviceState], detailed: bool = False,
                 builds: dict[str, str] | None = None) -> str:
        """The devices, in as much detail as the reader is allowed to see.

        Hostnames, versions, paths and error text identify a machine and a
        person, so they appear only when the answer is hidden from the channel.
        `builds` maps a device name to the vigo version it runs, when known.
        """
        lines = []
        for name, state in states.items():
            if state.up and state.health:
                if not detailed:
                    lines.append(f"`{name}` **up**")
                    continue
                age = state.activity_age()
                if age is None:
                    used = "no session yet"
                elif age < 90:
                    used = "in use now"
                elif age < 3600:
                    used = f"used {int(age // 60)}m ago"
                else:
                    used = f"used {int(age // 3600)}h ago"
                lines.append(
                    f"`{name}` **up**, {state.health.hostname}, "
                    f"OpenCode {state.health.version}, {used}"
                    + (f", {builds[name]}" if builds and name in builds else "")
                    + f" · {state.device.directory}"
                )
            elif detailed:
                lines.append(f"`{name}` **down**, {state.error or 'no answer'}")
            else:
                lines.append(f"`{name}` **down**")
        return "\n".join(lines)
