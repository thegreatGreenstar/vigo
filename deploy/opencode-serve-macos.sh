#!/bin/sh
# Start OpenCode's server with the password from the login Keychain.
#
#   security add-generic-password -a "$USER" -s vigo-opencode -w "$(openssl rand -hex 32)" -U
#   ./deploy/opencode-serve-macos.sh
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
OPENCODE_PASSWORD="$password" exec opencode serve \
    --hostname "${OPENCODE_HOSTNAME:-127.0.0.1}" \
    --port "${OPENCODE_PORT:-4096}"
