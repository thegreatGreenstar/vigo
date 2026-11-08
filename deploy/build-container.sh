#!/bin/sh
# Build the image that runs Vigo and its own OpenCode.
#
#   ./deploy/build-container.sh
#   OPENCODE_BIN=/usr/bin/opencode VIGO_IMAGE=localhost/vigo:dev ./deploy/build-container.sh
#
# The host's opencode binary is copied into a throwaway build context, so the
# image always matches the build installed here.  The checkout is a bind mount
# at run time, not part of the image: a `git pull` and a restart is enough to
# update Vigo.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
opencode=${OPENCODE_BIN:-$(command -v opencode)}
gh=${GH_BIN:-$(command -v gh)}
image=${VIGO_IMAGE:-localhost/vigo:latest}

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

tmp=$(mktemp -d "${TMPDIR:-/tmp}/vigo-build.XXXXXX")
cleanup() { rm -rf -- "$tmp"; }
trap cleanup EXIT HUP INT TERM

cp "$here/container/Containerfile" "$tmp/Containerfile"
cp "$here/container/vigo-entrypoint.py" "$tmp/vigo-entrypoint.py"
cp "$opencode" "$tmp/opencode"
cp "$gh" "$tmp/gh"
chmod 0755 "$tmp/opencode" "$tmp/gh"

podman build \
    --tag "$image" \
    --build-arg "OPENCODE_VERSION=$version" \
    "$tmp"

printf 'built %s with opencode %s\n' "$image" "$version"
