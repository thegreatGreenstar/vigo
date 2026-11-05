#!/bin/sh
# Run the suite on the Linux Pythons the CI matrix uses, before pushing.
#
#   tests/in-containers.sh              # 3.12, 3.13 and 3.14
#   tests/in-containers.sh 3.13         # just one
#
# `podman machine` is a Linux VM on a Mac, so this covers Linux only — which is
# the platform CI answered in 17 s while Windows was red.  Windows cannot be
# containerised here: it needs a Windows VM, and it is the one place CI found
# something no run on Linux could (a Windows-only socket wake).  macOS is also
# CI-only, and that is where the runner itself is the flaky part.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
versions=${*:-"3.12 3.13 3.14"}
status=0

if ! podman info >/dev/null 2>&1; then
    printf '%s\n' "podman is not answering: \`podman machine start\` first" >&2
    exit 2
fi

for version in $versions; do
    log=$(mktemp)
    printf 'python %s: ' "$version"
    if podman run --rm -v "$here:/w" -w /w "python:$version" sh -c '
            apt-get update -qq >/dev/null 2>&1
            apt-get install -y -qq git >/dev/null 2>&1
            python -m unittest discover -s tests -t .' >"$log" 2>&1; then
        tail -1 "$log"
    else
        printf 'FAILED\n'
        tail -30 "$log"
        status=1
    fi
    rm -f "$log"
done

exit "$status"
