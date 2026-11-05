"""One HTTP call, no third-party client.

Returns the status and the bytes even when the status is an error, so the
caller can read what the server said.  Raises HttpError only when nothing was
answered.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request


class HttpError(Exception):
    """Nothing answered: a refused connection, a name that does not resolve, a timeout."""


def call(
    url: str,
    method: str = "GET",
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return answer.status, dict(answer.headers), answer.read()
    except urllib.error.HTTPError as error:
        try:
            payload = error.read()
        except OSError:
            payload = b""
        finally:
            error.close()
        return error.code, dict(error.headers or {}), payload
    except urllib.error.URLError as error:
        raise HttpError(str(error.reason)) from error
    except (socket.timeout, TimeoutError) as error:
        raise HttpError("timed out") from error
    except OSError as error:
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
