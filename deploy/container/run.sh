#!/bin/sh
# Run Vigo's pod: the daemon and its agent in two rootless containers.
#
#   deploy/container/run.sh                       # both containers
#   deploy/container/run.sh --check               # the settings, then exit
#   VIGO_BIND_ADDRESS=100.64.0.5 deploy/container/run.sh
#
# One network namespace, two file-system namespaces:
#
#   vigo-opencode  the agent.  Sees the projects, its own home, its settings and
#                  the SSH agent.  It holds no Discord token and no daemon state.
#   vigo           the daemon.  Sees the checkout, the settings and its state.
#                  It holds the Discord token and never the projects or gh.
#
# Both drop every capability, refuse new privileges, use a read-only root and
# are bounded.  Ports are published on one named address, never 0.0.0.0.
set -eu

kit=$(cd "$(dirname "$0")" && pwd)
vigo_args=$*
home=${HOME:?}
checkout=${VIGO_CHECKOUT:-$home/.local/share/vigo/checkout}
projects=${VIGO_PROJECTS:-$home/Projects}
config=$home/.config/vigo
state=$home/.local/state/vigo
agent_home=${VIGO_AGENT_HOME:-$home/.local/share/vigo-agent}
image=${VIGO_IMAGE:-localhost/vigo:latest}
agent_image=${VIGO_AGENT_IMAGE:-localhost/vigo-agent:latest}
network=${VIGO_NETWORK:-vigo-net}
opencode_port=${VIGO_OPENCODE_PORT:-4096}
peer_port=${VIGO_PEER_PORT:-4098}
os=$(uname -s)

say() { printf '%s\n' "$*"; }
die() { printf 'vigo: %s\n' "$*" >&2; exit 2; }
real() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }

if ! command -v podman >/dev/null 2>&1; then
    die "podman is not installed"
fi
if ! podman info >/dev/null 2>&1; then
    if [ "$os" = Darwin ]; then
        printf '%s\n' "podman machine is not answering; starting it" >&2
        podman machine start || true
    fi
    podman info >/dev/null 2>&1 || die "podman is not answering"
fi

# The image is built from the kit, not from host binaries: both containers need
# a Linux OpenCode and the same pinned versions.
if ! podman image exists "$image" || ! podman image exists "$agent_image"; then
    say "building $image and $agent_image"
    "$kit/build-container.sh"
