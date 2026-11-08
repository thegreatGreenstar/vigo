#!/bin/sh
# Start OpenCode's server with the password unsealed from this machine's TPM.
#
# Host-only fallback: install.sh now uses deploy/vigo-container.sh, which runs
# OpenCode inside the container with Vigo.  This script remains for a machine
# where the host OpenCode is still the one Vigo drives.
#
#   sudo install -d -m 700 /etc/vigo
#   printf '%s' "$(openssl rand -hex 32)" | systemd-creds encrypt --user \
#       --name=opencode-password - /etc/vigo/opencode.cred
#   install -m 755 deploy/opencode-serve.sh /usr/local/libexec/opencode-serve.sh
#
# The .cred file is bound to this machine (and to the user, with --user): a
# copy on another machine does not decrypt.  That is the point of sealing it.
#
# What this does not do: OpenCode checks a plaintext password, so the value
# exists in this process's environment while it runs.  Sealing protects the
# file on disk, not the running process.
set -eu

cred="${VIGO_OPENCODE_CRED:-/etc/vigo/opencode.cred}"
name="${VIGO_OPENCODE_CRED_NAME:-opencode-password}"

if [ ! -r "$cred" ]; then
    echo "opencode-serve: $cred is not readable" >&2
    exit 1
fi

password="$(systemd-creds decrypt --user --name="$name" "$cred" -)"

OPENCODE_PASSWORD="$password" exec opencode serve \
    --hostname "${OPENCODE_HOSTNAME:-127.0.0.1}" \
    --port "${OPENCODE_PORT:-4096}"
