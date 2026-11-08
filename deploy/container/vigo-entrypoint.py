#!/usr/bin/env python3
"""Run Vigo and its own OpenCode in one container, and stop when either stops.

The password is read from the file systemd mounted at /run/secrets and put in
OpenCode's environment, never on its command line.  Vigo reads the same file
through its settings' `passwordCommand`.

If either process exits, the other is stopped and the container exits with a
non-zero status where it can: systemd on the host restarts the container.  When
`/vigo upgrade` pulls a new checkout, Vigo exits on purpose and this is the path
that brings it back.
"""

from __future__ import annotations

import base64
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

PASSWORD_FILE = os.environ.get("VIGO_OPENCODE_PASSWORD_FILE", "/run/secrets/opencode-password")
GH_TOKEN_FILE = os.environ.get("VIGO_GH_TOKEN_FILE", "/run/secrets/gh-token")
CHECKOUT = os.environ.get("VIGO_CHECKOUT", "/vigo")
PORT = os.environ.get("OPENCODE_PORT", "4096")
HOSTNAME = os.environ.get("OPENCODE_HOSTNAME", "0.0.0.0")
STARTUP_TIMEOUT = float(os.environ.get("VIGO_OPENCODE_TIMEOUT", "90"))


def log(text: str) -> None:
    sys.stderr.write(f"entrypoint: {text}\n")
    sys.stderr.flush()


def fail(text: str) -> None:
    log(text)
    raise SystemExit(1)


def read_secret(path: str, *, required: bool) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError as error:
        if required:
            fail(f"{path} cannot be read ({error.strerror})")
        return ""
    if not value and required:
        fail(f"{path} is empty")
    return value


def password() -> str:
    return read_secret(PASSWORD_FILE, required=True)


def environment(secret: str, gh_token: str) -> dict[str, str]:
    """The environment both children get: secrets in memory, not in argv."""
    env = dict(os.environ, OPENCODE_PASSWORD=secret)
    if gh_token:
        env["GH_TOKEN"] = gh_token
    env.setdefault("GH_CONFIG_DIR", "/home/vigo/.config/gh")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("GH_PROMPT_DISABLED", "1")
    return env


def auth(secret: str) -> str:
    return "Basic " + base64.b64encode(f"opencode:{secret}".encode()).decode()


def wait_for_opencode(process: subprocess.Popen, secret: str) -> None:
    """Wait until the server answers on loopback, so Vigo starts after it."""
    deadline = time.monotonic() + STARTUP_TIMEOUT
    url = f"http://127.0.0.1:{PORT}/api/info"
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            fail(f"OpenCode exited before it answered ({code})")
        request = urllib.request.Request(url, headers={"Authorization": auth(secret)})
        try:
            with urllib.request.urlopen(request, timeout=2) as reply:
                if 200 <= reply.status < 300:
                    return
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            pass
        time.sleep(0.5)
    fail(f"OpenCode did not answer on port {PORT} within {STARTUP_TIMEOUT:g}s")


def leave(children: list[subprocess.Popen], status: int) -> int:
    """Stop both children, then return the status the container should carry."""
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 10.0
    for child in children:
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    return status


def exit_status(code: int) -> int:
    # A child killed by a signal reports -N; the shell convention is 128+N.
    return 128 - code if code < 0 else code


def main() -> int:
    secret = password()
    gh_token = read_secret(GH_TOKEN_FILE, required=False)
    if gh_token:
        log("GitHub CLI token is set")
    env = environment(secret, gh_token)

    opencode = subprocess.Popen(
        ["opencode", "serve", "--hostname", HOSTNAME, "--port", PORT],
        env=env,
    )
    wait_for_opencode(opencode, secret)
    log(f"OpenCode is up on {HOSTNAME}:{PORT}")

    vigo = subprocess.Popen(
        [sys.executable, "-m", "vigo"],
        cwd=CHECKOUT,
        env=env,
    )
    children = [vigo, opencode]
    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, stop)

    while True:
        if stopping:
            log("asked to stop")
            return leave(children, 0)
        for name, child in (("vigo", vigo), ("opencode", opencode)):
            code = child.poll()
            if code is None:
                continue
            log(f"{name} exited ({code})")
            # OpenCode going away is never a clean end; a zero status would
            # hide it from a Restart=on-failure unit.
            status = exit_status(code)
            if name == "opencode" and status == 0:
                status = 1
            return leave(children, status)
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main())
