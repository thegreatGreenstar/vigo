#!/bin/sh
# The Linux half of what CI runs, on a machine that is not this one.
#
#   tests/in-codespace.sh              # 3.12, 3.13 and 3.14
#   tests/in-codespace.sh 3.13         # just one
#   tests/in-codespace.sh --no-git     # skip the secrets check: the copy has no .git
#
# Every version runs alone, as a separate CI job would: compileall, then the
# suite, then the ruff check, then the secrets check.  `tests/ci-codespace.sh`
# runs this inside a Codespace, whose devcontainer has all three interpreters;
# it runs on any Linux machine that has them on PATH.  It copies files and no
# `.git`, so that caller runs the secrets check on its own clone first and
# passes `--no-git` here.
#
# macOS and Windows are not here.  Codespaces is Linux only, so those two stay
# in `.github/workflows/tests.yml`, and this script cannot answer them.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
nogit=0
if [ "${1:-}" = "--no-git" ]; then
    nogit=1
    shift
fi
versions=${*:-"3.12 3.13 3.14"}
ruff_pin=0.17.4
status=0

say() { printf '%s\n' "$*"; }

for version in $versions; do
    python=python$version
    if ! command -v "$python" >/dev/null 2>&1; then
        say "Linux · Python $version: NOT INSTALLED (the devcontainer has 3.12, 3.13 and 3.14)"
        status=1
        continue
    fi
    log=$(mktemp)
    if "$python" -m compileall -q vigo tests >/dev/null 2>&1 \
        && "$python" -m unittest discover -s tests -t . >"$log" 2>&1; then
        say "Linux · Python $version: OK ($(grep -E '^Ran ' "$log" | tail -1))"
    else
        say "Linux · Python $version: FAILED"
        tail -30 "$log"
        status=1
    fi
    rm -f "$log"
done

if command -v ruff >/dev/null 2>&1; then
    found=$(ruff --version | awk '{print $2}')
    if [ "$found" != "$ruff_pin" ]; then
        say "lint: FAILED (ruff $found on PATH, the pin is $ruff_pin)"
        say "  update RUFF_VERSION in .devcontainer/Dockerfile and ruff in .github/workflows/tests.yml"
        status=1
    elif ruff check --output-format=concise .; then
        say "lint (ruff $ruff_pin): OK"
    else
        say "lint (ruff $ruff_pin): FAILED"
        status=1
    fi
else
    say "lint: NOT RUN (ruff is not installed in this image)"
    status=1
fi

# The same two checks as the `secrets` job: the settings file and a token
# shape.  The copy this runs in has no `.git`, so the machine that made the copy
# answers it and passes `--no-git`.
if [ "$nogit" -eq 1 ]; then
    say "no secrets: answered where the tree was copied from (no .git here)"
elif git rev-parse --git-dir >/dev/null 2>&1; then
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
else
    say "no secrets: NOT RUN (no .git in this tree)"
    status=1
fi

exit "$status"
