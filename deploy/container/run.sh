#!/bin/sh
# Run Vigo in a rootless podman container.
#
#   deploy/container/run.sh                       # the daemon
#   deploy/container/run.sh --check               # the settings, then exit
#   VIGO_PEER_PORT=4098 deploy/container/run.sh   # publish the peer port (macOS)
#
# The checkout, the settings and the state are mounted at their real paths, so
# the settings file works as it is.  The token, this device's OpenCode password
# and Vigo's SSH key are podman secrets, never files in the tree; the script
# names the command that makes a missing one.
#
# Linux runs with the host's network, which is what lets the daemon use
# 127.0.0.1 for OpenCode and for an SSH tunnel, as it does outside a container.
# macOS cannot: the container lives in the podman machine, and its 127.0.0.1 is
# the VM.  There the settings name host.containers.internal instead, and a peer
# port is published on the Mac's loopback with VIGO_PEER_PORT.
set -eu

here=$(cd "$(dirname "$0")/../.." && pwd)
home=${HOME:?}
image=${VIGO_IMAGE:-localhost/vigo:latest}
token_secret=${VIGO_TOKEN_SECRET:-vigo-token}
password_secret=${VIGO_PASSWORD_SECRET:-vigo-opencode}
key_secret=${VIGO_KEY_SECRET:-vigo-ssh-key}

if ! command -v podman >/dev/null 2>&1; then
    echo "vigo: podman is not installed" >&2
    exit 2
fi
if ! podman info >/dev/null 2>&1; then
    # On macOS the machine is a VM, and the Homebrew package does not start it
    # at login (its caveat names Podman Desktop instead).  The unit is the
    # thing that must be up, so start it here.  A `podman machine stop` by hand
    # is undone on the next start of the unit; booting the unit out stops both.
    if [ "$(uname -s)" = Darwin ]; then
        echo "vigo: the podman machine is not answering; starting it" >&2
        podman machine start || true
    fi
    if ! podman info >/dev/null 2>&1; then
        echo "vigo: podman is not answering; on macOS run: podman machine start" >&2
        exit 2
    fi
fi

# The three secrets, with the command that makes each one.
missing=0
for name in "$token_secret" "$password_secret" "$key_secret"; do
    if podman secret inspect "$name" >/dev/null 2>&1; then
        continue
    fi
    missing=1
    case "$name" in
    "$key_secret")
        printf 'vigo: podman secret %s is missing; make it from the key with no passphrase:\n' "$name" >&2
        printf '  podman secret create %s ~/.ssh/vigo_ed25519\n' "$name" >&2
        ;;
    *)
        printf 'vigo: podman secret %s is missing; make it from the value:\n' "$name" >&2
        printf '  printf %%s "$VALUE" | podman secret create %s -\n' "$name" >&2
        ;;
    esac
done
if [ "$missing" -ne 0 ]; then
    exit 2
fi

if ! podman image exists "$image"; then
    podman build -t "$image" "$here/deploy/container"
fi

# Where the Mac's loopback is, for a device URL that must leave the container.
case "$(uname -s)" in
Darwin)
    network=""
    port=""
    if [ -n "${VIGO_PEER_PORT:-}" ]; then
        # The other device reaches the peer on the address in the settings.
        # A loopback publish cannot answer it, so VIGO_PEER_BIND names the
        # host address to publish on.
        bind=${VIGO_PEER_BIND:-127.0.0.1}
        port="--publish=${bind}:${VIGO_PEER_PORT}:${VIGO_PEER_PORT}"
    fi
    ;;
*)
    network="--network=host"
    port=""
    ;;
esac

# shellcheck disable=SC2086 # the two network options are whole flags or empty
# No --restart here: the unit or the launchd agent starts the script again when
# the bot ends, which is also what /vigo upgrade does.  --replace clears the
# stopped container, and `podman logs vigo` keeps the last run.
exec podman run --replace --name vigo \
    $network $port \
    --cap-drop=all \
    --security-opt=no-new-privileges \
    --read-only --tmpfs /tmp \
    -e HOME="$home" \
    -e VIGO_CONFIG="$home/.config/vigo/vigo.json" \
    -e GIT_SSH_COMMAND="ssh -F /etc/vigo/ssh_config" \
    --secret "$token_secret,type=env,target=VIGO_TOKEN" \
    --secret "$password_secret,type=env,target=VIGO_OPENCODE_PASSWORD" \
    --secret "$key_secret,target=vigo-ssh-key,mode=0400" \
    --volume "$here:$here" \
    --volume "$home/.config/vigo:$home/.config/vigo:ro" \
    --volume "$home/.local/state/vigo:$home/.local/state/vigo" \
    --workdir "$here" \
    "$image" "$@"
