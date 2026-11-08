#!/bin/sh
# Install Vigo's container kit and its start-at-login unit.
#
#   deploy/container/install.sh
#   VIGO_BIND_ADDRESS=100.64.0.5 deploy/container/install.sh
#
# The unit runs the installed kit under ~/.local/libexec, not the checkout, so
# `/vigo upgrade` changes Python without changing how the pod starts.  Re-run
# this after editing deploy/container, then rebuild the image.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
kit=$HOME/.local/libexec/vigo-container
config=$HOME/.config/vigo
agent_home=$HOME/.local/share/vigo-agent
state=$HOME/.local/state/vigo

say() { printf '%s\n' "$*"; }

install -d -m 755 "$kit"
for file in Containerfile Containerfile.agent ssh_config ssh_config.linux known_hosts gitconfig opencode.json vigo-entrypoint.py; do
    install -m 644 "$here/$file" "$kit/$file"
done
install -m 755 "$here/run.sh" "$kit/run.sh"
install -m 755 "$here/../build-container.sh" "$kit/build-container.sh"
say "kit: $kit"

install -d -m 700 "$config" "$state" "$agent_home" \
    "$agent_home/.config/opencode" "$agent_home/.config/gh" \
    "$agent_home/.local/share/opencode" "$agent_home/.local/state" \
    "$agent_home/.ssh"

# The daemon runs a private checkout, not ~/Projects/vigo, so the agent cannot
# change the code the daemon is about to run.
checkout=${VIGO_CHECKOUT:-$HOME/.local/share/vigo/checkout}
source=${VIGO_SOURCE:-$(cd "$here/../.." && pwd)}
if [ -d "$source/.git" ]; then
    dirty=$(git -C "$source" status --porcelain)
    if [ -n "$dirty" ] && [ "${VIGO_INSTALL_DIRTY:-0}" != 1 ]; then
        say "install: $source has uncommitted changes; commit them or set VIGO_INSTALL_DIRTY=1"
        exit 1
    fi
fi
if [ ! -d "$checkout/.git" ]; then
    if [ -d "$source/.git" ]; then
        branch=$(git -C "$source" branch --show-current)
        [ -n "$branch" ] || branch=main
        install -d -m 700 "$(dirname "$checkout")"
        git clone --no-hardlinks --branch "$branch" "$source" "$checkout"
        origin=$(git -C "$source" remote get-url origin 2>/dev/null || true)
        case "$origin" in
            git@*|ssh://*|https://*) ;;
            *) say "checkout: $source has no remote origin; set one before installing"; exit 1 ;;
        esac
        git -C "$checkout" remote set-url origin "$origin"
        say "checkout: cloned $branch into $checkout"
    else
        say "checkout: $checkout is missing and $source is not a git checkout"
    fi
else
    say "checkout: $checkout already there"
fi
if [ ! -f "$agent_home/.config/opencode/opencode.json" ]; then
    if [ -f "$HOME/.config/opencode/opencode.json" ]; then
        install -m 644 "$HOME/.config/opencode/opencode.json" \
            "$agent_home/.config/opencode/opencode.json"
        say "agent settings: copied the host's OpenCode config (MCP included)"
    else
        install -m 644 "$here/opencode.json" "$agent_home/.config/opencode/opencode.json"
        say "agent settings: made $agent_home/.config/opencode/opencode.json"
    fi
else
    say "agent settings: $agent_home/.config/opencode/opencode.json already there"
fi

