"""Asking the other daemons what build they run, and telling them to update.

The daemons share no protocol and write nothing to Discord about themselves.
Each one serves two answers on a port of its own, behind the same password as
its OpenCode server:

    GET  /vigo/info     {"device": "macbook", "version": "1.0.0"}
    POST /vigo/update   git pull in this checkout; restart when a supervisor runs it

That port is off unless the settings name one (`peerPort`), because it must be
reachable from the other machine to be worth anything.
"""

from __future__ import annotations

import base64
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, httpc, log

CHECKOUT = Path(__file__).resolve().parent.parent


def _auth(password: str) -> str:
    return "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()


class PeerClient:
    """The other daemon, through the two answers it serves."""

    def __init__(self, base: str, password: str, timeout: float = 3.0):
        self.base = base.rstrip("/")
        self.password = password
        self.timeout = timeout

    def _call(self, method: str, path: str) -> dict:
        status, _, raw = httpc.call(
            f"{self.base}{path}", method,
            headers={"Authorization": _auth(self.password)},
            body=b"{}" if method == "POST" else None,
            timeout=self.timeout,
        )
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

    def log_message(self, *args):  # the bot's own log is the log
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _allowed(self) -> bool:
        return self.headers.get("Authorization") == _auth(self.server.password)

    def do_GET(self) -> None:
        if not self._allowed():
            self._send(401, {"error": "password"})
            return
        if self.path.split("?")[0] != "/vigo/info":
            self._send(404, {"error": "no route"})
            return
        self._send(200, {"device": self.server.device,
                         "version": self.server.version or __version__,
                         "build": self.server.build})

    def do_POST(self) -> None:
        if not self._allowed():
            self._send(401, {"error": "password"})
            return
        if self.path.split("?")[0] != "/vigo/update":
            self._send(404, {"error": "no route"})
            return
        self._send(200, update_self(checkout=self.server.checkout,
                                    restart=self.server.restart))


def _run(command: list[str], cwd: Path, timeout: float = 120.0) -> tuple[int, str]:
    try:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    return done.returncode, (done.stdout or done.stderr or "").strip()


def build_of(checkout: Path | None = None) -> str:
    """The commit this checkout is on, short; empty when it is not a git tree.

    A version string is bumped by hand and both daemons run "1.0.0" between
    releases, so it cannot tell which of them is behind.  The commit can.
    """
    here = Path(checkout or CHECKOUT)
    code, said = _run(["git", "rev-parse", "--short", "HEAD"], here, timeout=10.0)
    return said.splitlines()[0].strip() if code == 0 and said else ""


def update_self(checkout: Path | None = None, restart: bool = True) -> dict:
    """Pull this checkout, and restart through a supervisor when one runs it."""
    here = Path(checkout or CHECKOUT)
    code, said = _run(["git", "pull", "--ff-only"], here)
    if code != 0:
        log.info(f"update: git refused ({said.splitlines()[-1] if said else code})")
        return {"ok": False, "said": said.splitlines()[-1] if said else "git failed",
                "version": __version__}
    restarted = False
    if restart and _run(["systemctl", "--user", "is-active", "--quiet", "vigo.service"], here)[0] == 0:
        restarted = _run(["systemctl", "--user", "restart", "vigo.service"], here)[0] == 0
    log.info(f"update: pulled ({'restarting' if restarted else 'restart it here'})")
    return {"ok": True, "said": said.splitlines()[-1] if said else "up to date",
            "version": __version__, "restarted": restarted}


class PeerServer:
    def __init__(self, host: str, port: int, device: str, password: str,
                 checkout: Path | None = None, restart: bool = True,
                 version: str = "", build: str = ""):
        self.server = ThreadingHTTPServer((host, port), _Handler)
        self.server.device = device  # type: ignore[attr-defined]
        self.server.password = password  # type: ignore[attr-defined]
        self.server.checkout = checkout  # type: ignore[attr-defined]
        self.server.restart = restart  # type: ignore[attr-defined]
        self.server.version = version  # type: ignore[attr-defined]
        self.server.build = build or build_of(checkout)  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> PeerServer:
        self.thread.start()
        log.info(f"peer: listening on {self.server.server_address[0]}:{self.server.server_address[1]}")
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
