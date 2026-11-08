#!/bin/sh
# Start the agent's OpenCode inside Vigo's pod.
#
# The daemon reaches it on the pod's loopback, and another device reaches it on
# the address the pod publishes, with the same password as before.  It listens
# on 0.0.0.0 inside the pod because that is the only way the published port
# arrives; the host never sees the socket unless run.sh publishes it.
set -eu

: "${OPENCODE_PASSWORD:?OPENCODE_PASSWORD is not set; the podman secret is missing}"

export HOME=/home/agent
exec opencode serve --hostname 0.0.0.0 --port "${OPENCODE_PORT:-4096}"
