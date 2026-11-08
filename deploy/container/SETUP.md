# Container host setup

This file lists what lives on the host rather than in the repository. It has placeholders only.
Replace `you`, `peer-host`, `macbook`, `pc` and the documentation addresses with your own values.

The repository installs the image, the launcher, the units and the code. The steps below provide the
packages, secrets, keys, network and data that must stay outside it.

## Linux

### Packages

```sh
# Arch and CachyOS names; use the distribution's equivalents elsewhere
sudo pacman -S --needed podman tailscale github-cli openssh git openssl
# ssh-tpm-agent is the TPM agent that holds Vigo's keys
sudo pacman -S --needed ssh-tpm-agent
```

`podman` must be rootless and working before the units start. `tailscale` must be logged in, and the
launcher reads the first IPv4 address it reports.

### Settings

`~/.config/vigo/vigo.json` (mode 0600) is not in the repository. It names the device, the peers, the
real paths and the commands that read the secrets. The container form uses:

```jsonc
"tokenCommand": "cat /run/secrets/vigo-token",
"peerPasswordCommand": "cat /run/secrets/peer-password",
"devices": [
  { "name": "macbook", "url": "http://peer-host:4096", "directory": "/Users/you/Projects",
    "passwordFile": "peer.password" },
  { "name": "pc", "url": "http://vigo-opencode:4096", "directory": "/home/you/Projects",
    "passwordCommand": "cat /run/secrets/opencode-password" }
],
"peerPort": 4098,
"peerHost": "0.0.0.0"
```

`vigo-opencode` is the agent container's name on the user-defined network. A peer still reaches this
device at its tailnet address, because the agent publishes 4096 there.

### Secrets

Three TPM-sealed files live in `~/.config/vigo`. They are bound to this machine and this user:

```sh
# this device's OpenCode password
printf '%s' "$(openssl rand -hex 32)" | systemd-creds encrypt --user \
    --name=opencode-password - ~/.config/vigo/opencode.cred
# the Discord bot token
printf '%s' 'the bot token' | systemd-creds encrypt --user \
    --name=vigo-token - ~/.config/vigo/vigo-token.cred
# the peer API password, separate from the OpenCode password
printf '%s' "$(openssl rand -hex 32)" | systemd-creds encrypt --user \
    --name=peer-password - ~/.config/vigo/peer-api.cred
```

`~/.config/vigo/peer.password` (mode 0600) holds the peer device's OpenCode password. It is mounted
read-only into the daemon, never the agent.

The optional GitHub CLI token is sealed in the per-user credential store:

```sh
install -d -m 700 ~/.config/credstore.encrypted
bash -c 'umask 077; read -rsp "VIGO-VI token: " tok; echo; printf "%s" "$tok" | systemd-creds encrypt --user --name=gh-token - "$HOME/.config/credstore.encrypted/gh-token"; unset tok'
```

Use a token for the `VIGO-VI` account. The agent gets the token; the daemon does not.

### Vigo's SSH identity

```sh
./deploy/vigo-identity.sh
```

It creates `~/.ssh-vigo/id_ecdsa_vigo.tpm` and `id_ecdsa_vigo_signing.tpm`, prints the public halves,
and writes the public files into the agent home. Add the authentication key and the signing key to the
`VIGO-VI` account at <https://github.com/settings/keys>. The private TPM blobs stay in `~/.ssh-vigo`.
`vigo-ssh-agent.service` loads only that directory.

The daemon runs a private checkout at `~/.local/share/vigo/checkout`. The installer clones it and
requires a real remote origin, so `~/Projects/vigo` cannot become the code the daemon pulls and runs.

### Units and startup

```sh
./deploy/container/install.sh
systemctl --user enable --now vigo-ssh-agent vigo-container
```

The units start at login. To start at boot, run `loginctl enable-linger you`; systemd unseals the TPM
credentials without a login keychain.

### Host-only drop-ins

`~/.config/systemd/user/vigo-container.service.d/agent.conf`:

```ini
[Service]
# ScarletNX games, saves and shader cache
Environment=VIGO_AGENT_SCARLETNX=1
# Optional: hardware Vulkan, host Wayland, and encrypted-dump keys
#Environment=VIGO_AGENT_GPU=1
#Environment=VIGO_AGENT_WAYLAND=1
#Environment=VIGO_AGENT_KEYS=1
```

Additional project data is a colon-separated list, and is validated against the protected paths:

```ini
Environment=VIGO_AGENT_DATA=/home/you/.local/share/eden
```

A project that needs system packages the agent image lacks gets its own image:

```sh
printf 'FROM localhost/vigo-agent:latest\nRUN pacman -Syu --noconfirm --needed libfoo\n' \
    | podman build -t localhost/vigo-agent-eden -f - .
```

