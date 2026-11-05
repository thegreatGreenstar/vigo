"""The bot's settings: one JSON file, checked strictly.

A key the bot does not know stops it, naming the key, as does a value that is
not what its key takes.  Nothing here talks to the network.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SNOWFLAKE = re.compile(r"\A[0-9]{15,20}\Z")
NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")

KNOWN_KEYS = {
    "token",
    "tokenFile",
    "tokenCommand",
    "appID",
    "owner",
    "device",
    "password",
    "passwordFile",
    "passwordCommand",
    "install",
    "stateChannel",
    "users",
    "model",
    "devices",
    "activityWindow",
    "timeout",
    "editEvery",
    "peerPort",
    "peerHost",
}

KNOWN_DEVICE_KEYS = {"name", "url", "directory", "password", "passwordFile",
                     "passwordCommand", "peer"}


class ConfigError(Exception):
    """The settings cannot be used, and the message says why."""


@dataclass(frozen=True)
class Device:
    name: str
    url: str
    directory: str
    password: str = ""
    peer: str = ""

    def __repr__(self) -> str:
        return f"Device({self.name})"


@dataclass(frozen=True)
class Model:
    provider_id: str
    id: str
    variant: str = ""

    def as_ref(self) -> dict:
        ref = {"providerID": self.provider_id, "id": self.id}
        if self.variant:
            ref["variant"] = self.variant
        return ref

    def __str__(self) -> str:
        tail = f"#{self.variant}" if self.variant else ""
        return f"{self.provider_id}/{self.id}{tail}"


@dataclass(frozen=True)
class Config:
    token: str
    app_id: str
    owner: str
    device: str
    password: str
    devices: dict[str, Device]
    users: tuple[str, ...] = ()
    state_channel: str = ""
    install: str = "both"
    model: Model = field(default_factory=lambda: Model("opencode-go", "deepseek-v4.1-flash"))
    activity_window: int = 1800
    timeout: int = 900
    edit_every: float = 3.0
    peer_port: int = 0
    peer_host: str = "127.0.0.1"

    def device_named(self, name: str) -> Device | None:
        return self.devices.get(name)

    @property
    def own(self) -> Device:
        return self.devices[self.device]


def default_path() -> Path:
    override = os.environ.get("VIGO_CONFIG")
    if override:
        return Path(os.path.expanduser(override))
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "vigo" / "vigo.json"


def _need(obj: dict, key: str, where: str):
    if key not in obj:
        raise ConfigError(f"{where} needs the key {key!r}")
    return obj[key]


def _str(obj: dict, key: str, where: str, allow_empty: bool = False) -> str:
    value = _need(obj, key, where)
    if not isinstance(value, str):
        raise ConfigError(f"{where}: {key!r} is a {type(value).__name__}, not a string")
    if not allow_empty and not value.strip():
        raise ConfigError(f"{where}: {key!r} is empty")
    return value


def _whole(obj: dict, key: str, where: str, low: int, high: int, default: int) -> int:
    if key not in obj:
        return default
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: {key!r} is {value!r}, not a whole number")
    if not low <= value <= high:
        raise ConfigError(f"{where}: {key!r} is {value}, outside {low} to {high}")
    return value


def _snowflake(obj: dict, key: str, where: str) -> str:
    value = _str(obj, key, where)
    if not SNOWFLAKE.match(value):
        raise ConfigError(f"{where}: {key!r} is {value!r}, not a Discord id")
    return value


def _check_mode(path: Path) -> None:
    if os.name == "nt":  # Windows has ACLs instead of these bits
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise ConfigError(f"{path}: cannot be read ({error.strerror})") from error
    if mode & 0o077:
        raise ConfigError(
            f"{path}: mode is {mode:04o}; a secret in it needs 0600 or stricter"
        )


def _from_file(path_text: str, where: str, base: Path | None = None) -> str:
    path = Path(os.path.expanduser(path_text))
    if not path.is_absolute() and base is not None:
        path = base / path
    _check_mode(path)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigError(f"{where}: {path} cannot be read ({error.strerror})") from error
    if not text:
        raise ConfigError(f"{where}: {path} is empty")
    return text


def _from_command(command: str, where: str) -> str:
    try:
        completed = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigError(f"{where}: the command did not run ({error})") from error
    if completed.returncode != 0:
        reason = (completed.stderr or "").strip().splitlines()
        detail = reason[-1] if reason else f"exit {completed.returncode}"
        raise ConfigError(f"{where}: the command failed ({detail})")
    text = completed.stdout.strip()
    if not text:
        raise ConfigError(f"{where}: the command printed nothing")
    return text


def _secret(raw: dict, key: str, where: str, base: Path | None = None) -> str:
    """A secret, given plainly, read from a file, or asked of a command.

    The file or the command keeps the token out of the settings file, which is
    what a Keychain item or a TPM-sealed file needs.  A relative path is taken
    from the settings file's own folder.
    """
    names = {
        key: lambda: _str(raw, key, where),
        f"{key}File": lambda: _from_file(_str(raw, key + "File", where),
                                        f"{where}: {key}", base),
        f"{key}Command": lambda: _from_command(_str(raw, key + "Command", where),
                                               f"{where}: {key}"),
    }
    given = [name for name in names if name in raw]
    if len(given) > 1:
        raise ConfigError(f"{where}: {given[0]!r} and {given[1]!r} both name {key!r}; pick one")
    if not given:
        return ""
    value = names[given[0]]()
    if value.startswith(("PASTE-", "PASTE_", "<")):
        raise ConfigError(
            f"{where}: {key!r} still holds the example's placeholder {value!r}; "
            "put the real value in, or use "
            f"{key}File/{key}Command"
        )
    return value


def _device(entry: object, index: int, fallback_password: str, base: Path | None) -> Device:
    where = f"devices[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} is a {type(entry).__name__}, not an object")
    unknown = set(entry) - KNOWN_DEVICE_KEYS
    if unknown:
        raise ConfigError(f"{where} has the unknown key {sorted(unknown)[0]!r}")
    name = _str(entry, "name", where)
    if not NAME.match(name):
        raise ConfigError(f"{where}: name {name!r} takes letters, digits, '.', '_' and '-'")
    url = _str(entry, "url", where).rstrip("/")
    if not re.match(r"\Ahttps?://[^\s/]+", url):
        raise ConfigError(f"{where}: url {url!r} is not http:// or https://")
    directory = _str(entry, "directory", where)
    if not (directory.startswith("/") or re.match(r"\A[A-Za-z]:[\\/]", directory)):
        raise ConfigError(f"{where}: directory {directory!r} is not an absolute path")
    password = _secret(entry, "password", where, base) or fallback_password
    if not password:
        raise ConfigError(f"{where}: no password, and the file sets none")
    peer = ""
    if "peer" in entry:
        peer = _str(entry, "peer", where).rstrip("/")
        if not re.match(r"\Ahttps?://[^\s/]+", peer):
            raise ConfigError(f"{where}: peer {peer!r} is not http:// or https://")
    return Device(name=name, url=url, directory=directory, password=password, peer=peer)


def _model(raw: object) -> Model:
    if raw is None:
        return Model("opencode-go", "deepseek-v4.1-flash")
    if not isinstance(raw, dict):
        raise ConfigError("'model' is not an object")
    unknown = set(raw) - {"providerID", "id", "variant"}
    if unknown:
        raise ConfigError(f"'model' has the unknown key {sorted(unknown)[0]!r}")
    provider = _str(raw, "providerID", "model")
    ident = _str(raw, "id", "model")
    variant = raw.get("variant", "")
    if not isinstance(variant, str):
        raise ConfigError("'model': 'variant' is not a string")
    return Model(provider, ident, variant)


INSTALLS = ("both", "user", "guild")


def _install(raw: dict) -> str:
    if "install" not in raw:
        return "both"
    value = raw["install"]
    if not isinstance(value, str) or value not in INSTALLS:
        raise ConfigError(
            f"the file: 'install' is {value!r}, not one of "
            + ", ".join(repr(name) for name in INSTALLS)
        )
    return value


def parse(raw: object, base: Path | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("the file is not a JSON object")
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        raise ConfigError(f"the unknown key {sorted(unknown)[0]!r}")

    token = _secret(raw, "token", "the file", base)
    if not token:
        raise ConfigError("the file needs 'token' (or 'tokenFile', or 'tokenCommand')")
    app_id = _snowflake(raw, "appID", "the file")
    owner = _snowflake(raw, "owner", "the file")
    password = _secret(raw, "password", "the file", base)
    device_name = _str(raw, "device", "the file")

    if "devices" not in raw:
        raise ConfigError("the file needs the key 'devices'")
    listed = raw["devices"]
    if not isinstance(listed, list) or not listed:
        raise ConfigError("'devices' is not a list with at least one device")
    devices: dict[str, Device] = {}
    for index, entry in enumerate(listed):
        device = _device(entry, index, password, base)
        if device.name in devices:
            raise ConfigError(f"devices: {device.name!r} is named twice")
        devices[device.name] = device
    if device_name not in devices:
        raise ConfigError(
            f"'device' is {device_name!r}, which 'devices' does not name "
            f"({', '.join(sorted(devices))})"
        )

    users: list[str] = []
    if "users" in raw:
        listed_users = raw["users"]
        if not isinstance(listed_users, list):
            raise ConfigError("'users' is not a list")
        for index, user in enumerate(listed_users):
            if not isinstance(user, str) or not SNOWFLAKE.match(user):
                raise ConfigError(f"users[{index}] is {user!r}, not a Discord id")
            if user == owner:
                raise ConfigError("'users' lists the owner, who is always let through")
            if user in users:
                raise ConfigError(f"'users' lists {user} twice")
            users.append(user)

    state_channel = ""
    if "stateChannel" in raw:
        state_channel = _snowflake(raw, "stateChannel", "the file")

    return Config(
        token=token,
        app_id=app_id,
        owner=owner,
        device=device_name,
        password=password,
        devices=devices,
        users=tuple(users),
        state_channel=state_channel,
        install=_install(raw),
        model=_model(raw.get("model")),
        activity_window=_whole(raw, "activityWindow", "the file", 0, 86400, 1800),
        timeout=_whole(raw, "timeout", "the file", 30, 3600, 900),
        edit_every=float(_whole(raw, "editEvery", "the file", 1, 60, 3)),
        peer_port=_whole(raw, "peerPort", "the file", 0, 65535, 0),
        peer_host=_str(raw, "peerHost", "the file") if "peerHost" in raw else "127.0.0.1",
    )


def load(path: Path | None = None) -> Config:
    path = Path(path or default_path())
    _check_mode(path)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path}: not JSON ({error.msg} at line {error.lineno})") from error
    except OSError as error:
        raise ConfigError(f"{path}: cannot be read ({error.strerror})") from error
    try:
        return parse(raw, base=path.parent)
    except ConfigError as error:
        raise ConfigError(f"{path}: {error}") from error
