"""One HTTP call, no third-party client.

Returns the status and the bytes even when the status is an error, so the
caller can read what the server said.  Raises HttpError only when nothing was
answered.

A redirect is not followed.  `urllib` would send the same headers to whatever
host the 3xx names, `Authorization` included, so a server that answered for
Discord could hand the bot's token to its own host.  No address this bot calls
has a redirect to follow, so a 3xx comes back as the status it is and the
caller fails with a number.

An answer has a bound as well.  Discord, OpenCode and a peer each answer with
what their API holds, and a host that answers with a stream of bytes must not
make the daemon allocate memory no message needs.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import log

SLOW = 10.0  # seconds before a call that answered is also said at info
MOST_BYTES = 16 * 1024 * 1024  # the most one answer may hold


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back to the caller instead of following it.

    Returning None makes `urllib` raise the `HTTPError` it raised for the
    status, so `call` answers with that status and nothing is sent twice.
    """

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int,
                         msg: str, headers: Any, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_url(request: urllib.request.Request, timeout: float) -> Any:
    """Open one request with no redirect followed, for a call or the event stream."""
    return _OPENER.open(request, timeout=timeout)


class HttpError(Exception):
    """Nothing answered: a refused connection, a name that does not resolve, a timeout."""


def _read_bound(answer: Any) -> bytes:
    """Read one answer, and refuse one past the bound instead of holding it."""
    body = answer.read(MOST_BYTES + 1)
    if len(body) > MOST_BYTES:
        raise HttpError(f"the answer is larger than {MOST_BYTES} bytes")
    return body


def _where(url: str) -> str:
    """The host of a call: enough to say which server, with no path or token.

    A URL may carry a user and a password, and the log is not the place for
    either: the netloc is cut at its `@` so a configured credential stays out.
    """
    netloc = urllib.parse.urlsplit(url).netloc or url
    return netloc.rsplit("@", 1)[-1]


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
        with open_url(request, timeout) as answer:
            status, headers_out, raw = answer.status, dict(answer.headers), _read_bound(answer)
        _note(method, url, status, started)
        return status, headers_out, raw
    except urllib.error.HTTPError as error:
        try:
            # An error's own words are read for the caller, but the caller
            # never needs more than the bound either.
            payload = error.read(MOST_BYTES)
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