Then set `VIGO_AGENT_IMAGE=localhost/vigo-agent-eden` in the same drop-in and restart the unit.

### Checks

```sh
systemctl --user status vigo-container vigo-ssh-agent
podman ps
journalctl --user -u vigo-container -n 50 -o cat
podman logs vigo-opencode
podman exec vigo-opencode sh -c 'test -e /run/secrets/vigo-token && echo LEAK || echo no-token'
podman exec vigo sh -c 'test -e /run/secrets/gh-token && echo LEAK || echo no-gh'
```

The peer API must answer with the peer password and refuse the OpenCode password:

```sh
peer=$(systemd-creds decrypt --user --name=peer-password ~/.config/vigo/peer-api.cred -)
oc=$(systemd-creds decrypt --user --name=opencode-password ~/.config/vigo/opencode.cred -)
curl -o /dev/null -w '%{http_code}\n' -u "opencode:$peer" http://127.0.0.1:4098/vigo/info
curl -o /dev/null -w '%{http_code}\n' -u "opencode:$oc" http://127.0.0.1:4098/vigo/info
```

Expect `200` and `401`.

## macOS

The Macbook has two OpenCode copies. The one inside the podman machine is Vigo's agent. The host copy
stays for Metal, MoltenVK or kosmickrisp rendering, the host display, audio, USB and host loopback MCP
servers. The Linux VM has no Metal.

### Packages and machine

```sh
brew install podman tailscale gh
podman machine init --cpus 8 --memory 12288 --disk-size 100
podman machine start
```

`deploy/container/install.sh` starts the machine when `podman info` does not answer, then loads the
launchd job.

### Secrets

The Keychain cannot enter the podman machine. Use podman secrets, sourced from the Keychain or typed at
a prompt:

```sh
podman secret create vigo-token - < /path/with/token
podman secret create vigo-opencode - < /path/with/password
podman secret create vigo-peer-password - < /path/with/peer-password
podman secret create vigo-ssh-key ~/.ssh/vigo_container_ed25519
# optional, for the agent's gh
podman secret create vigo-gh-token - < /path/with/gh-token
```

The agent gets `vigo-opencode`, `vigo-ssh-key` and the optional gh token. The daemon gets the token,
the OpenCode password, the peer password and the SSH key. The peer password is not the OpenCode
password.

### Settings

The container settings use the same mounted paths as Linux:

```jsonc
"tokenCommand": "cat /run/secrets/vigo-token",
"peerPasswordCommand": "cat /run/secrets/peer-password",
"devices": [
  { "name": "macbook", "url": "http://vigo-opencode:4096", "directory": "/Users/you/Projects",
    "passwordCommand": "cat /run/secrets/opencode-password" },
  { "name": "pc", "url": "http://peer-host:4096", "directory": "/home/you/Projects",
    "passwordFile": "peer.password" }
]
```

Set `VIGO_BIND_ADDRESS` in the launchd `EnvironmentVariables` to the Mac's tailnet address, so the
peer can reach 4096 and 4098 through the podman machine. Without it the launcher publishes on
`127.0.0.1`, which is correct only when no other device must connect.

The one container SSH key is `~/.ssh/vigo_container_ed25519`, registered on `VIGO-VI` as an
authentication key and a signing key. The image's `/etc/gitconfig` signs every agent commit with it.
The Docker/podman machine cannot use the Secure Enclave or the Keychain directly.

### The host OpenCode copy

Keep the host OpenCode for Metal and host devices. Give it its own port and its own settings so it does
not collide with the container's published 4096. The host copy may use the Keychain, the host GPU, the
host display and host-only MCP servers. Vigo's daemon can point a device entry at it, but the container
agent and the host copy must not both bind 4096 on the same address.

### Checks

```sh
launchctl print gui/$(id -u)/dev.vigo.container | head -20
podman ps
podman logs vigo-opencode 2>/dev/null || podman logs --tail 50 <agent-name>
curl -o /dev/null -w '%{http_code}\n' -u "opencode:<peer-password>" \
    "http://$(tailscale ip -4):4098/vigo/info"
```

Expect one container daemon, one agent, both with the flags in `run.sh`, and `200` only with the peer
password.

## What stays outside the repository

- The packages above, the podman machine, Tailscale, and the GitHub account/key registration.
- Every secret and password, sealed or in a podman secret.
- `vigo.json`, `peer.password`, the private checkout, the agent home and optional project data.
- The host OpenCode copy and its Keychain items.
- The optional drop-ins for ScarletNX data, GPU, Wayland, keys and project-specific images.
- The tailnet ACLs that keep 4096 and 4098 away from the LAN and the public internet.
