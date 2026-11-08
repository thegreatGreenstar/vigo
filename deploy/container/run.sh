#!/bin/sh
# Run Vigo and its own OpenCode in one rootless podman pod.
#
#   deploy/container/run.sh                       # both containers
#   deploy/container/run.sh --check               # the settings, then exit
#   VIGO_PEER_PORT=4098 VIGO_PEER_BIND=100.64.0.5 deploy/container/run.sh
#
# Two containers, one network namespace:
#
#   vigo-opencode  the agent.  Mounts the projects read-write, its own state and
#                  a read-only settings file.  It holds no Discord token and no
#                  checkout of the daemon, and the daemon's SSH key is the only
#                  secret it shares, because it may push the branch it works on.
#   vigo           the daemon.  Mounts the checkout, the settings and the state.
#                  It talks to the agent on the pod's loopback.
#
# Both run with no capabilities, no new privileges, a read-only root, a private
# network and a bound on processes and memory.  The only host paths they see are
# the ones named below; the home directory is never mounted.
#
# The image is built from this directory when it is missing, so the kit in
# ~/.local/libexec/vigo-container must be kept in step with the repository.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
home=${HOME:?}
checkout=${VIGO_CHECKOUT:-$home/Projects/vigo}
projects=${VIGO_PROJECTS:-$home/Projects}
config=$home/.config/vigo
state=$home/.local/state/vigo
agent_home=$home/.local/share/vigo-agent
agent_config=$config/opencode
image=${VIGO_IMAGE:-localhost/vigo:latest}
token_secret=${VIGO_TOKEN_SECRET:-vigo-token}
password_secret=${VIGO_PASSWORD_SECRET:-vigo-opencode}
key_secret=${VIGO_KEY_SECRET:-vigo-ssh-key}
opencode_port=${VIGO_OPENCODE_PORT:-4096}
peer_port=${VIGO_PEER_PORT:-}
bind=${VIGO_PEER_BIND:-127.0.0.1}
pod=vigo-pod

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
        printf '  podman secret create %s ~/.ssh/vigo_container_ed25519\n' "$name" >&2
        ;;
    *)
        printf 'vigo: podman secret %s is missing; make it from the value:\n' "$name" >&2
        printf '  printf %%s "$VALUE" | podman secret create %s -\n' "$name" >&2
        printf '  (no trailing newline: it is part of the password)\n' >&2
        ;;
    esac
done
if [ "$missing" -ne 0 ]; then
    exit 2
fi

if ! podman image exists "$image"; then
    podman build -t "$image" "$kit"
fi

# The agent needs a place of its own to keep its sessions and its sign-in, and
# the settings the image ships.  A missing sign-in is said once by the agent's
# log; the daemon reports the device as down until it is there.
install -d -m 700 "$agent_home" "$agent_config"
if [ ! -f "$agent_config/opencode.json" ]; then
    install -m 644 "$kit/opencode.json" "$agent_config/opencode.json"
fi

# Published ports arrive on the address named here.  On a laptop that is the
# tailnet address when another device must reach this one, and loopback when it
# must not.  The agent's port is published the same way, because a daemon on the
# other device asks this device for work at that address.
ports="--publish=$bind:$opencode_port:$opencode_port"
if [ -n "$peer_port" ]; then
    ports="$ports --publish=$bind:$peer_port:$peer_port"
fi

cleanup() {
    podman pod rm -f "$pod" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM HUP

# A pod left by a run that was killed outright is cleared first; the daemon is
# the only thing the unit starts, so nothing else can hold this name.  The two
# names are removed too, for a machine that still has the one-container setup.
podman pod rm -f "$pod" >/dev/null 2>&1 || true
podman rm -f vigo vigo-opencode >/dev/null 2>&1 || true
# shellcheck disable=SC2086 # the published ports are whole flags or empty
podman pod create --name "$pod" $ports >/dev/null

# The agent.  No capability, no new privilege, a read-only root, the projects as
# its only host data, and a bound on what a runaway turn can take.
podman run --detach --restart=always --pod "$pod" --name vigo-opencode \
    --cap-drop=all \
    --security-opt=no-new-privileges \
    --read-only --tmpfs /tmp:rw,nosuid,nodev \
    --pids-limit="${VIGO_AGENT_PIDS:-1024}" \
    --memory="${VIGO_AGENT_MEMORY:-4g}" \
    -e HOME=/home/agent \
    -e OPENCODE_PORT="$opencode_port" \
    -e GIT_SSH_COMMAND="ssh -F /etc/vigo/ssh_config" \
    --secret "$password_secret,type=env,target=OPENCODE_PASSWORD" \
    --secret "$key_secret,target=vigo-ssh-key,mode=0400" \
    --volume "$projects:$projects:rw" \
    --volume "$agent_home:/home/agent:rw" \
    --volume "$agent_config:/home/agent/.config/opencode:ro" \
    --workdir "$projects" \
    --entrypoint /usr/local/bin/vigo-agent \
    "$image"

# The daemon.  It holds the Discord token, the settings and its own state, and
# never the projects: its one checkout is the one /vigo upgrade pulls.
# shellcheck disable=SC2086 # the published ports are whole flags or empty
podman run --pod "$pod" --name vigo \
    --cap-drop=all \
    --security-opt=no-new-privileges \
    --read-only --tmpfs /tmp:rw,nosuid,nodev \
    --pids-limit="${VIGO_DAEMON_PIDS:-256}" \
    --memory="${VIGO_DAEMON_MEMORY:-1g}" \
    -e HOME="$home" \
    -e VIGO_CONFIG="$config/vigo.json" \
    -e GIT_SSH_COMMAND="ssh -F /etc/vigo/ssh_config" \
    --secret "$token_secret,type=env,target=VIGO_TOKEN" \
    --secret "$password_secret,type=env,target=VIGO_OPENCODE_PASSWORD" \
    --secret "$key_secret,target=vigo-ssh-key,mode=0400" \
    --volume "$checkout:$checkout:rw" \
    --volume "$config:$config:ro" \
    --volume "$state:$state:rw" \
    --workdir "$checkout" \
    "$image" "$@"
