# vigo

Your OpenCode agent, on your own machines, reachable from Discord.You ask in a channel with `/vigo ask`, and the answer comes back there. The
work runs on your Mac or your PC — whichever one you last used — against the
project directory that device points at. Every tool that needs a permission
asks *you* in a DM, with a button. Nothing runs because someone typed
something in a channel; only because you clicked.

Python 3.12 or newer, standard library only. No packages, no build step.

## How it works

Each device runs two things:

```
Discord  <──gateway/HTTP──>  vigo (this program)  <──HTTP──>  opencode serve
   ^                                                              on 127.0.0.1
   └── you, in a channel or your DMs
```

- `vigo` holds the Discord connection, and every device runs its own copy.
- `opencode serve` is OpenCode's own API on that device. `vigo` creates a
  session in the project directory, sends the prompt, and streams the answer
  back as the message it keeps editing.
- Each Discord channel remembers one OpenCode session per device, so
  follow-ups continue the same conversation.

Only one daemon answers a given use: they all try to acknowledge it at once,
Discord takes the first, and the rest are told it was already answered and
stop. Whoever wins can drive every device, so it does not matter which one it
is — and a device answers even when its own daemon is not running.

## What can and cannot happen

- **Who may ask** is decided by the daemon from its own state, never from the
  text of a message. The owner, the ids in the settings file, and the people
  the owner allowed with `/vigo allow` may ask. Anyone else gets an ephemeral
  "no" and nothing is sent to any device, ever.
- **Allowed users can ask, but cannot approve.** Tool permissions go to the
  owner's DMs. An approved person's question cannot make the agent run a shell
  command without the owner's click.
- **Nothing is stored about what was said.** No message text is logged; the
  log has ids, outcomes and durations. Answers live in Discord, as they do for
  any bot.
- **The bot asks for no privileged intents.** It reads no messages: only the
  slash command and the buttons. There is no message-content intent to enable.
- **The token never enters the repository.** Settings live in
  `~/.config/vigo/vigo.json` (mode 0600); the repo ships only an example.
  CI fails if a `vigo.json` or a bot token shape reaches the tree.
