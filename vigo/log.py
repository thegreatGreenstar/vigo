"""stderr only: what ran or failed, never what was said.

The bot never writes a message's text, a prompt or an answer to the log.  What
it writes is a line per event, with ids and outcomes.
"""

from __future__ import annotations

import os
import sys
import time

_LEVELS = {"quiet": 0, "error": 1, "info": 2, "debug": 3}
_level = _LEVELS.get(os.environ.get("VIGO_LOG", "info"), 2)


def level(name: str) -> None:
    global _level
    _level = _LEVELS.get(name, 2)


def _write(tag: str, text: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    sys.stderr.write(f"{stamp} {tag} {text}\n")
    sys.stderr.flush()


def error(text: str) -> None:
    if _level >= 1:
        _write("error", text)


def info(text: str) -> None:
    if _level >= 2:
        _write("info ", text)


def debug(text: str) -> None:
    if _level >= 3:
        _write("debug", text)
