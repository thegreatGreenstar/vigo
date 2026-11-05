#!/bin/sh
# Install Vigo on this device: check the Python, make the settings folder, put
# the command and the login services in place, and say what is left.
#
#   ./install.sh                 # launchd agents on macOS, systemd units on Linux
#   VIGO_PYTHON=python3.13 ./install.sh
#
set -eu

here=$(cd "$(dirname "$0")" && pwd)
python=${VIGO_PYTHON:-python3}
config="$HOME/.config/vigo"
state="$HOME/.local/state/vigo"

say() { printf '%s\n' "$*"; }

# 1. a Python that can run it
if ! "$python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    say "vigo needs Python 3.12 or newer. This '$python' is:"
    "$python" -V || true
    say "Set VIGO_PYTHON to a newer one and run me again."
    exit 1
fi
say "python: $("$python" -V 2>&1)"

# 2. the settings file, made only when it is not there
install -d -m 700 "$config"
install -d -m 700 "$state"
if [ -f "$config/vigo.json" ]; then
    say "settings: $config/vigo.json already there, left alone"
else
    install -m 600 "$here/vigo.example.json" "$config/vigo.json"
    say "settings: made $config/vigo.json — fill in token, appID, device and devices"
fi

# 3. the launchers the services run
install -d -m 755 "$HOME/.local/libexec"
install -m 755 "$here/deploy/opencode-serve-macos.sh" "$HOME/.local/libexec/opencode-serve-macos.sh"
install -m 755 "$here/deploy/opencode-serve.sh" "$HOME/.local/libexec/opencode-serve.sh"
say "launchers: ~/.local/libexec/opencode-serve{,-macos}.sh"

# 4. start at login
case "$(uname -s)" in
Darwin)
    agents="$HOME/Library/LaunchAgents"
    install -d -m 755 "$agents"
    for name in bot opencode; do
        sed -e "s|__PYTHON__|$python|g" \
            -e "s|__CHECKOUT__|$here|g" \
            -e "s|__HOME__|$HOME|g" \
            "$here/deploy/dev.vigo.$name.plist.in" > "$agents/dev.vigo.$name.plist"
        chmod 644 "$agents/dev.vigo.$name.plist"
        launchctl bootout "gui/$(id -u)/dev.vigo.$name" 2>/dev/null || true
    done
    for name in opencode bot; do
        launchctl bootstrap "gui/$(id -u)" "$agents/dev.vigo.$name.plist" 2>/dev/null || true
    done
    say "agents: installed in ~/Library/LaunchAgents and started"
    say "        they run at login and restart themselves; status with:"
    say "        launchctl print gui/\$(id -u)/dev.vigo.bot | head -20"
    ;;
Linux)
    if command -v systemctl >/dev/null 2>&1 && [ -d "$HOME/.config" ]; then
        units="$HOME/.config/systemd/user"
        install -d -m 755 "$units"
        sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$here|" \
            -e "s|^ExecStart=/usr/bin/python3 -m vigo|ExecStart=$python -m vigo|" \
            "$here/deploy/vigo.service" > "$units/vigo.service"
        install -m 644 "$here/deploy/opencode-serve.service" "$units/opencode-serve.service"
        chmod 644 "$units/vigo.service"
        systemctl --user daemon-reload 2>/dev/null || true
        say "services: installed into ~/.config/systemd/user"
        say "          enable them with: systemctl --user enable --now opencode-serve vigo"
    else
        say "services: no systemd here, so run it by hand (see the README)"
    fi
    ;;
*)
    say "services: no launchd or systemd here, so run it by hand (see the README)"
    ;;
esac

# 5. main takes pull requests only: refuse a push straight to it
if [ -d "$here/.git" ] || git -C "$here" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$here" config core.hooksPath .githooks
    say "hooks: pushes to main are refused here (.githooks/pre-push)"
else
    say "hooks: not a git checkout, so main is not guarded on this copy"
fi

# 6. what the settings still need
say ""
say "Now, in $config/vigo.json:"
say "  device    this machine's name, exactly as it appears in devices"
say "  appID     the application id from the Discord portal"
say "  token     or tokenCommand, which reads it from the Keychain/TPM"
say "  password  this machine's OpenCode password, or passwordCommand"
say "Then check it with: $python -m vigo --check"
