"""stderr only: what ran or failed, never what was said.

The bot never writes a message's text, a prompt or an answer to the log.  What
it writes is a line per event, with ids and outcomes.

A line carries the date and its millisecond, the seconds since the process
started, the level, and the thread that wrote it.  A hang writes no event at
all, so the clock and the thread name are what separates a daemon that is
silent because it is stuck from one that is merely idle.  `failure` writes
the stack of an error, and `install` gives every uncaught error of the process
the same line and stack, on the main thread or on any other.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from typing import Any

_LEVELS = {"quiet": 0, "error": 1, "info": 2, "debug": 3}
_level = _LEVELS.get(os.environ.get("VIGO_LOG", "info"), 2)
_started = time.monotonic()
_lock = threading.Lock()
_stream: Any = None   # where the lines go; None is stderr


def attach(stream: Any) -> None:
    """Send the lines to `stream` instead of stderr; None puts stderr back.

    The tests use this to read a line without taking `sys.stderr` from the
    threads that write beside them.
    """
    global _stream
    _stream = stream


def level(name: str) -> None:
    global _level
    _level = _LEVELS.get(name, 2)


def _stamp() -> str:
    now = time.time()
    millis = int((now % 1) * 1000)
    return (f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}.{millis:03d} "
            f"+{time.monotonic() - _started:9.3f}s")


def _write(tag: str, text: str) -> None:
    """One block from one thread, so two writes never interleave their lines."""
    lines = text.splitlines() or [""]
    with _lock:
        stream = _stream or sys.stderr
        if stream is None:
            return
        try:
            stream.write(f"{_stamp()} {tag} [{threading.current_thread().name}] {lines[0]}\n")
            for line in lines[1:]:
                stream.write(f"    {line}\n")
            stream.flush()
        except (OSError, ValueError):
            # A closed pipe must not take the bot down, and it cannot be told.
            pass


def error(text: str) -> None:
    if _level >= 1:
        _write("error", text)


def info(text: str) -> None:
    if _level >= 2:
        _write("info ", text)


def debug(text: str) -> None:
    if _level >= 3:
        _write("debug", text)


def failure(text: str, error: BaseException, tb: object | None = None) -> None:
    """An error and the stack that led to it, as one block.

    `error` is given rather than taken from `sys.exc_info`, because a future
    carries its exception to a callback after the `except` block has ended.
    """
    if _level < 1:
        return
    frames = "".join(traceback.format_exception(
        type(error), error, tb if tb is not None else error.__traceback__,
    ))
    _write("error", text + "\n" + frames.rstrip())


def exception(text: str) -> None:
    """An error inside an `except` block: the same as `failure` with the live one."""
    error = sys.exc_info()[1]
    if isinstance(error, BaseException):
        failure(text, error)
    elif _level >= 1:
        _write("error", text)


def install() -> None:
    """Send the uncaught errors of the process, its threads and its callbacks here.

    The default handlers write a bare trace with no date and no thread, and a
    callback that raises is reported by nobody.  All three are what a crash of
    the daemon leaves behind, so all three reach the same line.
    """
    sys.excepthook = _uncaught
    threading.excepthook = _uncaught_thread  # type: ignore[assignment]
    sys.unraisablehook = _unraisable
    # A fatal fault of the interpreter or a native library writes the stacks of
    # every thread instead of dying with no bytes at all.
    faulthandler.enable()


def _uncaught(kind: type[BaseException], value: BaseException, tb: object) -> None:
    failure(f"uncaught: {kind.__name__}: {value}", value, tb)


def _uncaught_thread(args: threading.ExceptHookArgs) -> None:
    thread = args.thread.name if args.thread is not None else "a thread"
    failure(f"uncaught in {thread}: {args.exc_type.__name__}: {args.exc_value}",
            args.exc_value, args.exc_traceback)


def _unraisable(args: Any) -> None:
    error = args.exc_value
    if not isinstance(error, BaseException):
        error = RuntimeError(args.err_msg or "unraisable")
    failure(f"unraisable in {getattr(args.object, '__qualname__', args.object)!r}: {error}",
            error, args.exc_traceback)