fi
[ -d "$projects" ] || die "the projects directory $projects is missing"
[ -f "$config/vigo.json" ] || die "the settings file $config/vigo.json is missing"
[ -d "$checkout/.git" ] || die "the daemon checkout $checkout is missing; run deploy/container/install.sh"
checkout_real=$(real "$checkout")
projects_real=$(real "$projects")
case "$checkout_real" in
    "$projects_real"|"$projects_real"/*)
        die "the daemon checkout is inside the projects tree the agent can write" ;;
esac
origin=$(git -C "$checkout" remote get-url origin 2>/dev/null || true)
case "$origin" in
    git@*|ssh://*|https://*) ;;
    *) die "the daemon checkout origin is not a remote URL (got '$origin')" ;;
esac

install -d -m 700 "$agent_home" \
    "$agent_home/.config/opencode" "$agent_home/.config/gh" \
    "$agent_home/.local/share/opencode" "$agent_home/.local/state" \
    "$agent_home/.ssh" "$state"
if [ ! -f "$agent_home/.config/opencode/opencode.json" ]; then
    install -m 644 "$kit/opencode.json" "$agent_home/.config/opencode/opencode.json"
fi
# The agent's home must never hold the daemon's settings or state.  A migration
# from the old one-container layout is the usual way this appears.
if [ -e "$agent_home/.local/state/vigo" ] || [ -e "$agent_home/.config/vigo" ] \
    || [ -e "$agent_home/vigo.json" ] || [ -e "$agent_home/state.json" ]; then
    die "the agent home holds daemon state; move it out before starting"
fi

# Secrets and the address differ by host.
if [ "$os" = Darwin ]; then
    token_secret=${VIGO_TOKEN_SECRET:-vigo-token}
    password_secret=${VIGO_PASSWORD_SECRET:-vigo-opencode}
    key_secret=${VIGO_KEY_SECRET:-vigo-ssh-key}
    gh_secret=${VIGO_GH_SECRET:-vigo-gh-token}
    peer_secret=${VIGO_PEER_SECRET:-vigo-peer-password}
    for name in "$token_secret" "$password_secret" "$key_secret" "$peer_secret"; do
        podman secret inspect "$name" >/dev/null 2>&1 || die "podman secret $name is missing"
    done
    bind=${VIGO_BIND_ADDRESS:-${VIGO_PEER_BIND:-127.0.0.1}}
else
    cred=${CREDENTIALS_DIRECTORY:-}
    [ -n "$cred" ] || die "CREDENTIALS_DIRECTORY is unset; start this through systemd"
    [ -r "$cred/vigo-token" ] || die "$cred/vigo-token is missing"
    [ -r "$cred/opencode-password" ] || die "$cred/opencode-password is missing"
    agent_dir=${VIGO_AGENT_DIR:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/vigo}
    [ -S "$agent_dir/ssh-agent.sock" ] || die "$agent_dir/ssh-agent.sock is missing"
    bind=${VIGO_BIND_ADDRESS:-}
    if [ -z "$bind" ]; then
        bind=$(timeout 5 tailscale ip -4 2>/dev/null | head -n 1 || true)
    fi
fi

case "$bind" in
    ''|'0.0.0.0'|'::'|'[::]') die "no usable bind address (got '$bind')" ;;
esac

# A user-defined network keeps the two containers on separate network
# namespaces.  The daemon reaches OpenCode by name; the agent cannot bind the
# daemon's published peer port, so it cannot impersonate the peer API.
podman pod rm -f vigo-pod >/dev/null 2>&1 || true
podman rm -f vigo vigo-opencode >/dev/null 2>&1 || true
podman network exists "$network" >/dev/null 2>&1 || podman network create "$network" >/dev/null

cleanup() { podman rm -f vigo vigo-opencode >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM HUP

# --- the agent ---------------------------------------------------------------
set -- \
    --detach --restart=always --network "$network" --name vigo-opencode \
    --publish "$bind:$opencode_port:$opencode_port" \
    --cap-drop=all --security-opt=no-new-privileges \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=512m \
    --tmpfs /run:rw,nosuid,nodev,size=64m --shm-size=256m \
    --pids-limit="${VIGO_AGENT_PIDS:-3072}" \
    --cpus="${VIGO_AGENT_CPUS:-8}" \
    --memory="${VIGO_AGENT_MEMORY:-10g}" --memory-swap="${VIGO_AGENT_MEMORY:-10g}" \
    --ulimit core=0 --ulimit nofile=4096:8192 \
    -e HOME=/home/vigo -e VIGO_ROLE=agent -e OPENCODE_PORT="$opencode_port" \
    --mount "type=bind,src=$agent_home,dst=/home/vigo" \
    --mount "type=bind,src=$agent_home/.config/opencode,dst=/home/vigo/.config/opencode,ro" \
    --workdir "$projects"

# Extra host data a project needs, as a colon-separated list of absolute paths.
# Each is mounted read-write at the same path, only in the agent.
if [ -n "${VIGO_AGENT_DATA:-}" ]; then
    old_ifs=$IFS
    IFS=:
    set -f
    for dir in $VIGO_AGENT_DATA; do
        case "$dir" in
            /*) ;;
            *) die "VIGO_AGENT_DATA entry is not absolute: $dir" ;;
        esac
        [ -d "$dir" ] || die "VIGO_AGENT_DATA entry is not a directory: $dir"
        realdir=$(real "$dir")
        [ "$realdir" = "$home" ] && die "VIGO_AGENT_DATA must not be the home directory"
        for protected in "$config" "$state" "$checkout" "$agent_home" \
                "$home/.ssh" "$home/.config/opencode" "$home/.local/share/opencode" \
                "$home/.local/share/containers" "$home/.config/credstore.encrypted"; do
            protected_real=$(real "$protected")
            case "$realdir" in
                "$protected_real"|"$protected_real"/*)
                    die "VIGO_AGENT_DATA $dir overlaps $protected" ;;
            esac
            case "$protected_real" in
                "$realdir"|"$realdir"/*)
                    die "VIGO_AGENT_DATA $dir contains the protected $protected" ;;
            esac
        done
        set -- "$@" --mount "type=bind,src=$dir,dst=$dir"
    done
    set +f
    IFS=$old_ifs
fi

if [ "$os" = Darwin ]; then
    set -- "$@" \
        --secret "$password_secret,target=opencode-password,mode=0400" \
        --secret "$key_secret,target=vigo-ssh-key,mode=0400" \
        -e GIT_SSH_COMMAND="ssh -F /etc/vigo/ssh_config" \
        --mount "type=bind,src=$projects,dst=$projects"
    if podman secret inspect "$gh_secret" >/dev/null 2>&1; then
        set -- "$@" --secret "$gh_secret,target=gh-token,mode=0400"
    fi
else
    set -- "$@" \
        --mount "type=bind,src=$projects,dst=$projects" \
        --mount "type=bind,src=$agent_home/.ssh,dst=/home/vigo/.ssh,ro" \
        --mount "type=bind,src=$cred/opencode-password,dst=/run/secrets/opencode-password,ro" \
        --mount "type=bind,src=$agent_dir,dst=/run/vigo,ro"
    if [ -f "$agent_home/.gitconfig" ]; then
        set -- "$@" --mount "type=bind,src=$agent_home/.gitconfig,dst=/home/vigo/.gitconfig,ro"
    fi
    if [ -r "$cred/gh-token" ]; then
        set -- "$@" --mount "type=bind,src=$cred/gh-token,dst=/run/secrets/gh-token,ro"
    fi
    # ScarletNX's data (games, recompiled modules, shader cache) is host data
    # the agent needs, and it stays in the agent.
    if [ "${VIGO_AGENT_SCARLETNX:-0}" = 1 ] && [ -d "$home/.local/share/scarletnx" ]; then
        set -- "$@" --mount "type=bind,src=$home/.local/share/scarletnx,dst=$home/.local/share/scarletnx"
    fi
    # These are opt-in because they widen the boundary. The render node is the
    # only one a Vulkan driver needs: card* adds modesetting and EDID, which a
    # headless game never uses. Software Vulkan (llvmpipe) reports a subgroup
    # size of 8, and the console's is 32, so a title whose shaders use vote or
    # shuffle loses those draws. RADV reports 64.
    if [ "${VIGO_AGENT_GPU:-0}" = 1 ] && [ -e /dev/dri/renderD128 ]; then
        set -- "$@" --device /dev/dri/renderD128 --group-add keep-groups
    fi
    if [ "${VIGO_AGENT_WAYLAND:-0}" = 1 ] && [ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wayland-0" ]; then
        set -- "$@" \
            --mount "type=bind,src=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wayland-0,dst=/run/wayland-0" \
            -e WAYLAND_DISPLAY=wayland-0 -e XDG_RUNTIME_DIR=/run
    fi
    if [ "${VIGO_AGENT_KEYS:-0}" = 1 ] && [ -d "$home/.switch" ]; then
        set -- "$@" --mount "type=bind,src=$home/.switch,dst=$home/.switch,ro"
    fi
    # MCP servers are part of Vigo's work; an MCP binary and its data stay
    # inside the agent container, never the daemon.
    if [ -x "$home/.local/bin/ScarletNX-linux-x86_64.AppImage" ]; then
        set -- "$@" --mount "type=bind,src=$home/.local/bin/ScarletNX-linux-x86_64.AppImage,dst=$home/.local/bin/ScarletNX-linux-x86_64.AppImage,ro"
    fi
fi
podman run "$@" "$agent_image" >/dev/null

# --- the daemon --------------------------------------------------------------
set -- \
    --rm --network "$network" --name vigo \
    --publish "$bind:$peer_port:$peer_port" \
    --cap-drop=all --security-opt=no-new-privileges \
    --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
    --tmpfs /run:rw,nosuid,nodev,size=64m \
    --pids-limit="${VIGO_DAEMON_PIDS:-512}" \
    --cpus="${VIGO_DAEMON_CPUS:-2}" \
    --memory="${VIGO_DAEMON_MEMORY:-1g}" --memory-swap="${VIGO_DAEMON_MEMORY:-1g}" \
    --ulimit core=0 --ulimit nofile=4096:8192 \
    -e HOME=/home/vigo -e VIGO_ROLE=daemon -e VIGO_CONFIG=/config/vigo.json \
    --mount "type=bind,src=$checkout,dst=$checkout" \
    --mount "type=bind,src=$config/vigo.json,dst=/config/vigo.json,ro" \
    --mount "type=bind,src=$state,dst=/home/vigo/.local/state/vigo" \
    --workdir "$checkout"
if [ -f "$config/peer.password" ]; then
    set -- "$@" --mount "type=bind,src=$config/peer.password,dst=/config/peer.password,ro"
fi

if [ "$os" = Darwin ]; then
    set -- "$@" \
        --secret "$token_secret,target=vigo-token,mode=0400" \
        --secret "$password_secret,target=opencode-password,mode=0400" \
        --secret "$peer_secret,target=peer-password,mode=0400" \
        --secret "$key_secret,target=vigo-ssh-key,mode=0400" \
        -e VIGO_CONFIG=/config/vigo.json \
        -e GIT_SSH_COMMAND="ssh -F /etc/vigo/ssh_config" \
        --mount "type=bind,src=$config/vigo.json,dst=/config/vigo.json,ro"
    if [ -f "$config/peer.password" ]; then
        set -- "$@" --mount "type=bind,src=$config/peer.password,dst=/config/peer.password,ro"
    fi
else
    set -- "$@" \
        --mount "type=bind,src=$cred/vigo-token,dst=/run/secrets/vigo-token,ro" \
        --mount "type=bind,src=$cred/opencode-password,dst=/run/secrets/opencode-password,ro" \
        --mount "type=bind,src=$cred/peer-password,dst=/run/secrets/peer-password,ro" \
        --mount "type=bind,src=$config/ssh,dst=/home/vigo/.ssh,ro" \
        --mount "type=bind,src=$agent_dir,dst=/run/vigo,ro"
    if [ -f "$config/gitconfig" ]; then
        set -- "$@" --mount "type=bind,src=$config/gitconfig,dst=/home/vigo/.gitconfig,ro"
    fi
fi
podman run "$@" "$image" $vigo_args
