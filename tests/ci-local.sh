#!/bin/sh
# Run what CI runs, as far as this machine can, before pushing.
#
#   tests/ci-local.sh                   # everything this machine can answer
#   tests/ci-local.sh --no-containers   # skip the Linux matrix in podman
#
# CI's matrix is Linux, macOS and Windows on Python 3.12, 3.13 and 3.14, plus
# ruff and the secrets check.  Linux is exact in podman, or in a Codespace with
# `tests/ci-codespace.sh`.  macOS is the host interpreters that are installed
# here.  Windows is CI only: it needs a Windows VM, and it is where CI found
# something no other platform did.
#
# The podman VM heats a laptop, and GitHub Actions is free now that the
# repository is public.  This script refuses to start on a laptop that is not on
# AC power with the battery at 80% or more.  A desktop always runs it.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
containers=1
[ "${1:-}" = "--no-containers" ] && containers=0
status=0

say() { printf '%s\n' "$*"; }

# The check comes before the first suite: a run that may not finish should not
# start.  Exit 3 is the machine saying no; any other failure is the reading
# itself, and it must not pass for a refusal.
if power=$(python3 tests/power_gate.py 2>&1); then
    say "power: $power"
else
    code=$?
    say "power: $power"
    if [ "$code" -eq 3 ]; then
        say ""
        say "skipped: run this on AC power with the battery at 80% or more, or on a desktop."
        exit 0
    fi
    exit "$code"
fi

suite() {  # suite <python> <label>
    log=$(mktemp)
    if "$1" -m compileall -q vigo tests >/dev/null 2>&1 \
        && "$1" -m unittest discover -s tests -t . >"$log" 2>&1; then
        say "$2: OK ($(grep -E '^Ran ' "$log" | tail -1))"
    else
        say "$2: FAILED"
        tail -30 "$log"
        status=1
    fi
    rm -f "$log"
}

lint() {  # lint
    log=$(mktemp)
    if podman run --rm -v "$here:/w" -w /w python:3.12 sh -c \
        'pip install -q ruff==0.17.4 >/dev/null 2>&1 && ruff check .' >"$log" 2>&1; then
        say "lint (ruff 0.17.4): OK"
    else
        say "lint (ruff 0.17.4): FAILED"
        tail -30 "$log"
        status=1
    fi
    rm -f "$log"
}

secrets() {  # secrets
    if git ls-files | grep -qE '(^|/)vigo\.json$'; then
        say "no secrets: FAILED (vigo.json is tracked)"
        status=1
    elif git grep -qE --untracked \
        'Bot [A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}'; then
        say "no secrets: FAILED (a Discord bot token is in the tree)"
        status=1
    else
        say "no secrets: OK"
    fi
}

# The host's CI Pythons that exist here.
for version in 3.12 3.13 3.14; do
    if command -v "python$version" >/dev/null 2>&1; then
        suite "python$version" "host · Python $version"
    else
        say "host · Python $version: not installed here (the codespace covers it)"
    fi
done

if command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
    lint
    if [ "$containers" -eq 1 ]; then
        say ""
        tests/in-containers.sh || status=1
    fi
else
    say "lint: podman is not answering (start it, run me again, or use tests/ci-codespace.sh)"
    say "Linux 3.12 to 3.14: not run here (tests/ci-codespace.sh runs them in a Codespace)"
fi

say ""
secrets

say ""
say "Windows and macOS: the workflow only, and any Python named as not installed above."
exit "$status"
