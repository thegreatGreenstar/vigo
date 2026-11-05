#!/bin/sh
# Start OpenCode's server with the password from the login Keychain.
#
#   security add-generic-password -a "$USER" -s vigo-opencode -w "$(openssl rand -hex 32)" -U
#   OPENCODE_HOSTNAME=tailscale ./deploy/opencode-serve-macos.sh
#
# OPENCODE_HOSTNAME may be an address, or "tailscale" for this machine's tailnet
# address, which is what a launchd agent uses so the address is never written
# into a file.
#
# The password never appears in a command line or a shell history entry, and
# the settings file never holds it either: it is read here and handed to the
# server as an environment variable, which is what OpenCode checks.
#
# What this does not do: OpenCode compares a plaintext password, so the value
# lives in this process's environment while it runs.  The Keychain protects it
# at rest, not in use.
set -eu

password="$(security find-generic-password -s "${VIGO_OPENCODE_ITEM:-vigo-opencode}" -w)"

host="${OPENCODE_HOSTNAME:-127.0.0.1}"
if [ "$host" = "tailscale" ]; then
    host="$("/Applications/Tailscale.app/Contents/MacOS/Tailscale" ip -4)"
fi

# The desktop app ships its own CLI, which is what is installed here.
if command -v opencode >/dev/null 2>&1; then
    server=opencode
else
    server="$(ls -d "$HOME/Library/Application Support/ai.opencode.desktop/cli"/*/opencode-cli 2>/dev/null | tail -1)"
    if [ -z "$server" ]; then
        echo "opencode not found; install the desktop app or put it on PATH" >&2
        exit 1
    fi
fi

OPENCODE_PASSWORD="$password" exec "$server" serve \
    --hostname "$host" \
    --port "${OPENCODE_PORT:-4096}"
