"""Asking the other daemons what build they run, and telling them to update.

The daemons share no protocol and write nothing to Discord about themselves.
Each one serves two answers on a port of its own, behind the same password as
its OpenCode server:

    GET  /vigo/info     {"device": "fresh-device", "version": "1.0.0"}
    POST /vigo/update   git pull in this checkout; restart when a supervisor runs it

That port is off unless the settings name one (`peerPort`), because it must be
reachable from the other machine to be worth anything.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, httpc, log

CHECKOUT = Path(__file__).resolve().parent.parent


def _auth(password: str) -> str:
    return "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()


class PeerClient:
    """The other daemon, through the two answers it serves."""

    def __init__(self, base: str, password: str, timeout: float = 3.0) -> None:
        self.base = base.rstrip("/")
        self.password = password
        self.timeout = timeout

    def _call(self, method: str, path: str) -> dict:
        try:
            status, _, raw = httpc.call(
                f"{self.base}{path}", method,
                headers={"Authorization": _auth(self.password)},
                body=b"{}" if method == "POST" else None,
                timeout=self.timeout,
            )
        except httpc.HttpError as error:
            # Every caller asks "is that daemon there?" with `except OSError`:
            # a refused connection or a timeout is that answer, not a crash.
            raise OSError(str(error)) from error
        if not 200 <= status < 300:
            raise OSError(f"the daemon said {status}")
        parsed = httpc.parse_json(raw)
        return parsed if isinstance(parsed, dict) else {}

    def info(self) -> dict:
        return self._call("GET", "/vigo/info")

    def update(self) -> dict:
        return self._call("POST", "/vigo/update")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # the bot's own log is the log
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _allowed(self) -> bool:
        """Whether this request carries the password.

        The values are compared in constant time: a plain `==` answers faster
        when it fails at the first byte, which is a way to learn the password a
        byte at a time.  Both sides are bytes, because a header that is not
        ASCII would make `compare_digest` raise instead of refusing it.
        """
        given = self.headers.get("Authorization") or ""
        return hmac.compare_digest(given.encode("latin-1", "replace"),
                                   _auth(self.server.password).encode("ascii"))

    def do_GET(self) -> None:
        if not self._allowed():
            self._send(401, {"error": "password"})
            return
        if self.path.split("?")[0] != "/vigo/info":
            self._send(404, {"error": "no route"})
            return
        self._send(200, {"device": self.server.device,
                         "version": self.server.version or __version__,
                         "build": self.server.build,
                         "built": self.server.built,
                         "queue": self.server.queue()})

    def do_POST(self) -> None:
        if not self._allowed():
            self._send(401, {"error": "password"})
            return
        if self.path.split("?")[0] != "/vigo/update":
            self._send(404, {"error": "no route"})
            return
        self._send(200, update_self(checkout=self.server.checkout,
                                    restart=self.server.restart,
                                    before_restart=self.server.suspend))


def _run(command: list[str], cwd: Path, timeout: float = 120.0) -> tuple[int, str]:
    try:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    return done.returncode, (done.stdout or done.stderr or "").strip()


def _why(said: str) -> str:
    """The line of git's output that says why, without the paths around it.

    A refused `git pull` ends with advice, not with the cause: "Please make
    sure you have the correct access rights" and "and the repository exists."
    Taking the last line alone therefore reported "and the repository exists.",
    which says nothing.  The `fatal:` line is the one that names the cause, and
    the last of them is the summary ("Could not read from remote repository.")
    rather than one that quotes a local path.
    """
    fatal = [line.strip() for line in said.splitlines() if line.startswith("fatal:")]
    if fatal:
        return fatal[-1]
    lines = [line.strip() for line in said.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def build_of(checkout: Path | None = None) -> str:
    """The commit this checkout is on, short; empty when it is not a git tree.

    A version string is bumped by hand and both daemons run "1.0.0" between
    releases, so it cannot tell which of them is behind.  The commit can.
    """
    here = Path(checkout or CHECKOUT)
    code, said = _run(["git", "rev-parse", "--short", "HEAD"], here, timeout=10.0)
    return said.splitlines()[0].strip() if code == 0 and said else ""


def head_of(checkout: Path | None = None) -> str:
    """The newest commit in this checkout: short hash and subject; empty if not git."""
    here = Path(checkout or CHECKOUT)
    code, said = _run(["git", "log", "-1", "--format=%h %s"], here, timeout=10.0)
    return said.strip() if code == 0 and said else ""


def built_of(checkout: Path | None = None) -> int:
    """When this checkout's commit was made, in seconds; 0 when it is not git.

    A hash orders nothing and the version string is shared between releases,
    so the commit's own time is the one thing two daemons can compare.  A
    rebase writes a new time, and that is the point: the checkout was made
    again then, and the daemon that reads it starts from the newer code.
    """
    here = Path(checkout or CHECKOUT)
    code, said = _run(["git", "log", "-1", "--format=%ct"], here, timeout=10.0)
    try:
        return int(said.strip().splitlines()[0]) if code == 0 and said else 0
    except ValueError:
        return 0


def _launchd_workdir(printed: str) -> str:
    """The working directory a `launchctl print` names, or empty."""
    for line in printed.splitlines():
        line = line.strip()
        if line.startswith("working directory = "):
            return line.split("= ", 1)[1].strip()
    return ""


def _in_container() -> bool:
    """Whether this process runs in a container.

    Podman and Docker each leave their own mark, so nothing has to be set in
    the settings or in the environment.  A container has no systemd and no
    launchd inside it, which is why this is checked before either.
    """
    return any(Path(mark).exists() for mark in ("/run/.containerenv", "/.dockerenv"))


def _supervisor(checkout: Path, container: bool | None = None) -> list[str] | None:
    """The command that restarts this daemon, when a supervisor runs it here.

    systemd says so itself on Linux.  launchd says so for any job, so it is
    only this checkout's job when the working directory matches: a job that
    serves another clone is left alone.  A container has neither inside it, so
    the unit or agent that runs the container starts it again after this
    process ends.  `container` is None for the real check; a test gives a value.
    """
    here = checkout.resolve()
    in_container = _in_container() if container is None else container
    if in_container:
        # SIGKILL, because a signal with a default action is ignored by PID 1,
        # which the bot is when the image's entry point starts it.
        return ["sh", "-c", f"kill -KILL {os.getpid()}"]
    if _run(["systemctl", "--user", "is-active", "--quiet", "vigo.service"],
            here, timeout=10.0)[0] == 0:
        return ["systemctl", "--user", "restart", "vigo.service"]
    getuid = getattr(os, "getuid", None)
    if sys.platform != "darwin" or getuid is None:
        return None
    label = f"gui/{getuid()}/dev.vigo.bot"
    code, said = _run(["launchctl", "print", label], here, timeout=10.0)
    if code == 0 and _launchd_workdir(said) == str(here):
        return ["launchctl", "kickstart", "-k", label]
    return None


def _restart_later(command: list[str], here: Path,
                   before_restart: Callable[[], None] | None = None,
                   delay: float = 1.0) -> None:
    """Restart once the answer is out, because the restart kills this process.

    The work this restart will cut off is written down here, as late as it can
    be: a question that arrives during the second before the command runs is
    in the record too, and one that ended is not.
    """
    time.sleep(delay)
    if before_restart is not None:
        try:
            before_restart()
        except Exception as error:  # the restart must still happen
            log.failure("update: cannot write down the work this restart cuts off", error)
    code, said = _run(command, here, timeout=30.0)
    if code != 0:
        log.error(f"update: restart refused ({said or code})")


def update_self(checkout: Path | None = None, restart: bool = True,
                before_restart: Callable[[], None] | None = None) -> dict:
    """Pull this checkout, and restart through a supervisor when one runs it.

    `before_restart` runs one last time before the supervisor takes this
    process down.  Only an upgrade restarts a daemon, so it is also the one
    moment at which a run in flight is carried over a start instead of lost.
    """
    here = Path(checkout or CHECKOUT)
    before = build_of(here)
    code, said = _run(["git", "pull", "--ff-only"], here)
    if code != 0:
        why = _why(said) or f"git failed ({code})"
        log.info(f"update: git refused ({why})")
        if said:
            log.info("update: git said " + " ".join(said.split()))
        return {"ok": False, "said": why, "version": __version__, "before": before}
    command = _supervisor(here) if restart else None
    if command:
        threading.Thread(target=_restart_later,
                         args=(command, here, before_restart), daemon=True).start()
    after = build_of(here)
    moved = f"{before or '?'} -> {after or '?'}"
    log.info(f"update: pulled ({moved}; {'restarting' if command else 'restart it here'})")
    return {"ok": True, "said": _why(said) or "up to date",
            "version": __version__, "restarted": bool(command),
            "before": before, "after": after}


class PeerServer:
    def __init__(self, host: str, port: int, device: str, password: str,
                 checkout: Path | None = None, restart: bool = True,
                 version: str = "", build: str = "", built: int = 0,
                 queue: Callable[[], list] | None = None,
                 suspend: Callable[[], None] | None = None) -> None:
        self.server = ThreadingHTTPServer((host, port), _Handler)
        self.server.device = device  # type: ignore[attr-defined]
        self.server.password = password  # type: ignore[attr-defined]
        self.server.checkout = checkout  # type: ignore[attr-defined]
        self.server.restart = restart  # type: ignore[attr-defined]
        self.server.version = version  # type: ignore[attr-defined]
        self.server.build = build or build_of(checkout)  # type: ignore[attr-defined]
        self.server.built = built or built_of(checkout)  # type: ignore[attr-defined]
        # What this daemon runs and waits on, for `/vigo queue` on the other
        # one.  A daemon that holds nothing yet answers with an empty list.
        self.server.queue = queue or (lambda: [])  # type: ignore[attr-defined]
        # What this daemon's runs are, written down before an upgrade takes the
        # process down.  The other daemon asks for the update, so the record is
        # made here, by the daemon that owns the runs.
        self.server.suspend = suspend or (lambda: None)  # type: ignore[attr-defined]
        # A short poll: `shutdown()` waits for the loop to notice, and half a
        # second per server is half a second per test.
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True,
        )

    def start(self) -> PeerServer:
        self.thread.start()
        log.info(f"peer: listening on {self.server.server_address[0]}:{self.server.server_address[1]}")
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
