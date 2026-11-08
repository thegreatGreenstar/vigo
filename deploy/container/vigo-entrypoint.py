#!/usr/bin/env python3
"""Start one container of Vigo's pod.

`VIGO_ROLE=agent` starts OpenCode; `VIGO_ROLE=daemon` starts Vigo.  The two
containers share a network namespace but not a file system, so the Discord
token is mounted only in the daemon and the projects only in the agent.

Secrets arrive either as files under /run/secrets (Linux, systemd) or in the
environment (macOS, podman secrets).  They are never put on a command line.
"""

from __future__ import annotations

import os
import sys

PASSWORD_FILE = os.environ.get("VIGO_OPENCODE_PASSWORD_FILE", "/run/secrets/opencode-password")
GH_TOKEN_FILE = os.environ.get("VIGO_GH_TOKEN_FILE", "/run/secrets/gh-token")
PORT = os.environ.get("OPENCODE_PORT", "4096")
HOSTNAME = os.environ.get("OPENCODE_HOSTNAME", "0.0.0.0")


def log(text: str) -> None:
    sys.stderr.write(f"entrypoint: {text}\n")
    sys.stderr.flush()


def fail(text: str) -> None:
    log(text)
    raise SystemExit(1)


def read_secret(path: str, env: str, *, required: bool) -> str:
    """A file if it is there, otherwise the environment, otherwise ""."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        value = os.environ.get(env, "").strip()
    if not value and required:
        fail(f"no {env}: {path} is unreadable and the environment is empty")
    return value


def environment(password: str, gh_token: str) -> dict[str, str]:
    """The environment the agent gets: secrets in memory, not in argv."""
    env = dict(os.environ, OPENCODE_PASSWORD=password)
    if gh_token:
        env["GH_TOKEN"] = gh_token
    env.setdefault("GH_CONFIG_DIR", "/home/vigo/.config/gh")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    env.setdefault("GH_PROMPT_DISABLED", "1")
    return env


def main() -> int:
    role = os.environ.get("VIGO_ROLE", "")
    if role == "daemon":
        # The daemon holds no OpenCode credential of its own; its settings read
        # the files that are mounted.  Become the bot, with the environment the
        # launcher gave us.
        os.execv(sys.executable, [sys.executable, "-m", "vigo", *sys.argv[1:]])
    if role != "agent":
        fail("VIGO_ROLE must be 'agent' or 'daemon'")

    password = read_secret(PASSWORD_FILE, "VIGO_OPENCODE_PASSWORD", required=True)
    gh_token = read_secret(GH_TOKEN_FILE, "GH_TOKEN", required=False)
    if gh_token:
        log("GitHub CLI token is set")
    env = environment(password, gh_token)
    log(f"starting OpenCode on {HOSTNAME}:{PORT}")
    os.execvpe("opencode", ["opencode", "serve", "--hostname", HOSTNAME, "--port", PORT], env)


if __name__ == "__main__":
    sys.exit(main())
