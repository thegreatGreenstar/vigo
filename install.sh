#!/bin/sh
# Install Vigo on this device: check the Python, make the settings folder,
# put the command and the user services in place, and say what is left.
#
#   ./install.sh                 # systemd user units when there is systemd
#   VIGO_PYTHON=python3.13 ./install.sh
#
set -eu

here=$(cd "$(dirname "$0")" && pwd)
python=${VIGO_PYTHON:-python3}
config="$HOME/.config/vigo"

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
if [ -f "$config/vigo.json" ]; then
    say "settings: $config/vigo.json already there, left alone"
else
    install -m 600 "$here/vigo.example.json" "$config/vigo.json"
    say "settings: made $config/vigo.json — fill in token, appID, device and devices"
fi

# 3. the launcher the service runs
install -d -m 755 "$HOME/.local/libexec"
install -m 755 "$here/deploy/opencode-serve.sh" "$HOME/.local/libexec/opencode-serve.sh"
say "launcher: ~/.local/libexec/opencode-serve.sh"

# 4. the user services, pointed at this checkout and this Python
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
    say "services: no systemd here, so run it by hand (see the README's Run it)"
fi

# 5. what the settings still need
say ""
say "Now, in $config/vigo.json:"
say "  device    this machine's name, exactly as it appears in devices"
say "  appID     the application id from the Discord portal"
say "  token     or tokenCommand, which reads it from the Keychain/TPM"
say "  password  this machine's OpenCode password, or passwordCommand"
say "Then check it with: $python -m vigo --check"
