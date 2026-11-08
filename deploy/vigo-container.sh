#!/bin/sh
# Run Vigo and its own OpenCode in one rootless podman container.
#
# systemd starts this as the user, unseals the two credentials into
# $CREDENTIALS_DIRECTORY, and restarts the container when it exits.  The only
# paths the container can write are the checkout, ~/Projects, and its own home
# under ~/.local/share/vigo; everything else is read-only or absent.
#
#   VIGO_BIND_ADDRESS=... deploy/vigo-container.sh
#
# The bind address defaults to this machine's first Tailscale IPv4 address, so
# the ports are never on the LAN.  If Tailscale is not up yet this exits 1 and
# systemd tries again.
set -eu

root=${VIGO_CONTAINER_ROOT:-$HOME/.local/share/vigo}
checkout=${VIGO_CHECKOUT:-$HOME/Projects/vigo}
projects=${VIGO_PROJECTS_DIR:-$HOME/Projects}
config=${VIGO_CONFIG_FILE:-$HOME/.config/vigo/vigo.json}
peer_password=${VIGO_PEER_PASSWORD_FILE:-$HOME/.config/vigo/macbook.password}
image=${VIGO_IMAGE:-localhost/vigo:latest}
runtime=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
agent_dir=${VIGO_AGENT_DIR:-$runtime/vigo}
home=$root/home
port=${VIGO_OPENCODE_PORT:-4096}
peer_port=${VIGO_PEER_PORT:-4098}

need() {
    [ -e "$1" ] || {
        echo "vigo-container: $1 is missing" >&2
        exit 1
    }
}

need "$checkout/.git"
need "$projects"
need "$config"
need "$peer_password"
need "$home"
need "$agent_dir/ssh-agent.sock"

cred=${CREDENTIALS_DIRECTORY:-}
if [ -z "$cred" ]; then
    echo "vigo-container: CREDENTIALS_DIRECTORY is unset; start it through systemd" >&2
    exit 1
fi
need "$cred/vigo-token"
need "$cred/opencode-password"

podman image exists "$image" || {
    echo "vigo-container: no image $image; run deploy/build-container.sh" >&2
    exit 1
}

bind=${VIGO_BIND_ADDRESS:-}
if [ -z "$bind" ]; then
    bind=$(timeout 5 tailscale ip -4 2>/dev/null | head -n 1 || true)
fi
if [ -z "$bind" ]; then
    echo "vigo-container: no Tailscale IPv4 address yet" >&2
    exit 1
fi

# k8s-file is deliberate: the host's default is journald, and podman attach
# already sends the same lines to this unit's journal, so journald doubles them.
set -- podman run \
    --rm \
    --replace \
    --name vigo \
    --hostname vigo \
    --userns=keep-id:uid=1000,gid=1000 \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=512m \
    --tmpfs /run:rw,nosuid,nodev,size=64m \
    --shm-size=256m \
    --cap-drop=all \
    --security-opt=no-new-privileges \
    --pids-limit=8192 \
    --memory=6g \
    --memory-swap=6g \
    --ulimit core=0 \
    --ulimit nofile=4096:8192 \
    --network=pasta \
    --log-driver=k8s-file \
    --publish "$bind:$port:$port" \
    --publish "$bind:$peer_port:$peer_port" \
    --mount "type=bind,src=$checkout,dst=/vigo" \
    --mount "type=bind,src=$projects,dst=$projects" \
    --mount "type=bind,src=$home,dst=/home/vigo" \
    --mount "type=bind,src=$config,dst=/config/vigo.json,ro" \
    --mount "type=bind,src=$peer_password,dst=/config/macbook.password,ro" \
    --mount "type=bind,src=$cred/vigo-token,dst=/run/secrets/vigo-token,ro" \
    --mount "type=bind,src=$cred/opencode-password,dst=/run/secrets/opencode-password,ro" \
    --mount "type=bind,src=$agent_dir,dst=/run/vigo"

# Optional: Vigo's own GitHub CLI token, sealed in the per-user credential
# store.  Without it gh runs unauthenticated instead of holding a token.
if [ -f "$cred/gh-token" ]; then
    set -- "$@" --mount "type=bind,src=$cred/gh-token,dst=/run/secrets/gh-token,ro"
fi

set -- "$@" "$image"
exec "$@"