case "$(uname -s)" in
Darwin)
    agents=$HOME/Library/LaunchAgents
    install -d -m 755 "$agents"

    # The host runner: the one door to a program only the Mac can run.  It
    # listens on loopback, so the agent reaches it through the podman machine's
    # host gateway, and it needs the token the agent holds as a podman secret.
    install -d -m 755 "$HOME/.local/libexec"
    install -m 755 "$here/../host-runner.py" "$HOME/.local/libexec/host-runner.py"
    if [ ! -f "$config/host-runner.json" ]; then
        install -m 600 "$here/../host-runner.example.json" "$config/host-runner.json"
        say "host runner: made $config/host-runner.json from the example"
    fi
    if [ ! -f "$config/host-runner.token" ]; then
        (umask 077; openssl rand -hex 32 > "$config/host-runner.token")
        say "host runner: made $config/host-runner.token"
    fi
    python=$(command -v python3.14 2>/dev/null || command -v python3.12 2>/dev/null \
        || command -v python3 2>/dev/null || true)
    [ -n "$python" ] || python=/usr/bin/python3
    runner=$agents/dev.vigo.host-runner.plist
    sed -e "s|__PYTHON__|$python|g" -e "s|__HOME__|$HOME|g" \
        "$here/../dev.vigo.host-runner.plist.in" > "$runner"
    chmod 644 "$runner"
    launchctl bootout "gui/$(id -u)/dev.vigo.host-runner" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$runner" 2>/dev/null || true
    say "host runner: dev.vigo.host-runner loaded"
    if podman secret inspect host-runner-token >/dev/null 2>&1; then
        say "host runner: the podman secret host-runner-token is there"
    else
        podman secret create host-runner-token - < "$config/host-runner.token" >/dev/null \
            && say "host runner: made the podman secret host-runner-token"
    fi
    # The agent's MCP entry, once.  A config that does not have it is no error.
    "$python" - "$agent_home/.config/opencode/opencode.json" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(0)
if not isinstance(data, dict):
    raise SystemExit(0)
mcp = data.get("mcp")
if not isinstance(mcp, dict):
    mcp = data["mcp"] = {}
servers = mcp.get("servers")
holder = servers if isinstance(servers, dict) else mcp
# A project may ship an MCP of its own (ScarletNX's opencode.json names one
# `scarletnx`, and it is a macOS binary).  Ours has a distinct name, so the
# project's entry cannot override it.  An older entry of ours is removed.
old = holder.get("scarletnx")
if isinstance(old, dict) and "host.containers.internal" in str(old.get("url", "")):
    holder.pop("scarletnx", None)
entry = {
    "type": "remote",
    "url": "http://host.containers.internal:4099/mcp?program=scarletnx",
    "enabled": True,
    "oauth": False,
    "headers": {"Authorization": "Bearer {env:VIGO_HOST_RUNNER_TOKEN}"},
}
if holder.get("scarletnx-host") != entry:
    holder["scarletnx-host"] = entry
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print("host runner: the agent config carries the scarletnx-host MCP")
PY

    plist=$agents/dev.vigo.container.plist
    # The launcher publishes the peer port on this address, and the plist is
    # the only place it is kept.  A reinstall must not drop the address the
    # machine already uses, and the documented VIGO_BIND_ADDRESS must land.
    bind=${VIGO_BIND_ADDRESS:-}
    if [ -z "$bind" ] && [ -f "$plist" ]; then
        bind=$(plutil -extract EnvironmentVariables.VIGO_BIND_ADDRESS raw -o - \
                "$plist" 2>/dev/null || true)
    fi
    sed -e "s|__KIT__|$kit|g" -e "s|__HOME__|$HOME|g" \
        "$here/dev.vigo.container.plist.in" > "$plist"
    if [ -n "$bind" ]; then
        plutil -insert EnvironmentVariables.VIGO_BIND_ADDRESS -string "$bind" \
            "$plist" >/dev/null
    fi
    chmod 644 "$plist"
    launchctl bootout "gui/$(id -u)/dev.vigo.container" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || true
    say "agent: dev.vigo.container loaded"
    ;;
Linux)
    units=$HOME/.config/systemd/user
    install -d -m 755 "$units"
    install -m 644 "$here/vigo-container.service" "$units/vigo-container.service"
    install -m 644 "$here/../vigo-ssh-agent.service" "$units/vigo-ssh-agent.service"
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable vigo-ssh-agent vigo-container 2>/dev/null || true
    systemctl --user restart vigo-container 2>/dev/null || true
    say "units: vigo-ssh-agent.service and vigo-container.service enabled"
    say "       journalctl --user -u vigo-container -n 50 -o cat"
    ;;
*)
    say "services: no launchd or systemd here; run $kit/run.sh by hand"
    ;;
esac

say ""
say "The agent needs OpenCode's own sign-in once per machine:"
say "  podman exec -it vigo-opencode opencode auth login opencode --method device"
say "For gh, the optional token is TPM-sealed on Linux and a podman secret on macOS."