- **The OpenCode password is the real trust boundary.** Anyone with it can run
  anything on that device, so it should not be a password you use elsewhere,
  and the port should not face the internet. See [Security](#security).

## Setup

### 1. A Discord application

1. <https://discord.com/developers/applications> → **New Application**, name it
   `Vigo`.
2. **Bot** → **Reset Token**, copy the token. Turn **Public Bot** off.
3. **General Information** → copy the **Application ID**.
4. **Installation** → turn on **User Install** (and **Guild Install** if you
   want the bot in a server of your own). Under *Default Install Settings*, the
   user-install scopes are `applications.commands`.
5. Add it to your own account — no server, no invite permission needed:

   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&integration_type=1&scope=applications.commands
   ```

   In the Discord client it appears under **Settings → Apps**, and `/vigo`
   works in your DMs with it, in group DMs, and in the command picker of any
   server you are in — even servers the bot is not a member of.
6. Get **your** numeric user id: Settings → Advanced → Developer Mode on, then
   right-click your avatar → **Copy User ID**. The username is not enough —
   every check here is an id.

If you also want to invite it to a server (its own avatar posting there), use
the `bot` scope and these permissions — View Channels, Send Messages, Read
Message History:

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=68608
```

The command is registered globally on the application, so it serves both kinds
of install at once. Discord takes up to an hour to show a changed command; the
first start is usually a minute.

**What a user install can and cannot do.** Answers, progress edits and extra
messages all go through the interaction's own token, so they work in a channel
where the bot is not a member. Two things need the bot in the channel: posting
an answer that arrives *after* the interaction's 15 minutes, and posting a
permission card in the channel when a DM is impossible. Both fall back with a
line in the log; the normal paths do not need it.

### 2. OpenCode on each device

Run OpenCode's own server with a fixed password, bound to the network so the
other device can reach it:

```sh
OPENCODE_PASSWORD='something-long-and-not-reused' \
  opencode serve --hostname 0.0.0.0 --port 4096
```

Both devices use the same `OPENCODE_PASSWORD`. Check it answers:

```sh
curl -u "opencode:$OPENCODE_PASSWORD" http://127.0.0.1:4096/api/info
```

On the Mac, `/api/info` also tells you the hostname to put in the other
device's settings.

### 3. The settings file

```sh
install -d -m 700 ~/.config/vigo
install -m 600 vigo.example.json ~/.config/vigo/vigo.json
```

Then fill it in:

```jsonc
{
  "token": "the bot token",
  "appID": "111111111111111111",
  "owner": "145512144145512144",       // your user id
  "device": "macbook",                 // which entry this copy of vigo is
  "password": "the OPENCODE_PASSWORD",
  "users": [],                         // ids always allowed to ask
  "model": { "providerID": "opencode-go", "id": "deepseek-v4.1-flash" },
  "devices": [
    { "name": "macbook", "url": "http://127.0.0.1:4096",
      "directory": "/Users/you/Projects/ScarletNX" },
    { "name": "pc", "url": "http://192.168.1.20:4096",
      "directory": "C:/Users/you/Projects/ScarletNX" }
  ],
  "activityWindow": 1800,
  "timeout": 900,
  "editEvery": 3
}
```

| Key | What it is |
| --- | --- |
| `token`, `appID`, `owner` | from the Developer Portal, and your user id |
| `device` | this machine's entry; the same file runs on both, with this line changed |
| `password` | the OpenCode password; a device may set its own with `password` in its entry |
| `users` | ids that may always ask, besides the owner |
| `install` | `both` (default), `user` or `guild`: which install kinds the command supports. Keep `both` — a user install can be used in servers too |
| `model` | provider, model id, optional `variant` |
| `devices` | name, base URL and project directory of each machine |
| `activityWindow` | seconds of "recent use" that make a device the one to use (0 disables) |
| `timeout` | seconds before a run reports "still running" (30–3600) |
| `editEvery` | seconds between progress edits of the Discord message |
| `stateChannel` | optional, but recommended: a private channel the bot can read, where the allowlist message lives; empty means the bot's DM with you |

A key the file does not know stops the bot and names the key, and so does a
value of the wrong shape. Check without connecting:

```sh
python3 -m vigo --check       # the file, and what it says
python3 -m vigo --whoami      # asks Discord for the bot's own name
python3 -m vigo --devices     # probes the devices and says where a question would go
```

### 4. Run it

```sh
python3 -m vigo
```

Python 3.12 or newer. On macOS `python3` is often 3.9, so use the newer one by
name (`python3.14 -m vigo`), or install the package into a 3.12+ interpreter
(`pip install .`, then `vigo`).

Until it exits, that terminal is the bot. Run it on both devices; both can
answer, and only one answers each question.

### 5. The other device

The second machine needs the repo, Python 3.12 or newer, and its own entry in the settings. Clone it over SSH and run
the installer:

```sh
git clone git@github.com:thegreatGreenstar/vigo.git
cd vigo
./install.sh
```

It checks the Python, makes `~/.config/vigo/vigo.json` from the example (never overwriting one), installs
`~/.local/libexec/opencode-serve.sh`, and — where systemd exists — both user units, pointed at that checkout and that
interpreter. Then:

1. In the settings, set `device` to this machine's name, keep the **same** `devices` list as the other machine, and
   keep this machine's password in its own hardware:
   ```sh
   printf '%s' "$(openssl rand -hex 32)" | systemd-creds encrypt --user \
       --name=opencode-password - ~/.config/vigo/opencode.cred
   ```
   with `"passwordCommand": "systemd-creds decrypt --user --name=opencode-password ~/.config/vigo/opencode.cred -"`,
   so the file holds no secret. "The two devices cannot hold the same file" below says which secret sits where.
2. Start OpenCode there too, reachable from the other machine:
   ```sh
   OPENCODE_HOSTNAME=0.0.0.0 ./deploy/opencode-serve.sh
   ```
   or `systemctl --user enable --now opencode-serve vigo`.
3. Reach each other by VPN address (Tailscale, WireGuard), not the LAN address, so the traffic is encrypted and the
   port is never open on the network.


## The Discord application, in words

What the Developer Portal asks for, if it helps:

- **Name**: `Vigo` — **Bot username**: `vigo`
- **About me**: *Vigo is my OpenCode agent on my own machines. I ask from a
  channel, the work runs on my Mac or PC, and every tool that needs a
  permission waits for my click. Nothing is stored: no message text, no logs of
  what was said.*
- **Tags**: `AI`, `Productivity`, `Developer Tools`, `Utilities`
- **Icon**: `art/vigo-icon-41.png` (512×512) — Aogo's blossom, petals shifted to
  DeepSeek blue, **41** on the wing where Aogo had 55 (Opus 5.5), and **VI** on
  the hat tag where Aogo had AO. Rebuild it with `./art/make-icon.sh`;
  `art/recolor.py` and `art/badge.swift` are what it runs, and
  `art/aogo-source.png` is the original.

## The commands

| Command | Who | What happens |
| --- | --- | --- |
| `/vigo ask prompt:<text> [device] [hidden] [new]` | approved users | asks the device you last worked on; the answer is public in the channel unless `hidden:true`, in which case only you see the question and answer |
| `/vigo status` | approved users | the channel's session, its device, tokens and cost |
| `/vigo devices` | approved users | what is up, and which one a question would go to |
| `/vigo stop` | owner | interrupts the channel's run |
| `/vigo allow user:<who> [for:<2h｜1d｜7d｜perm>]` | owner | lets someone ask, for a time; 1 day when unnamed |
| `/vigo deny user:<who>` | owner | takes that allow back |
| `/vigo allowed` | owner | who may ask, and until when |
| `/vigo help` | anyone | what the bot does |

`/vigo allow`, `deny`, `allowed`, `stop` and every answer about the devices
are ephemeral — only you see them.

The allowlist is kept in a message the bot owns: in `stateChannel` when the
file names one, otherwise in the bot's DM with you. It is read before every
decision, so an allow made from the Mac holds on the PC at once, and it
survives restarts. If that message cannot be read — a channel the bot cannot
see, or a message edited into something it cannot parse — changes stop with an
error naming the problem, and nothing is overwritten. Prefer `stateChannel`: a
DM works only when you and the bot share a server that lets you receive DMs
from its members.

## Which device answers

1. `device:` on the command, if you named one.
2. Otherwise, the device whose OpenCode session has the newest message from a
   person, if that is inside `activityWindow`. That is where you were typing.
3. Otherwise, the first device in the file that answers at all.
4. If none answers, the question fails with the device list, and says how to
   start a server.

Because the answer needs that device's OpenCode to be running, a question
fails when no device is up — that is the honest answer, not a queue.

## Permissions

When the agent wants to run a command or write a file, OpenCode raises a
permission request. `vigo` sends it to your DMs:

> **🔐 Permission needed** on `macbook`
> The agent asks to run `bash`:
> > `git status`
> **[Allow once] [Always allow] [Deny]**

Clicks from anyone but the owner are refused and change nothing. The run
waits until you answer; the channel message says it is waiting. `Always allow`
saves the rule in OpenCode for that project.

OpenCode's own permissions rules still apply — `vigo` does not loosen them.
If a project's config allows everything, the bot will not ask, so decide what
that project may do in its own `opencode.json`.

## Security

- Bind `opencode serve` to your LAN or a VPN (Tailscale, WireGuard), never to
  the public internet. The password is Basic auth over plain HTTP.
- Use a password made for this, not one you reuse: it grants shell execution
  on that machine. **One per device**, so a secret that leaks reaches one
  machine and not both.
- Keep `~/.config/vigo/vigo.json` at mode 0600 — the program refuses to
  start otherwise — and never commit it. `git status` should never show it.
- Invite the bot only to servers you control. Its commands are guild-scoped to
  each server it is in.
- The bot stores nothing about what was said: no message text in the log, no
  database. Sessions live in OpenCode; the Discord channel holds the answers.

### Keeping the secrets off the disk

Each secret — `token`, and every device's `password` — may be given three ways,
one of them only:

| Key | What it is |
| --- | --- |
| `token` / `password` | the value, in the file |
| `tokenFile` / `passwordFile` | a file holding it; mode 0600 or stricter, relative paths taken from the settings file's folder |
| `tokenCommand` / `passwordCommand` | a command whose output is the secret; run once at start |

Each takes one form only: naming two of them stops the bot.

On the Mac, the login Keychain is the natural home, and macOS keeps that
item's key Encrypted with the Secure Enclave-wrapped volume key:

```sh
security add-generic-password -a "$USER" -s vigo-token -w 'the bot token' -U
security add-generic-password -a "$USER" -s vigo-macbook -w "$(openssl rand -hex 32)" -U
```

then the settings file holds no secret at all:

```jsonc
"tokenCommand": "security find-generic-password -s vigo-token -w",
"devices": [
  { "name": "macbook", "url": "http://127.0.0.1:4096",
    "directory": "/Users/you/Projects/ScarletNX",
    "passwordCommand": "security find-generic-password -s vigo-macbook -w" },
  { "name": "pc", "url": "http://192.168.1.20:4096",
    "directory": "C:/Users/you/Projects/ScarletNX",
    "password": "the PC's own password" }
]
```

**What this does and does not buy.** OpenCode checks a plaintext password, so
whatever holds the secret has to hand that plaintext to `opencode serve` and to
this bot when they start. The Enclave therefore protects the secret *at rest*
(no plaintext on disk, the Keychain's own access control, no copy in a backup
of the settings file) — not its use: a process running as you can read the
environment of another. To keep the peer's password off this machine as well
you would need a key exchange OpenCode does not have, so the *other* device's
password is necessarily here; use a different one there so it unlocks nothing
else.

On Linux the same idea is `systemd-creds` sealed to the TPM, and `deploy/`
holds a working pair of units for it:

```sh
# this machine's opencode password, and the bot token, each sealed to it
printf '%s' "$(openssl rand -hex 32)" | systemd-creds encrypt --user \
    --name=opencode-password - ~/.config/vigo/opencode.cred
printf '%s' 'the bot token' | systemd-creds encrypt --user \
    --name=vigo-token - ~/.config/vigo/vigo-token.cred

install -D -m 755 deploy/opencode-serve.sh ~/.local/libexec/opencode-serve.sh
install -D -m 644 deploy/opencode-serve.service ~/.config/systemd/user/
install -D -m 644 deploy/vigo.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now opencode-serve vigo
```

The unit passes the token to the bot as `$CREDENTIALS_DIRECTORY/vigo-token`,
so the settings file says:

```jsonc
"tokenCommand": "cat \"$CREDENTIALS_DIRECTORY/vigo-token\""
```

A `.cred` file decrypts only on the machine (and user, with `--user`) that
sealed it — copying it to the other device gains nothing, which is the point.

### The two devices cannot hold the same file

Every daemon needs the password of *every* device, because it reaches each one
over HTTP. So each machine can seal **its own** password and must hold the
**peer's** password some other way — plainly in its 0600 settings file, or in a
0600 file. That asymmetry is unavoidable without a key exchange OpenCode does
not offer:

| On the Mac | On the PC |
| --- | --- |
| `macbook.passwordCommand` → Keychain | `macbook.password` → the Mac's password, in the file |
| `pc.password` → the PC's password, in the file | `pc.passwordCommand` → `systemd-creds decrypt …` |

Keep the two passwords different: the copy that sits in plaintext should unlock
the least it can.

And the quieter answer, which is usually enough: a 32-byte random password in a
0600 file, over a VPN rather than the open LAN.

On Windows the equivalent of sealing is DPAPI: a small launcher can read an
`CryptProtectData` blob and set `OPENCODE_PASSWORD` from it, and the bot's
settings can use `passwordCommand` the same way. It protects the file at rest
with the same caveat about the running process.

## What it stores

| Where | What |
| --- | --- |
| `~/.config/vigo/vigo.json` | the settings, mode 0600 |
| `~/.local/state/vigo/state.json` | channel → session, permission card tickets, the allowlist message's id; folder 0700, file 0600 |
| Discord | the answers, and the allowlist message |

A state file that cannot be read is kept as `state.json.bad` and a fresh one
starts, with an error in the log. It holds no conversation text.

## Tests

```sh
python3 -m unittest discover -s tests -t . -v
```

No network and no Discord: a fake Discord REST API, a fake gateway and a fake
OpenCode on 127.0.0.1. The suite covers the settings file and its refusals,
message chunking and code fences, device routing, the allowlist across two
devices, the command surface and who may use it, the permission cards
(approved, refused, a stranger's click), a full ask with streamed deltas, the
gateway's handshake, heartbeats and resume, and two daemons answering one use
exactly once.

CI runs the same suite on Linux, macOS and Windows for Python 3.12, 3.13 and
3.14, plus a lint and a check that no token is in the tree.
## Troubleshooting

| What you see | What it means |
| --- | --- |
| `closed with 4004` | the token is not the bot's; reset it and copy it again |
| `closed with 4014` | the bot asked for an intent Discord does not allow it; the bot asks only for `GUILDS` |
| `no device answers` | no `opencode serve` is reachable from this daemon — check the URL, the port, and the password |
| `Permission needed` never arrives | the owner and the bot do not share a server that allows DMs, so the card falls back to the channel |
| the answer says `still running` | the run passed `timeout`; `/vigo status` shows it, `/vigo stop` ends it |
| a channel has no session | the first ask creates one; `new:true` starts a fresh one on purpose |
