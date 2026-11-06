"""Shared helpers: a settings file, and a bot wired to the fakes."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import time
from pathlib import Path

from vigo import config as configuration
from vigo import ask as rules


def base_settings(**overrides) -> dict:
    settings = {
        "token": "test-token",
        "appID": "111111111111111111",
        "owner": "145512144145512144",
        "device": "macbook",
        "password": "pw",
        "users": [],
        "model": {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"},
        "devices": [
            {"name": "macbook", "url": "http://127.0.0.1:1", "directory": "/tmp/project"},
        ],
        "activityWindow": 1800,
        "timeout": 60,
        "editEvery": 1,
    }
    settings.update(overrides)
    return settings


def write_settings(directory: Path, settings: dict) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "vigo.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle)
    os.chmod(path, 0o600)
    return path


def question_of(prompt: str) -> str:
    """What the device was asked, without the audience, self and rules lines.

    A run sends what Vigo is, who reads the answer, the question, and the
    subagent rule.  A test that checks the question wants that one part.
    """
    body = prompt
    if body.startswith("[Discord] "):
        body = body.split("\n\n", 1)[1]
    if body.startswith(rules.SELF):
        body = body[len(rules.SELF):].lstrip("\n")
    if body.endswith(rules.RULES):
        body = body[: -len(rules.RULES)]
    return body.rstrip("\n")


def load_settings(directory: Path, **overrides) -> configuration.Config:
    return configuration.load(write_settings(directory, base_settings(**overrides)))


class TempDir:
    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory()

    @property
    def path(self) -> Path:
        return Path(self._dir.name)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *args) -> None:
        # A run can still be writing the state file as the test ends. Windows
        # refuses to remove an open file (WinError 32, a PermissionError), and
        # a directory that refills between the walk and the rmdir is ENOTEMPTY.
        # Retry briefly, then raise if it never clears.
        for _ in range(100):
            try:
                self._dir.cleanup()
                return
            except PermissionError:
                time.sleep(0.01)
            except OSError as error:
                if error.errno != errno.ENOTEMPTY:
                    raise
                time.sleep(0.01)
        self._dir.cleanup()
