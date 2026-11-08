"""One HTTP call, no third-party client.

Returns the status and the bytes even when the status is an error, so the
caller can read what the server said.  Raises HttpError only when nothing was
answered.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import log

SLOW = 10.0  # seconds before a call that answered is also said at info


class HttpError(Exception):
    """Nothing answered: a refused connection, a name that does not resolve, a timeout."""


def _where(url: str) -> str:
    """The host of a call: enough to say which server, with no path or token."""
    return urllib.parse.urlsplit(url).netloc or url


def _note(method: str, url: str, status: int, started: float) -> None:
    elapsed = time.monotonic() - started
    if elapsed >= SLOW:
        log.info(f"http: {method} {_where(url)} took {elapsed:.1f}s and said {status}")
    else:
        log.debug(f"http: {method} {_where(url)} said {status} in {elapsed * 1000:.0f}ms")


def _failed(method: str, url: str, started: float, reason: object) -> None:
    log.debug(f"http: {method} {_where(url)} failed after "
              f"{time.monotonic() - started:.3f}s ({reason})")


def call(
    url: str,
    method: str = "GET",
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict[str, str], bytes]:
    started = time.monotonic()
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            status, headers_out, raw = answer.status, dict(answer.headers), answer.read()
        _note(method, url, status, started)
        return status, headers_out, raw
    except urllib.error.HTTPError as error:
        try:
            payload = error.read()
        except OSError:
            payload = b""
        finally:
            error.close()
        _note(method, url, error.code, started)
        return error.code, dict(error.headers or {}), payload
    except urllib.error.URLError as error:
        _failed(method, url, started, error.reason)
        raise HttpError(str(error.reason)) from error
    except TimeoutError as error:
        _failed(method, url, started, "timed out")
        raise HttpError("timed out") from error
    except OSError as error:
        _failed(method, url, started, error.strerror or error)
        raise HttpError(error.strerror or str(error)) from error


def json_body(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def parse_json(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
