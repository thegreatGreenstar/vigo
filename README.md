# vigo

Your OpenCode agent, on your own machines, reachable from Discord.You ask in a channel with `/vigo ask`, and the answer comes back there. The
work runs on your Mac or your PC, whichever one you last used, against the
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
  session in the project directory, sends the prompt, and turns the deferred
  message into a short "got it" before the answer arrives as its own message.
- Each Discord channel remembers one OpenCode session per device, so
  follow-ups continue the same conversation. Both daemons and any restart
  find that session rather than making another: the device marks the session
  it made with the channel it belongs to, and a daemon whose own state has
  forgotten adopts it. Only `/vigo ask new:true` (or a cleared session) starts
  a fresh one.

Only one daemon answers a given use: they all try to acknowledge it at once,
Discord takes the first, and the rest are told it was already answered and
stop. Whoever wins can drive every device, so it does not matter which one it
is, and a device answers even when its own daemon is not running.

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
5. Add it to your own account. No server and no invite permission are needed:

   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&integration_type=1&scope=applications.commands
   ```

   In the Discord client it appears under **Settings → Apps**, and `/vigo`
   works in your DMs with it, in group DMs, and in the command picker of any
   server you are in, even servers the bot is not a member of.
6. Get **your** numeric user id: Settings → Advanced → Developer Mode on, then
   right-click your avatar → **Copy User ID**. The username is not enough.
   every check here is an id.

If you also want to invite it to a server (its own avatar posting there), use
the `bot` scope and these permissions: View Channels, Send Messages, Read
Message History:

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=68608
```

The command is registered globally on the application, so it serves both kinds
of install at once. Discord takes up to an hour to show a changed command; the
first start is usually a minute.

**What a user install can and cannot do.** The acknowledgement, the answer and
extra messages all go through the interaction's own token, so they work in a
channel where the bot is not a member. Two things need the bot in the channel:
posting an answer that arrives *after* the interaction's 15 minutes, and posting
a fallback line when a DM is impossible. Both fall back with a line in the log;
the normal paths do not need it.

### 2. OpenCode on each device

Run OpenCode's own server with a fixed password, bound to the **Tailscale** address, so it is reachable from the
other machine and from nothing else:

```sh
OPENCODE_PASSWORD='something-long-and-not-reused' \
  opencode serve --hostname "$(tailscale ip -4)" --port 4096
```

If Tailscale is not up yet, bind `127.0.0.1` for now and come back to "Reaching the other device".

Check it answers (from this machine):

```sh
curl -u "opencode:$OPENCODE_PASSWORD" http://127.0.0.1:4096/api/info || \
curl -u "opencode:$OPENCODE_PASSWORD" "http://$(tailscale ip -4):4096/api/info"
```

`/api/info` also tells you the hostname this machine goes by.

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
| `install` | `both` (default), `user` or `guild`: which install kinds the command supports. Keep `both`, because a user install can be used in servers too |
| `model` | provider, model id, optional `variant` |
| `devices` | name, base URL and project directory of each machine |
| `activityWindow` | seconds of "recent use" that make a device the one to use (0 disables) |
| `timeout` | seconds before a run says the device is still working (30–3600); it keeps watching |
| `editEvery` | unused; an older settings file that names it still loads |
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

### Saying something to the owner from a session

The bot can DM you from the machine the settings live on, which is what an
OpenCode session that finished a job needs — no Discord channel involved:

```sh
python3 -m vigo dm "the tests pass and the branch is ready"
echo "longer text" | python3 -m vigo dm
```

It uses the same token from `tokenCommand`, sends to `owner`, and exits
non-zero with the reason when Discord will not take it. `/vigo upgrade` uses
the same path to tell you what it moved, naming each device and the commit it
went from and to.

### 5. The other device

The second machine needs the repo, Python 3.12 or newer, and its own entry in the settings. Clone it over SSH and run
the installer:

```sh
git clone git@github.com:thegreatGreenstar/vigo.git
cd vigo
./install.sh
```

It checks the Python, makes `~/.config/vigo/vigo.json` from the example (never overwriting one), installs
`~/.local/libexec/opencode-serve.sh`, and, where systemd exists, both user units, pointed at that checkout and that
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
3. Reach each other over the **Tailscale** address, never the LAN address and never a port forwarded from the
   router. See "Reaching the other device".


### Reaching the other device

What crosses between the machines is a shell and a password, so the only thing in front of it is the tunnel. Two ways
to make one; the first is what this setup uses.

**Tailscale.** Both machines on the same tailnet, and each service bound to the tailnet address rather than the LAN:

```sh
opencode serve --hostname "$(tailscale ip -4)" --port 4096
```

then the settings name each machine by its tailnet address and nothing else:

