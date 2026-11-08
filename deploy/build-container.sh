#!/bin/sh
# Build the image that runs Vigo and its own OpenCode.
#
#   ./deploy/build-container.sh
#   OPENCODE_BIN=/usr/bin/opencode GH_BIN=/usr/bin/gh ./deploy/build-container.sh
#
# Linux copies the host's opencode and gh into a throwaway build context, so the
# image always matches the builds installed there.  macOS builds the daemon-only
# image instead: its opencode is a Darwin binary and cannot run in the podman
# machine, and OpenCode stays on the host there.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
image=${VIGO_IMAGE:-localhost/vigo:latest}
tmp=$(mktemp -d "${TMPDIR:-/tmp}/vigo-build.XXXXXX")
cleanup() { rm -rf -- "$tmp"; }
trap cleanup EXIT HUP INT TERM

case "$(uname -s)" in
Darwin)
    cp "$here/container/Containerfile.daemon" "$tmp/Containerfile"
    cp "$here/container/ssh_config" "$here/container/known_hosts" "$tmp/"
    podman build --tag "$image" "$tmp"
    printf 'built %s (daemon only; OpenCode stays on the host)\n' "$image"
    ;;
*)
    opencode=${OPENCODE_BIN:-$(command -v opencode)}
    gh=${GH_BIN:-$(command -v gh)}
    if [ ! -x "$opencode" ]; then
        echo "build-container: no executable opencode at $opencode" >&2
        exit 1
    fi
    if [ ! -x "$gh" ]; then
        echo "build-container: no executable gh at $gh" >&2
        exit 1
    fi
    version=$("$opencode" --version 2>/dev/null | awk '{print $2}' | sed 's/^v//')
    [ -n "$version" ] || version=unknown

    cp "$here/container/Containerfile" "$tmp/Containerfile"
    cp "$here/container/vigo-entrypoint.py" "$tmp/vigo-entrypoint.py"
    cp "$here/container/ssh_config" "$here/container/known_hosts" "$tmp/"
    cp "$opencode" "$tmp/opencode"
    cp "$gh" "$tmp/gh"
    chmod 0755 "$tmp/opencode" "$tmp/gh"

    podman build \
        --tag "$image" \
        --build-arg "OPENCODE_VERSION=$version" \
        "$tmp"
    printf 'built %s with opencode %s\n' "$image" "$version"
    ;;
esac
