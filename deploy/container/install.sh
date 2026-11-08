#!/bin/sh
# Install Vigo's container kit and its start-at-login unit.
#
#   deploy/container/install.sh
#   VIGO_PEER_PORT=4098 VIGO_PEER_BIND=100.64.0.5 deploy/container/install.sh
#
# The unit runs the installed copy under ~/.local/libexec, not the checkout, so
# a `/vigo upgrade` that pulls Python code does not change how the pod starts.
# Re-run this after a change under deploy/container, then rebuild the image
# (`podman build -t localhost/vigo ~/.local/libexec/vigo-container`).
set -eu

here=$(cd "$(dirname "$0")" && pwd)
kit=$HOME/.local/libexec/vigo-container
config=$HOME/.config/vigo
agent_home=$HOME/.local/share/vigo-agent

say() { printf '%s\n' "$*"; }

install -d -m 755 "$kit"
for file in Containerfile ssh_config known_hosts gitconfig opencode.json; do
    install -m 644 "$here/$file" "$kit/$file"
done
install -m 755 "$here/run.sh" "$kit/run.sh"
install -m 755 "$here/agent-entrypoint.sh" "$kit/agent-entrypoint.sh"
say "kit: $kit"

install -d -m 700 "$config/opencode" "$agent_home"
if [ ! -f "$config/opencode/opencode.json" ]; then
    install -m 644 "$here/opencode.json" "$config/opencode/opencode.json"
    say "agent settings: made $config/opencode/opencode.json"
else
    say "agent settings: $config/opencode/opencode.json already there, left alone"
fi

case "$(uname -s)" in
Darwin)
    agents=$HOME/Library/LaunchAgents
    install -d -m 755 "$agents"
    plist=$agents/dev.vigo.container.plist
    sed -e "s|__KIT__|$kit|g" \
        -e "s|__HOME__|$HOME|g" \
        "$here/dev.vigo.container.plist.in" > "$plist"
    chmod 644 "$plist"
    if [ -n "${VIGO_PEER_PORT:-}" ]; then
        plutil -remove EnvironmentVariables.VIGO_PEER_PORT "$plist" 2>/dev/null || true
        plutil -insert EnvironmentVariables.VIGO_PEER_PORT -string "$VIGO_PEER_PORT" "$plist"
        plutil -remove EnvironmentVariables.VIGO_PEER_BIND "$plist" 2>/dev/null || true
        plutil -insert EnvironmentVariables.VIGO_PEER_BIND \
            -string "${VIGO_PEER_BIND:-127.0.0.1}" "$plist"
    fi
    launchctl bootout "gui/$(id -u)/dev.vigo.container" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    say "agent: dev.vigo.container started; read ~/.local/state/vigo/container.err"
    ;;
Linux)
    units=$HOME/.config/systemd/user
    install -d -m 755 "$units"
    sed -e "s|__KIT__|$kit|g" \
        "$here/vigo-container.service" > "$units/vigo-container.service"
    chmod 644 "$units/vigo-container.service"
    if [ -n "${VIGO_PEER_PORT:-}" ]; then
        install -d -m 755 "$units/vigo-container.service.d"
        printf '[Service]\nEnvironment=VIGO_PEER_PORT=%s\nEnvironment=VIGO_PEER_BIND=%s\n' \
            "$VIGO_PEER_PORT" "${VIGO_PEER_BIND:-127.0.0.1}" \
            > "$units/vigo-container.service.d/peer.conf"
    fi
    systemctl --user daemon-reload
    systemctl --user enable --now vigo-container
    say "unit: vigo-container started; read it with journalctl --user -u vigo-container"
    ;;
*)
    say "services: no launchd or systemd here; run $kit/run.sh by hand"
    ;;
esac

say ""
say "If the agent has not signed in on this machine, sign it in once:"
say "  podman exec -it vigo-opencode opencode auth login opencode --method device"