```jsonc
// on macbook
{ "name": "pc", "url": "http://100.64.1.20:4096", "directory": "C:/Users/…" }
// on pc
{ "name": "macbook", "url": "http://100.64.0.10:4096", "directory": "/Users/…" }
```

Add `"peerPort": 4098` and `"peerHost": "0.0.0.0"` on both, or set `"peer"` on each device entry to point at the
other's tailnet address, and `/vigo upgrade` works across the pair. Binding to the tailnet address rather than
`0.0.0.0` means the port is not on the café Wi-Fi at all.

**SSH, instead.** No VPN daemon and no account; both services stay on loopback and the bot's machine forwards the
ports:

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -L 4097:127.0.0.1:4096 -L 4099:127.0.0.1:4098 \
    user@pc
```

```jsonc
{ "name": "pc", "url": "http://127.0.0.1:4097",
  "directory": "C:/Users/…", "peer": "http://127.0.0.1:4099" }
```

`peer` must be given by hand with a tunnel: the port is derived from the device's URL host only, and here the local
port would collide with this daemon's own. Keep the tunnel up with `deploy/dev.vigo.tunnel.plist` (macOS) or
`deploy/vigo-tunnel.service` (Linux); both restart it when it drops.

Either way the password still matters, because it is the only check OpenCode makes, but the port is never reachable by
anything that is not on the tailnet or holding the SSH key.

### 6. Start on login

`./install.sh` puts both services in place, and they come up when you log in, not at boot, so nothing waits on a
password and nothing runs before your Keychain is unlocked.

- **macOS**: two LaunchAgents, `dev.vigo.opencode` and `dev.vigo.bot`, both with `RunAtLoad` and `KeepAlive`. Check
  them with `launchctl print gui/$(id -u)/dev.vigo.bot | head -20`, read their output in
  `~/.local/state/vigo/*.err`, and stop one with `launchctl bootout gui/$(id -u)/dev.vigo.bot`.
- **Linux**: two systemd user units. `systemctl --user enable --now opencode-serve vigo`; they start at login too
  unless you run `loginctl enable-linger $USER`, which starts them before you log in and is only useful if the
  password does not need your login keychain.

Both restart themselves after a crash, which matters at login: OpenCode binds the tailnet address, so it fails until
Tailscale is up, and the supervisor simply tries again.

## The Discord application, in words

What the Developer Portal asks for, if it helps:

- **Name**: `Vigo`. **Bot username**: `vigo`
- **About me**: *Vigo is my OpenCode agent on my own machines. I ask from a
  channel, the work runs on my Mac or PC, and every tool that needs a
  permission waits for my click. Nothing is stored: no message text, no logs of
  what was said.*
- **Tags**: `AI`, `Productivity`, `Developer Tools`, `Utilities`
- **Icon**: `art/vigo-icon-41.png` (512×512), Aogo's blossom, petals shifted to
  DeepSeek blue, **41** on the wing where Aogo had 55 (Opus 5.5), and **VI** on
  the hat tag where Aogo had AO. Rebuild it with `./art/make-icon.sh`;
  `art/recolor.py` and `art/badge.swift` are what it runs, and
  `art/aogo-source.png` is the original.

## The commands

| Command | Who | What happens |
| --- | --- | --- |
| `/vigo ask prompt:<text> [device] [goal:<true｜false>] [hidden] [new]` | approved users | asks the device you last worked on; the question is acknowledged at once and the answer follows as a new message, public in the channel unless `hidden:true`, in which case only you see the question and answer |
| `/vigo goal objective:<text> [device] [hidden]` | approved users | gives the channel's agent an objective to keep working toward, with no question; `pause`, `resume` and `clear` control the goal it already has |
| `/vigo quick prompt:<text> [device] [hidden]` | approved users | one answer, with no session and no tools |
| `/vigo status` | approved users | the channel's session, its device, tokens and cost; the next three questions that wait, and the last three that ran |
| `/vigo devices` | approved users | what is up, and which one a question would go to |
| `/vigo stop` | owner | interrupts the channel's run, and drops the questions that wait behind it |
| `/vigo upgrade [device]` | owner | pulls and restarts a daemon that runs a different commit; empty does every one of them, and the owner gets a DM naming each device and the commit it moved from and to |
| `/vigo allow user:<who> [for:<2h｜1d｜7d｜perm>]` | owner | lets someone ask, for a time; 1 day when unnamed |
| `/vigo deny user:<who>` | owner | takes that allow back |
| `/vigo allowed` | owner | who may ask, and until when |
| `/vigo help` | anyone | what the bot does |

`/vigo status`, `devices`, `upgrade`, `allow`, `deny` and `allowed` are
always ephemeral: only the person who ran it sees the answer, and no option
could show it to a channel. `/vigo stop` takes `hidden` like the questions do.

One question runs in a channel at a time. A question asked while one runs
waits its turn, and the message it came from says how many run before it. The
line holds ten. `/vigo stop` clears it. `/vigo status` names the next three and
the last three that ran.

The deferred message becomes "Got it, working on it" at once, and the answer
is a new message when the agent is done, so Discord notifies you. The answer
carries its first two paragraphs, at most one Discord message. A follow-up in
the same session reaches the rest. A run that passes `timeout` says the device
is still working and keeps watching. The answer still arrives, and the channel
stays busy until it does. No progress line is posted. If the device is not
working on it by then — nothing on the session is newer than the question — the
run ends and says so, because a device that took the question and will not run
it would hold the channel for ever with every later question behind it.

An ask that is a task gives the agent a **goal**, and it works toward that
until it is met instead of answering once. A question stays a question:
`/vigo ask goal:true` or `goal:false` says which it is when the wording is read
wrong. `/vigo goal objective:…` sets one without asking anything, and
`/vigo goal pause`, `resume` and `clear` control the one the channel has.
`/vigo status` names the goal and the state the device keeps for it, including
a goal the device paused because it restarted under it.


A run is not always the daemon's that answers `/vigo status`: both daemons get
every command, Discord takes the first acknowledgement, and either may restart
under a run. So `status` asks the device's own session, which is the record
they share, and reports the device working when it is, even with no run in
this process. A question asked then is refused with that reason instead of
joining the agent's turn; `new:true` still starts a fresh session.

The allowlist is kept in a message the bot owns: in `stateChannel` when the
file names one, otherwise in the bot's DM with you. It is read before every
decision, so an allow made from the Mac holds on the PC at once, and it
survives restarts. If that message cannot be read, because a channel the bot cannot
see, or a message edited into something it cannot parse, changes stop with an
error naming the problem, and nothing is overwritten. Prefer `stateChannel`: a
DM works only when you and the bot share a server that lets you receive DMs
from its members.

## Protecting `main`

`main` takes pull requests only, so a failing CI matrix refuses the merge. On a
private repository GitHub's branch protection and rulesets are paid features,
so the guard that exists today is a hook every clone installs (`install.sh`
sets `core.hooksPath`, and `git config core.hooksPath .githooks` does it by
hand):

```sh
VIGO_ALLOW_MAIN=1 git push      # the deliberate exception, and the only one
```

When the repository is public or on Pro, the same rule belongs on GitHub, where
it also covers machines that never installed the hook:

Only the full-matrix names are worth requiring: the `quick` jobs exist for
drafts, and a draft cannot merge anyway.

```sh
gh api -X PUT repos/thegreatGreenstar/vigo/branches/main/protection --input - <<'JSON'
{"required_status_checks": {"strict": true, "contexts": ["lint", "no secrets in the tree",
  "ubuntu-latest · Python 3.12", "ubuntu-latest · Python 3.13", "ubuntu-latest · Python 3.14",
  "macos-latest · Python 3.12", "macos-latest · Python 3.13", "macos-latest · Python 3.14",
  "windows-latest · Python 3.12", "windows-latest · Python 3.13", "windows-latest · Python 3.14"]},
 "enforce_admins": true, "required_pull_request_reviews": null, "restrictions": null,
 "allow_force_pushes": false, "allow_deletions": false}
JSON
```

## Which device answers

1. `device:` on the command, if you named one.
2. Otherwise, the device whose OpenCode session has the newest message from a
   person, if that is inside `activityWindow`. That is where you were typing.
3. Otherwise, the first device in the file that answers at all.
4. If none answers, the question fails with the device list, and says how to
   start a server.

Because the answer needs that device's OpenCode to be running, a question
fails when no device is up. That is the honest answer, not a queue.

## Permissions

When the agent wants to run a command or write a file, OpenCode raises a
permission request. `vigo` sends it to your DMs:

> **Permission needed** on `macbook`
> The agent asks to run `bash`:
> > `git status`
> **[Allow once] [Always allow] [Deny]**

Clicks from anyone but the owner are refused and change nothing. The run
waits until you answer, and the channel message says approval is needed.
`Always allow` saves the rule in OpenCode for that project.

When the agent asks you a question of its own, it reaches the same DM as a card
with a button for each choice and an **Other** button for a typed answer. The
run waits for the answer, and other channels keep working. The card and its
answer never appear in the channel.

OpenCode's own permissions rules still apply. `vigo` does not loosen them.
If a project's config allows everything, the bot will not ask, so decide what
that project may do in its own `opencode.json`.

## Security

- Bind `opencode serve` to your LAN or a VPN (Tailscale, WireGuard), never to
  the public internet. The password is Basic auth over plain HTTP.
- Use a password made for this, not one you reuse: it grants shell execution
  on that machine. **One per device**, so a secret that leaks reaches one
  machine and not both.
- Keep `~/.config/vigo/vigo.json` at mode 0600, because the program refuses to
  start otherwise, and never commit it. `git status` should never show it.
- Invite the bot only to servers you control. Its commands are guild-scoped to
  each server it is in.
- The bot stores nothing about what was said: no message text in the log, no
  database. Sessions live in OpenCode; the Discord channel holds the answers.

### Keeping the secrets off the disk

Each secret, `token` and every device's `password`, may be given three ways,
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
of the settings file), not its use: a process running as you can read the
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
sealed it, copying it to the other device gains nothing, which is the point.

### The two devices cannot hold the same file

Every daemon needs the password of *every* device, because it reaches each one
over HTTP. So each machine can seal **its own** password and must hold the
**peer's** password some other way, plainly in its 0600 settings file, or in a
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
| `~/.local/state/vigo/state.json` | channel → the session it has on each device, permission card tickets, the allowlist message's id; folder 0700, file 0600 |
| Discord | the answers, and the allowlist message |

A state file that cannot be read is kept as `state.json.bad` and a fresh one
starts, with an error in the log. It holds no conversation text.

## Platforms

macOS (launchd) and Linux (systemd) are the hosts. Windows is in the CI matrix
for the portable mistakes — a path, a clock, a socket — and one thing is known
to differ there: shutting a socket down does not wake a read that is already
blocked, so `EventStream.close` frees the connection but the reader thread
waits for the device to speak. `tests/test_stream.py` skips that one assertion
on Windows and says why.

## Tests

```sh
python3 -m unittest discover -s tests -t . -v   # the canonical run
python3 -m tests.parallel                       # the same suite on threads
tests/in-containers.sh                          # Linux 3.12-3.14, in podman
```

`tests/in-containers.sh` runs the suite in `python:3.12`, `3.13` and `3.14`
containers on the `podman machine` Linux VM, so the Linux half of the CI matrix
is answered here first. It cannot answer Windows or macOS: `podman` on a Mac
is always Linux, and Windows behaviour has already differed once (a socket read
that `shutdown` does not wake there).

The product code is typed: every parameter and return in `vigo/` carries an
annotation, and a test fails on a missing one or on one that does not resolve.
A full type checker is not wired in yet.

The parallel runner is standard library only: every test builds its own
temporary directory and its own fakes on port 0, so nothing is shared and the
cases can run beside each other. `--slowest 5` names where the time goes, and
the canonical runner above stays what CI uses — a fault in the runner would
show up there rather than hide here.

No network and no Discord: a fake Discord REST API, a fake gateway and a fake
OpenCode on 127.0.0.1. The suite covers the settings file and its refusals,
message chunking and code fences, device routing, the allowlist across two
devices, the command surface and who may use it, the permission cards
(approved, refused, a stranger's click), a full ask with streamed deltas, the
gateway's handshake, heartbeats and resume, and two daemons answering one use
exactly once.

CI runs the same suite on Linux, macOS and Windows for Python 3.12, 3.13 and
3.14, plus a lint and a check that no token is in the tree. It does that in two
stages, so a draft answers the cross-platform question quickly:

| stage | what runs | why |
| --- | --- | --- |
| draft pull request | `quick`: one Python (3.14, the newest in the matrix) on all three OSes | a path, a socket or a clock that differs by platform shows up in one job per OS instead of three |
| ready for review, or a push to `main` | `test`: all three OSes × 3.12, 3.13, 3.14 | version-specific breakage, which one Python cannot show |

Lint and the secrets check are the same in both, because they are cheap and
platform-free. Marking a draft ready runs the full matrix; converting it back
to a draft runs the quick one again.
## Troubleshooting

| What you see | What it means |
| --- | --- |
| `closed with 4004` | the token is not the bot's; reset it and copy it again |
| `closed with 4014` | the bot asked for an intent Discord does not allow it; the bot asks only for `GUILDS` |
| `no device answers` | no `opencode serve` is reachable from this daemon: check the URL, the port, and the password |
| `Permission needed` never arrives | the owner and the bot do not share a server that allows DMs, so the card falls back to the channel |
| the answer says `still running` | the run passed `timeout`; it keeps watching, `/vigo status` shows it, `/vigo stop` ends it |
| the answer says `never started` | the device admitted the question and its session never began the turn: `opencode serve` there is stuck, so restart it and ask again |
| a goal says `paused` | the device restarted while the goal was active; `/vigo goal resume` starts it again |
| a question says `Queued` | one run at a time per channel; it starts when the one before it ends, or `/vigo stop` clears it |
| a channel has no session | the first ask creates one; `new:true` starts a fresh one on purpose |
