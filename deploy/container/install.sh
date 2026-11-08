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
    plist=$agents/dev.vigo.container.plist
    sed -e "s|__KIT__|$kit|g" -e "s|__HOME__|$HOME|g" \
        "$here/dev.vigo.container.plist.in" > "$plist"
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
