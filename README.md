# vigo

Your OpenCode agent, on your own machines, reachable from Discord. You ask in a channel with `/vigo ask`, and the answer comes back there. The
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
stop. Whoever wins can drive every device, so the device answers even when its
own daemon is not running. When the two daemons run different code, the one
with the older checkout steps out before that acknowledgement for a question,
so the newer code answers: it queues where the older one refuses, and it does
not put a second prompt into a session the other is working. A peer is asked
for its commit's time for this, kept a few seconds, and skipped when it does
not answer.

### GitHub work

An answer that reports work on GitHub reads the way the maintainers of the
project report it on GitHub: the verdict in the first sentence, then the facts
that matter with their numbers (a pull request or issue, a commit, a file and
line, a count of tests), then what still waits for a person. It is short. In
that project the replies run to about fifty words, and a list of four facts is
the long one.

A pull request and an issue the agent writes end with the line `From Vigo (an
agent)`, and a comment starts with it. One mark only, and never the name of the
model that ran the turn. The model changes from question to question, and the
agent that asked it does not.

The agent is told to write it that way. vigo adds the one part a machine does
better: a first line with the work itself, in the order it was made. It stands
where the byline stands in those replies, provenance before the verdict, and it
cannot forget an artifact the prose mentions.

```
GitHub: [PR #1251](https://github.com/scarletnx/ScarletNX/pull/1251) · [commit aaf1a0d](…)

the fix is right, and the test it lacked is on top of the branch.
```

The line is not a second report: the agent says what it did and what is left,
in its own words, and the line only names the work it points at. That line
holds only what the answer already names: nothing is fetched from GitHub, so a
private project stays private, and a device with no `gh` still reports. A link
the answer puts on its own first line is not repeated, so the line is always an
addition.

## What can and cannot happen

- **Who may ask** is decided by the daemon from its own state, never from the
  text of a message. The owner, the ids in the settings file, and the people
  the owner allowed with `/vigo allow` may ask. Anyone else gets an ephemeral
  "no" and nothing is sent to any device, ever. With `requests` on, that "no"
  carries one button, **Ask for access**, which DMs the owner a card naming the
  person and offering **Allow 1 day**, **Allow for good** or **Deny**. The
  owner's click is the grant. The request itself runs nothing.
- **Owner commands belong to the application's owner.** Vigo reads that id
  from the Discord application at start. A second person who runs their own
  Vigo owns that application, so `status`, `queue`, `devices`, `stop`,
  `upgrade`, `goal`, `allow`, `deny` and `allowed` answer to them, and to none
  of that app's guests. A file that names a different `owner` stops the bot.
- **An allow is permission to ask, not to drive the machine.** An approved
  person gets questions, not work: their ask never becomes a goal, never picks
  a device, and never runs in the owner's session. Their question and its answer
  are private to them, and their session is their own, so the owner's
  conversation is not read into it and nothing about the machine lands in the
  channel. The agent still reads what the machine lets it read, and the answer
  goes to the person who asked: an allow is trust in that person, not only in
  their questions. They may have one question in flight at a time, and they
  cannot use `status`, `queue`, `devices`, `stop`, `upgrade`, `goal`, `allow`,
  `deny` or `allowed`.
- **Allowed users can ask, but cannot approve.** Tool permissions go to the
  owner's DMs, and only a click there runs a tool. An approved person's card
  offers **Allow once** and **Deny**, never **Always allow**: one use of a tool
  is granted, not a standing rule the next question inherits.
- **An allow that ends stops the question too.** The list is read again when a
  queued question starts, so `/vigo deny` reaches a question that was already
  waiting, and an allow that ran out refuses the next one.
- **What an allow may spend is capped.** The plan is the owner's, so approved
  people together may use a third of it. Their questions stop there, with a
  message that says so, and the owner is never capped: every question they ask
  runs. An approved person's question runs in a session of its own, because
  the plan's usage is counted by session. "The share of the plan an allow
  carries" below says how it is measured and what to set.
- **Nothing is stored about what was said.** No message text is logged; the
  log has ids, outcomes and durations. Answers live in Discord, as they do for
  any bot.
- **The bot asks for no privileged intents.** It asks for `DIRECT_MESSAGES` only.
  A direct message is read only when it replies to a message the bot posted, and
  the text is never stored. There is no message-content intent to enable.
- **The token never enters the repository.** Settings live in
  `~/.config/vigo/vigo.json` (mode 0600); the repo ships only an example.
  CI fails if a `vigo.json` or a bot token shape reaches the tree.
- **The peer API has its own password.** It is separate from the OpenCode password, kept in the daemon
  only, so a compromised agent can use OpenCode but cannot call `/vigo/update` or restart the daemon.
- **The OpenCode password is the real trust boundary.** Anyone with it can run
  anything on that device, so it should not be a password you use elsewhere,
  and the port should not face the internet. See [Security](#security).

## Setup

### 1. A Discord application

1. <https://discord.com/developers/applications> → **New Application**, name it
   `Vigo`.
2. **General Information** → copy the **Application ID**.
3. **Bot** → **Reset Token**, copy the token. Leave **Public Bot** on if other
   people will install it: Discord only gives a shareable install link to a
   public app. Turn it off only for a bot that is just yours, and then set
   **Install Link** to **None**. Public Bot alone puts the bot in no server.
4. **Installation** → under **Installation Contexts** turn on **User Install**
   and leave **Guild Install** off. Set **Install Link** to **Discord Provided
   Link**. Under *Default Install Settings*, add `applications.commands` to the
   user-install scopes, and **Save Changes**.
5. Add it to your own account. No server and no invite permission are needed:

   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&integration_type=1&scope=applications.commands
   ```

   In the Discord client it appears under **Settings → Apps**, and `/vigo`
   works in your DMs with it, in group DMs, and in the command picker of any
   server you are in, even servers the bot is not a member of.
6. You do not need to copy your own user id. Vigo reads the application's
   owner from Discord at start, so whoever owns the application also owns its
   `status`, `queue`, `devices`, `stop`, `upgrade`, `goal`, `allow`, `deny` and
   `allowed` commands. If the settings file names `owner` anyway, it must be
   that same id or the bot stops.

If you also want to invite it to a server (its own avatar posting there), turn
on **Guild Install** in the same **Installation** page, use the `bot` scope and
these permissions: View Channels, Send Messages, Read Message History:

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=68608
```

The command is registered globally on the application, so it serves both kinds
of install at once. Discord takes up to an hour to show a changed command; the
first start is usually a minute.

**What a user install can and cannot do.** The acknowledgement, the answer and
extra messages all go through the interaction's own token, so they work in a
channel where the bot is not a member. One thing needs the bot in the channel:
the answer once the token is gone. Discord stops taking the token 15 minutes
after the interaction, and a question that waits in the channel's queue spends
part of that time before it starts. Then the answer goes to the channel with the
bot's own token, and when that is refused — the bot is not a member — it goes to
a direct message with the person who asked. A `hidden` answer has no ephemeral
message left to go in, so that direct message takes it, named as the answer to
its question. A goal that runs for an hour therefore still reaches the person
who asked for it.

### Letting other people use it

A user install is the whole door. You keep the daemon, the token, the
machines and the plan. Another person adds the app to their own account and
asks you for access. Nothing runs for them until you click. The same
walkthrough, with the portal pages in order and the copy-paste commands, is
in [docs/OTHERS.md](docs/OTHERS.md).

**In the Developer Portal.** `python3 -m vigo --invite` prints these pages
with your application's id, checks the settings below, and exits
non-zero while one is still wrong. The steps are:

1. **Installation**: open
   <https://discord.com/developers/applications>, choose your app, then
   **Installation**.
   - **Installation Contexts**: **User Install** on, **Guild Install** off.
   - **Install Link**: **Discord Provided Link**.
   - **Default Install Settings** → **User Install**: add the
     `applications.commands` scope.
   - **Save Changes**.
2. **Bot**: turn **Public Bot** on. Discord only gives a shareable install link
   to a public app. With it off, only your team can install it. Public Bot
   does not put the bot in any server by itself.
3. **General Information**: copy the **Application ID** into `appID` if it is
   not already there. Never put the token in a channel, an issue or a commit.
4. In the settings file set `"install": "user"`, because the app now offers
   only User Install, and leave `"requests": true` unless you want the
   request button gone. Restart the daemon:

   ```sh
   systemctl --user restart vigo                        # Linux
   launchctl kickstart -k gui/$(id -u)/dev.vigo.bot     # macOS
   ```

5. Check the portal settings and print the link to share:

   ```sh
   python3 -m vigo --invite
   ```

   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&integration_type=1&scope=applications.commands
   ```

Anyone with that link can add Vigo under **Settings → Apps**. `/vigo` then
appears in their DMs, in group DMs, and in the command picker of every server
they are in, including servers the bot has never joined. No server install is
needed.

**When the other person should own their own Vigo.** A guest asks on your
machines. If they want their own agent, they create their own application and
run their own daemon with their own token, as the setup above does. Vigo reads
that application's owner, so `status`, `queue`, `allow` and the rest answer to
them, and their guests never get them. Their plan, machines and allowlist are
theirs, and nothing is shared with yours.

**What the person gets.** `/vigo ask`, and every command except `/vigo help`,
answers with a short refusal and one button, **Ask for access**. The button
DMs you a card with their display name and three answers: **Allow 1 day**,
**Allow for good**, **Deny**. Only the application owner's id can press them. A
request on its own runs nothing and touches no device. If the card cannot
reach your DM, the person is told so and the request is not left half-open.

The person is told the decision in a DM. An allow is the same allow
`/vigo allow` makes: questions only, their own session, their own private
answer, one question in flight, no device choice, no goal, and the share of
your plan kept for approved people. Every tool they cause still comes to you
as a card, with **Allow once** and **Deny** and no **Always allow**. [What can
and cannot happen](#what-can-and-cannot-happen) and [The share of the plan an
allow carries](#the-share-of-the-plan-an-allow-carries) say the rest.

A request costs you at most one DM per person per hour: a pending request is
not sent twice, and a denial starts a one-hour quiet period. A request that
waits a day without an answer stops blocking that person, who may ask again,
and twenty requests may wait at once. `"requests": false` removes the button
and tells a stranger to ask you directly, without changing anyone who is
already allowed.

**What a public app does and does not expose.** The token stays on your
machine, the OpenCode password never goes to Discord, and the commands run in
your daemon. A stranger who installs the app can only ask you for access.
Keep **Guild Install** off: that is the setting that lets the app be added to
a server, and a public app can be added to anyone's server. If you want the
bot in a server, turn Guild Install on, use the `bot` invite link, set
`"install": "both"`, and accept that the bot posts there with its own avatar.

The link works before the app is in the App Directory. A listing is a
separate review and this setup does not need one. Discord's verification
thresholds are about server installs and privileged intents. Vigo asks for
neither a server install nor a privileged intent, and `DIRECT_MESSAGES` is the
one gateway intent it uses.

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
  // "owner": "145512144145512144",    // optional: the application's owner is used
  "device": "macbook",                 // which entry this copy of vigo is
  "password": "the OPENCODE_PASSWORD",
  "install": "both",                   // "user" when the app offers only User Install
  "requests": true,                    // the Ask for access button for a stranger
  "users": [],                         // ids always allowed to ask
  "model": { "providerID": "opencode-go", "id": "deepseek-v4.1-flash" },
  "guests": {                           // what approved people may spend
    "share": 0.333,                     // of the plan's rate limit, together
    "hourly": 500000, "daily": 2000000, // tokens, all of them
    "perUserHourly": 250000, "perUserDaily": 1000000
  },
  "devices": [
    { "name": "macbook", "url": "http://vigo-opencode:4096",
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
| `token`, `appID` | from the Developer Portal |
| `owner` | optional. The application owner's user id. When absent, Vigo reads it from Discord at start. When present it must match that id, or the bot stops |
| `device` | this machine's entry; the same file runs on both, with this line changed |
| `password` | the OpenCode password; a device may set its own with `password` in its entry |
| `users` | ids that may always ask, besides the owner |
| `install` | `both` (default), `user` or `guild`: which install kinds the commands register for. Use `user` when the app offers only User Install, which is the public setup above. A value the app does not offer makes Discord refuse the commands |
| `requests` | `true` (default) or `false`: whether a refused person gets the **Ask for access** button. Off still refuses, and says to ask the owner directly |
| `model` | provider, model id, optional `variant` |
| `guests` | the share of the plan approved people may spend, and their hourly and daily token caps. A third of the plan and the defaults in the example when absent; 0 turns a cap off, and `share: 0` pauses approved people completely |
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
python3 -m vigo --invite      # asks Discord for the app settings, prints the install link
```

`--invite` is the one check that talks to Discord about the application
itself: it names the Installation and Bot pages, says who owns the
application, and says whether User Install, Public Bot and the
`applications.commands` scope are set. It exits non-zero while the portal and
`install` disagree, or a hand-written `owner` is not the application's.

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

It makes `~/.config/vigo/vigo.json` from the example (never overwriting one) and puts the service files in place. On
macOS that is the Keychain launcher and two LaunchAgents. On Linux it is `vigo-container.service` plus
`vigo-ssh-agent.service`: one rootless container running Vigo **and its own OpenCode**, with systemd unsealing the
secrets. Then:

1. In the settings, set `device` to this machine's name, keep the **same** `devices` list as the other machine, and
   keep this machine's password in its own hardware:
   ```sh
   printf '%s' "$(openssl rand -hex 32)" | systemd-creds encrypt --user \
       --name=opencode-password - ~/.config/vigo/opencode.cred
   ```
   On Linux the device's own `passwordCommand` is `cat /run/secrets/opencode-password`; the unit mounts the unsealed
   file there. "The two devices cannot hold the same file" below says which secret sits where.
2. On macOS, start OpenCode there too, reachable from the other machine:
   ```sh
   OPENCODE_HOSTNAME=0.0.0.0 ./deploy/opencode-serve-macos.sh
   ```
   or load the LaunchAgents. On Linux, make Vigo's TPM identity, add its public keys to `VIGO-VI`, build the image,
   and start the container:
   ```sh
   ./deploy/vigo-identity.sh
   ./deploy/build-container.sh
   systemctl --user enable --now vigo-ssh-agent vigo-container
   ```
   See "Running in a rootless container" below.
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
    user@peer-host
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
- **Linux**: `vigo-ssh-agent.service` and `vigo-container.service`. `systemctl --user enable --now vigo-ssh-agent
  vigo-container`; they start at login unless you run `loginctl enable-linger $USER`. Lingering is safe here because
  systemd unseals the container's credentials without a login keychain, so a boot start needs no person.

Both restart themselves after a crash, which matters at login: the container binds the tailnet address, so it exits
until Tailscale is up, and systemd simply tries again.

### Running in a rootless pod

`deploy/container/` runs two rootless containers in one pod: one network namespace, two file-system
namespaces.

| Container | Holds | Never sees |
| --- | --- | --- |
| `vigo-opencode` | `~/Projects`, its own home and OpenCode state, the agent settings, the `gh` token, the TPM SSH agent | the Discord token, the daemon settings or state |
| `vigo` | the Discord token, the settings, the state, the private checkout `~/.local/share/vigo/checkout` | `~/Projects`, the agent OpenCode data, the `gh` token |

Both drop every capability, refuse new privileges, use a read-only root, a bounded `/tmp`, separate
memory and pid limits, and publish their ports on one named address, never `0.0.0.0`.

#### Linux: the pod

Host-only setup, including packages, sealed credentials, keys, units and optional data/GPU mounts, is in
[`deploy/container/SETUP.md`](deploy/container/SETUP.md).

The kit is installed under `~/.local/libexec/vigo-container`, so `/vigo upgrade` changes Python without
changing how the pod starts:

```sh
./deploy/container/install.sh
./deploy/vigo-identity.sh          # Vigo's TPM auth + signing keys
./deploy/build-container.sh        # daemon + agent images
systemctl --user enable --now vigo-ssh-agent vigo-container
```

`vigo-container.service` uses systemd credentials; `vigo-ssh-agent.service` holds only `~/.ssh-vigo`.
The agent reaches Compose/OpenCode on the pod's loopback; the daemon reaches it at `127.0.0.1:4096`.
Ports 4096 and 4098 are published only on the tailnet address.

The two images share a base but not their tools:

- `localhost/vigo` is the daemon: Python, git and ssh only. It carries no OpenCode and no gh.
- `localhost/vigo-agent` is CachyOS on x86-64 and Arch on arm64, with Clang/LLVM 23, GCC, CMake,
  Ninja, Meson, NASM, ccache, sccache, mold, Rust, Go, Node/npm, Python venv, GDB, the X11 and
  Wayland headers, SDL3, Vulkan with lavapipe and validation layers, SPIR-V tools, Qt 6, Boost,
  FFmpeg, OpenSSL, libusb, RenderDoc and the usual emulator libraries.

On this x86-64 host the agent is the same distribution and glibc as the host, so a binary built in
`~/Projects/compiled_scarlet` runs inside it. A binary built against a newer glibc than the image's
will not; build it in the agent or refresh the image.

That is what ScarletNX and Eden need to configure and build. Anything still missing is a deliberate
image change, not a host mount:

```sh
# a project-specific image on top of the agent's
printf 'FROM localhost/vigo-agent:latest\nRUN apt-get update && apt-get install -y --no-install-recommends libfoo-dev\n' \
    | podman build -t localhost/vigo-agent-eden -f - .
# then set VIGO_AGENT_IMAGE=localhost/vigo-agent-eden in the unit's drop-in
```

Existing project data, such as an emulator's games and saves, is mounted only in the agent. Name the
host paths as a colon-separated list:

```sh
# in the same drop-in
Environment=VIGO_AGENT_DATA=/home/you/.local/share/eden:/home/you/.local/share/eden/keys
```

The agent's OpenCode settings are a read-only copy of the host's `opencode.json`, so MCP servers stay
available. The ScarletNX AppImage and its data folder are mounted only in the agent, and
`APPIMAGE_EXTRACT_AND_RUN=1` lets the AppImage run without `/dev/fuse`. Project data such as
`~/.local/share/scarletnx` is mounted only in the agent when `VIGO_AGENT_SCARLETNX=1` is set. GPU,
Wayland and key access are opt-in:

```sh
# in a vigo-container.service.d/agent.conf drop-in
[Service]
Environment=VIGO_AGENT_SCARLETNX=1  # ~/.local/share/scarletnx, for ScarletNX games
Environment=VIGO_AGENT_GPU=1        # --device /dev/dri/renderD128, for real Vulkan
Environment=VIGO_AGENT_WAYLAND=1    # the host wayland-0 socket
Environment=VIGO_AGENT_KEYS=1       # ~/.switch, read-only, for encrypted dumps
```

Without those, headless runs use lavapipe and the validation layers. A missing key, GPU or display
fails in the project, not in Vigo. Without the GPU, lavapipe's subgroup size is 8 where the console's is
32, so a title whose shaders use vote or shuffle loses those draws: Luigi's Mansion 3 then draws the title
logo on black instead of the hotel. The render node is enough; `card*` adds modesetting and EDID that a
headless game does not use.

Logs:

```sh
journalctl --user -u vigo-container -n 50 -o cat   # the daemon
podman logs vigo-opencode                           # the agent
journalctl --user -u vigo-ssh-agent -n 20 -o cat
```

The daemon runs a private checkout at `~/.local/share/vigo/checkout`, not `~/Projects/vigo`. The agent can edit the
project copy under `~/Projects`, but it cannot change the code the daemon will run. `/vigo upgrade` pulls the private
checkout and ends the daemon; systemd starts the pod again on the new code.

A restart keeps `~/.local/share/vigo-agent` and `~/.local/state/vigo`.

`systemctl --user enable` starts both units at login. They do not start at boot unless you run
`loginctl enable-linger $USER`; then systemd unseals the credentials and the launcher retries until
Tailscale is up.

#### When the host OpenCode is the right runner

The pod is not the host. These cases need the host OpenCode copy, or a native runner, and must not be
fixed by mounting more of the host or by using host networking:

| Case | Why the pod cannot do it |
| --- | --- |
| macOS Metal, MoltenVK or kosmickrisp rendering | the podman machine is a Linux VM and has no Metal |
| Hardware Vulkan through a host GPU driver | the agent gets `/dev/dri/renderD128` only when `VIGO_AGENT_GPU=1`, and the host driver may still be the only one that matches |
| A GUI on the host display, host audio, controllers or USB devices | those host devices and sockets are not in the pod |
| An MCP server that reaches a service on the host loopback, such as a profiler | pasta does not put the host loopback in the pod |
| A host kernel module, host-only driver or a binary built against a newer glibc | the pod has its own kernel and userspace, and the agent image has its own glibc |
| Host data outside the mounts below | only the project tree, the agent home, the private checkout and explicit `VIGO_AGENT_DATA` paths are visible |
| A credential held by the host Keychain, Secure Enclave or host TPM | the pod gets only the secrets the launcher mounts |

The host copy is a separate OpenCode with its own data. Use it for those cases and leave the pod for
builds, CPU-side tests, the Linux Vulkan path and work that fits the mounts above. The rule is chosen
by capability. No hostname, address or user name is needed to decide it.

#### macOS: the same pod

The podman machine is a Linux VM, so the image is the same Arch build. The Keychain cannot enter it,
so macOS uses podman secrets instead of systemd credentials:

```sh
podman secret create vigo-token - < /path/with/token
podman secret create vigo-opencode - < /path/with/password
podman secret create vigo-peer-password - < /path/with/peer-password
podman secret create vigo-ssh-key ~/.ssh/vigo_container_ed25519
# optional, for the agent's gh
podman secret create vigo-gh-token - < /path/with/gh-token
```

The settings read the mounted files on both platforms, never `printenv`: `cat /run/secrets/vigo-token`,
`cat /run/secrets/opencode-password` and `cat /run/secrets/peer-password`.

The agent gets the password, the key and the optional gh token; the daemon gets the token, the
password and the key. OpenCode runs in the agent container, not on the host. The one key the agent
uses for SSH and commit signing is `/run/secrets/vigo-ssh-key`; the image's `/etc/gitconfig` selects
it. Set `VIGO_BIND_ADDRESS` (or `VIGO_PEER_BIND`) to the address the other device reaches through the
podman machine; the default `127.0.0.1` publishes only to the Mac.

`deploy/container/install.sh` installs the launchd job. The launcher starts the podman machine when
`podman info` does not answer, then says what to run if it still does not. Booting the job out stops
the pod and the machine it started.

The podman machine is Linux and has no Metal. MoltenVK or kosmickrisp rendering tests therefore run
through the host OpenCode copy on the Mac, not through the Linux agent; the agent covers builds,
CPU-side tests and the Linux Vulkan path.

An upgrade is the one stop that keeps the work. A question whose prompt already reached a device is
written down just before the restart, with the session and the marks that name its turn. The new
process watches that turn, then posts the answer in the channel, because the interaction ended with the
old process. The restart ends only the daemon: the launcher keeps the agent container when its image
has not changed, so the device keeps working on the turn across the upgrade. A question that only
waited in a line is not carried: it never ran, and the state file keeps no message text. A crash, a
stop or a restart nobody asked for carries nothing either, so the bot never answers a channel with
work from hours ago.

A turn can still be lost: the agent may be restarted on its own, or crash. Vigo does not wait for an
idle the device will never write. The device's own list of running sessions says nothing runs there,
so the run ends, posts the part the device wrote, says the turn was cut, and lets the channel go. The
same list keeps a busy stamp left behind by a lost turn from refusing every later question in that
channel.


## The Discord application, in words

What the Developer Portal asks for, if it helps:

- **Name**: `Vigo`. **Bot username**: `vigo`
- **About me**: *Vigo is my OpenCode agent on my own machines. I ask from a
  channel, the work runs on my Mac or PC, and every tool that needs a
  permission waits for my click. Nothing is stored: no message text, no logs of
  what was said.*
- **Tags**: `AI`, `Productivity`, `Developer Tools`, `Utilities`
- **Icon**: `art/vigo-icon-41.png` (512×512), Wimsy's blossom, petals shifted
  to DeepSeek blue, **41** on the wing where the source had 55 (Opus 5.5), and
  **VI** on the hat tag where it had AO. Rebuild it with `./art/make-icon.sh`;
  `art/recolor.py` and `art/badge.swift` are what it runs, and
  `art/aogo-source.png` is the source. Wimsy is the ScarletNX mascot, drawn by
  Liz; the version here is Aogo's (https://github.com/Aogo-AO55). Both are
  GPL-3.0-or-later, so the icon is too and is covered by the same licence as
  the rest of the repository. See `THIRD-PARTY.md`.

## The commands

| Command | Who | What happens |
| --- | --- | --- |
| `/vigo ask prompt:<text> [file] [device] [hidden] [new]` | approved users | asks the device you last worked on; the question is acknowledged at once and the answer follows as a new message. The owner's answer is public unless `hidden:true`, in which case only they see the question and answer. An approved person's ask is always private and is one question in a session of their own: no goal, no device choice. Any file attached with it, or on a reply to an answer that carries one, is fetched on that device and read with the tool its format needs: a log or source file with `grep`, an image or PDF with the read tool, an archive or binary with `file` and its own tool. Nothing has to be pasted |
| `/vigo goal objective:<text> [device] [hidden]` | owner | gives the device an objective to keep working toward, with no question, in a session of its own; `pause`, `resume` and `clear` control the goal it already has |
| `/vigo quick prompt:<text> [device] [hidden]` | owner | one answer, with no session and no tools; the message stays as thinking until the answer is ready, then becomes it, with no line before, and the device is told the same two-paragraph limit as a question. An approved person is refused: a one-off leaves no session, so the share of the plan cannot be counted for it |
| `/vigo status` | owner | the channel's session, its device, tokens and cost, and the subagents at work under it; every question that waits, and the last three that ran |
| `/vigo queue [public]` | owner | every channel that runs or waits, on this daemon and the other ones, every task in each line, and how many agents work on each task. The task at work is item 0 and the tasks behind it are 1 and up, and a run that no daemon holds any more is still read from the device. `public:true` shows the channel a redacted copy |
| `/vigo devices` | owner | what is up, and which one a question would go to |
| `/vigo stop` | owner | interrupts the channel's run, and drops the questions that wait behind it |
| `/vigo upgrade [device]` | owner | pulls and restarts a daemon that runs a different commit; empty does every one of them, and the owner gets a DM naming each device and the commit it moved from and to. A turn already running on that daemon carries over the restart, and its answer still arrives |
| `/vigo allow user:<who> [for:<2h｜1d｜7d｜30d｜180d｜365d｜perm>]` | owner | lets someone ask, for a time from 2 hours to a year or for good. One day when unnamed. The **Allow** buttons on a request card do the same |
| `/vigo deny user:<who>` | owner | takes that allow back |
| `/vigo allowed` | owner | who may ask, and until when |
| `/vigo help` | anyone | what the bot does. A stranger also gets the **Ask for access** button |

`/vigo status`, `devices`, `upgrade`, `allow`, `deny` and `allowed` are always
ephemeral: only the person who ran it sees the answer, and no option could show
it to a channel. `/vigo queue` is ephemeral too, unless the owner passes
`public:true`. `/vigo stop` takes `hidden` like the questions do.

The public queue is the one the channel reads: no channel is named, no device
is named, and a task that holds a path, an address, an id or a device name is
shown as `redacted task`. What stays is the shape of the work, which is each
line's state, how many tasks wait behind it, and how many agents work on it.

A line says how many **agents** work on its task: the task's own agent while it
runs, and every subagent under it. The device answers that from the sessions it
runs now, because a subagent is a session of its own under the task's session.
A task with nothing at work shows no count, and a device that does not answer
leaves the count out rather than showing a wrong one. `/vigo status` names the
subagents at work under the channel's session.

One question runs in a channel at a time. A question asked while one runs
waits its turn, and the message it came from says how many run before it. The
line holds ten. `/vigo stop` clears it. A **task** asks for neither: it always
goes through the line, and it can start while the device is still answering a
question, because a task runs in a session of its own. A question is what the
device's busy conversation would make steer, so a question that arrives while
another daemon's run holds that session is refused with that reason instead.
`/vigo status` names every task in this channel's line and the last three that ran, and `/vigo queue` names every
channel that runs or waits, on this daemon and on the others, so a task waiting
somewhere else is found without knowing where to look. A daemon that does not
answer its peer port is said to be silent, never left out. A line lives in the
memory of the daemon that took its questions, so a restart drops what waited,
and those questions have to be asked again.

A queue lists the task at work first, as item 0, and the tasks behind it from
1. The item at work does not need a line in a daemon's memory: the device is
asked which of its sessions still runs, and a session made for a channel is
that channel's run. So a run that outlived its daemon, and a goal that works
with no question, both keep their place. A goal names the work in item 0. A run
whose daemon has gone keeps the place without its text, because the question
went with that daemon.

The deferred message becomes "Got it, " plus one short phrase the device wrote
for the request — "fixing the failing CI job", "explaining why CI is red" — so
the channel learns what was understood, not the question read back. The phrasing
follows the work, and a device that will not write one leaves the line saying
only that the question arrived. The answer is a new message when the agent is done, so Discord notifies
you. A request is acknowledged as "working on" and a question as "looking
into", so the channel can tell what was taken on. The device is told to keep
the answer to two short paragraphs and leave the rest out, and the code does not
cut it shorter than Discord requires: an answer that fits a message arrives as
written, however many paragraphs it has, and a longer one arrives in the
messages that carry it, in order, with each code fence closed. Only a runaway
answer stops at four messages, and its last one says so, naming the session
where the rest is when there is one.
A run that passes `timeout` adds a note to the end of the line the question
already shows, the queued line or the got-it line, so the question keeps one
message, and the run keeps watching. No second message arrives for it. The
line is edited by the message's own id, which the daemon keeps when it writes
the line, so the note still lands after Discord's fifteen minutes on the
interaction have run out: a question that waited in the queue can pass them
before its run even starts. When Discord takes no edit at all, because the
line is ephemeral or the app is not in that channel, the note goes out alone
rather than not at all. The answer still arrives, and the channel stays busy
until it does. No progress line is posted. If the device is not working on it
by then — nothing on the session is newer than the question — the run ends and
says so, because a device that took the question and will not run it would hold
the channel for ever with every later question behind it.

When a turn fails, the answer names what the person can act on: the model is
rate limited, the account has no quota left, the session is out of context, or
the provider refused the key. The device's own words stay in the log, never in
the channel. A turn that produced nothing at all — the machine slept under it,
or the session never ran — says that, instead of reading like an empty answer.

An ask may hand parts of its work to subagents — **up to three at a time, only
where it helps**: fewer when fewer are enough, none when none is. Both a
question and a goal say so, and the rule carries what a subagent costs: it
starts with fresh context, it cannot see the conversation, and it cannot start
another subagent. So the work gives each one the exact files, the exact result
and the constraints, runs independent parts in the background, and reads every
report before it trusts it. The **plan** is your OpenCode subscription: one
account, and both machines draw on it, so its concurrency and its rate limits
are shared. Vigo refuses a question that arrives when ten agents already run on
the devices it drives, because a burst past the plan's limits comes back as
failed turns instead of answers. The count is read when the question arrives,
so the subagents a running turn starts after that are not in it: the turn is
told to keep its own fan-out to three. An "agent" there is a session
whose turn is running: a conversation answering a question, a task's own
session, or a session OpenCode made for a subagent. A question that would be
the eleventh is refused, saying how many run, and `/vigo status` names the
count when more than one agent runs.

In a direct message, reply to a message the bot posted and the reply is the
next question in the same session. Its answer is posted in the DM as a message.
A plain message is not a question. A reply to somebody else's message is
ignored, and somebody who may not ask is told so.

An ask that is a task gives the device a **goal**, and it works toward that
until it is met instead of answering once. No option says which it is: the
wording decides, and a request ("can you fix the tests") is a task while a
question ("why is CI red?") is not. When the wording is read wrong, say it with
`/vigo goal objective:…`, which sets a goal without asking anything.
`/vigo goal pause`, `resume` and `clear` control the one the channel has.
`/vigo status` names the goal and the state the device keeps for it, including
a goal the device paused because it restarted under it. The answer to a goal
says where it stands when the turn ends: **the goal is met**, still active with
its round, or paused with the device's reason. A goal keeps working across
turns, so that line is the one that says it is finished.

A task does not run in the conversation's session. It gets a session of its
own, named for the channel and the first words of the task, and it runs under
the device's coding agent (`build` in OpenCode), a primary agent rather than a
subagent: OpenCode runs a subagent only in a child session through its
`subagent` tool, and a subagent cannot start another subagent, so a subagent
agent could not follow the rule above.
A goal goes the same way, whether the wording of an ask made it or
`/vigo goal objective:…` set it: it runs in a session of its own under that
coding agent, so the conversation keeps answering questions while the goal
works. A channel that already holds a goal keeps its session, so a second
objective steers the work that runs instead of starting another.
OpenCode lists that session, so the person can open it there and carry on with
the work directly, and the answer names it once. The channel remembers the
session the work is in, so a follow-up and `/vigo status` reach it. A question
keeps the conversation's session and no worker.


Two daemons answer every channel, one on each machine, and each keeps its own
queue. Before a question is sent, its run claims the channel on the device
itself: the session's metadata holds `vigoRun`, which names the daemon and a
heartbeat that the run bumps every few seconds while it works. A run that finds
a claim held by the other daemon waits for it and then asks. A claim whose
heartbeat has not moved for twenty seconds is dead and is taken over, so a crash
or a restart costs one turn and not the channel. `/vigo goal` reads the same
claim on the session the channel already has, and refuses while somebody else
holds it.

A run is not always the daemon's that answers `/vigo status`: both daemons get
every command, Discord takes the first acknowledgement, and either may restart
under a run. So `status` asks the device's own session, which is the record
they share, and reports the device working when it is, even with no run in
this process. A question asked then is refused with that reason instead of
joining the agent's turn; `new:true` still starts a fresh session.

The question carries one line the device would not otherwise know: who asked
and who reads the answer. A public answer is read by everyone in the channel,
and the line says so and asks the agent to keep credentials, keys and other
people's private details out of it. An answer only the asker sees says nothing
more, because the person asking may see what they asked about.

A public answer is also checked before it goes out: a value shaped like a key,
a token or a private key block is replaced with `[redacted: …]`, and the answer
says one was removed. `hidden:true` shows the answer whole. Paths, e-mails and
hostnames are left alone, because an answer about a machine has to name them.

The allowlist is kept in a message the bot owns: in `stateChannel` when the
file names one, otherwise in the bot's DM with you. It is read before every
decision, so an allow made from the Mac holds on the PC at once, and it
survives restarts. The message holds the ids and the display names of the
people you allowed, so `stateChannel` must be a private channel the bot and you
can read, and nothing else. If that message cannot be read, because a channel
the bot cannot see, or a message edited into something it cannot parse, changes
stop with an error naming the problem, and nothing is overwritten.

The bot finds the list by its id, and reads back through the channel when that
id is gone, so a second device or a lost state file still sees it. It reads to
the channel's first message before it calls the list missing, and a DM is read
further than a state channel, because a card and an answer land there for every
turn. A list it could not reach that way is named, not replaced: a new message
would take over and the entries of the old one would stop applying. Prefer
`stateChannel`: a DM works only when you and the bot share a server that lets
you receive DMs from its members.

An allow used to mean "may use the bot". It now means "may ask the owner's
device a question". The device still holds your files, your sessions and your
keys, so the limits are the point: an approved person's answer is ephemeral, so
nothing about the machine lands in the channel; their content is their own
session, so your conversation is not read into it; and their tools are one use
at a time, approved by you. Do not read the allow as a grant of control, and do
not leave `users` in the settings file as a quieter way to grant the same thing
without the time limit.

### The share of the plan an allow carries

The plan is your subscription, so an allow spends your plan and not someone
else's. Two records bound that spend, and both come from the devices:

- The plan's own meters, `/api/experimental/usage/limits`, report the percent
  of the rolling, weekly and monthly windows that is spent. This is the rate
  limit itself, so no size of a limit has to be guessed.
- The tokens of a window come from `/api/experimental/session/stats` for the
  whole device. The guest part comes from the messages of the sessions that
  belong to an approved person. Those sessions carry `vigoGuest` with the
  asker's id in their metadata. A session made before that mark is still found,
  because a guest's channel key carries the asker after a colon, and a subagent
  counts as the session that delegated it.

`percent * guest tokens / all tokens` is the percent of the plan that approved
people spent. Their questions are refused when that reaches `share` of the
plan, a third by default. A guest who is the only one using the plan therefore
stops at a third of it. A guest who arrives after you spent two thirds gets
what is left of that third.

A test reproduces the rule on a scripted month of a 30M-token plan: 30
questions of 500k tokens each. Without the cap the guests spend 15M, half the
plan. With the cap they stop at 10M, a third of it. The plan's own report on
the machine that set the defaults read 8.3M tokens in one day, input, output
and reasoning, so the default daily cap of 2M for all guests together is under
a third of that. The per-person cap of 1M keeps one guest from taking the
whole of it before the others ask. The hourly caps stop a burst before the
plan's meters can show it.

The owner is never checked against the share: every question they ask runs.
`/vigo status` shows what approved people spent, as a line naming today's and
this hour's tokens and the percent of the plan they hold. A refusal tells the
person which limit it was, and never names a device, a path or an id.

The check needs a plan that reports its meters, which OpenCode Go does. A
device whose report cannot be read, or a total that does not hold the guest
tokens it lists, refuses an approved person's question rather than guess, and
the log says why. `share: 0` pauses approved people completely.

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
drafts, and a draft cannot merge anyway. Linux has no check to require, because
it runs in a codespace that nobody starts for you, so the rule below covers the
two platforms a runner answers and the codespace run is a promise, not a gate.

```sh
gh api -X PUT repos/thegreatGreenstar/vigo/branches/main/protection --input - <<'JSON'
{"required_status_checks": {"strict": true, "contexts": ["lint", "no secrets in the tree",
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

## The status it shows

The bot's Discord status says what it is doing instead of staying green:

- **Online** (green) while any server is at work on a turn, while it answers a
  question or a quick command, and for five minutes after it last answered
  anything.
- **Idle** when nothing was answered in those five minutes, no server is at
  work, and a server can take a question now.
- **Do not disturb** (red) when every server that answers is at work on a turn
  and nothing was answered recently, so a question would wait behind that work.
  A turn at work is what the server is doing, not a session list that is full.
  A daemon that no server answers shows this too, because nothing can be asked
  there.

The bot never shows offline while it runs. A daemon that cannot reach its own
OpenCode server does not know that another daemon cannot answer, so one
machine's view must not make the account look gone. With no gateway connection
there is no status to show, and Discord draws the bot offline by itself.

Every device runs a daemon, and each one sets the status of the same bot
account, so the last update to arrive is the one Discord keeps. Each daemon
therefore shows the one status that the facts on any daemon support, in this
order: green, then idle, then do not disturb. Two daemons that see the same
servers then agree whatever order their updates arrive in, and a machine at
work cannot turn the account red while another one is free. A daemon answers
its own status on its peer port and reads the others' from theirs (see
`peerPort`), so the merge needs no state in Discord, and a daemon that does not
answer is left out instead of counted as offline.

Only a change is sent, and the gateway sends the current status again after a
reconnect.

## Permissions

When the agent wants to run a command or write a file, OpenCode raises a
permission request. `vigo` sends it to your DMs:

> **Permission needed** on `macbook`
> The agent asks to run `bash`:
> > `git status`
> **[Allow once] [Always allow] [Deny]**

Clicks from anyone but the owner are refused and change nothing. The run
waits until you answer, and the channel message says approval is needed.
`Always allow` saves the rule in OpenCode for that project. A permission that
came from an approved person's question has **Allow once** and **Deny** only:
their question may earn one use of a tool, never a standing rule.

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
  each server it is in. A user install needs no server, so leave Guild Install
  off unless you want that second door.
- A public app lets anyone install it and ask you for access. Only your click
  allows one, the allow runs out or is denied as any other, and a request on
  its own reaches no device. Keep `requests` off if you do not want the card.
- The bot stores nothing about what was said: no message text in the log, no
  database. Sessions live in OpenCode; the Discord channel holds the answers.
- An allow is a read grant. An approved person's question runs the agent under
  your account, so it can read what that agent can read on that device. The
  share cap limits the plan's spend, not what the agent may see. Allow only
  people you would trust with the machine.
- A group DM is not a private conversation. Its answers are scrubbed the way a
  channel's are, and the device is told that everyone in it reads the answer.

See [SECURITY.md](SECURITY.md) for the threat model, the audit behind the
current code, and the limits that remain by design.

### Before this repository is public

The source can be public. The history goes with it, and so does every token that
was ever written into it.

- **A token in an old commit still works.** The `secrets` job runs on the tree
  and on every commit. Run the same check before publishing, and rotate a token
  that appears even once. Redacting a log is not rotation.
- **A public app is a choice, not a leak.** Leave **Public Bot** off and
  **Install Link** at **None** for a bot that is only yours. Then nobody else
  can install it. Turn it on for the public user install above: strangers can
  then install the app and ask you for access, and nothing runs until you
  click. The token is still yours alone. A second person who wants their own
  agent makes their own application and their own token. Sharing this app
  shares the machines behind it, which is what an allow already says.
- **Never track the settings file.** `vigo.json` holds the token when it is not
  read from the Keychain or a TPM. `.gitignore` covers it and CI fails on it.
- **Pin what runs.** Every action in `.github/workflows/tests.yml` is pinned to a
  commit SHA, and `tests/test_ci_pins.py` fails when one is a tag.
- **The history carries the people who wrote it.** Author lines and commit
  messages can name a machine or a network. Read them before publishing, and
  rewrite them if that matters to you.
- **Every ref goes public, not only `main`.** Delete merged branches, and read
  each pull request body: a machine name or an address there is visible too. A
  rewrite cleans a branch, but GitHub may keep a pull request's old commits
  reachable. A fresh repository is the only way to remove them completely, and
  that is a larger cost than the risk usually deserves.
- **The licence is GPL-3.0-or-later.** See `LICENSE`. The icon is Wimsy,
  drawn by Liz in Aogo's variant, and is covered by the same licence; see
  `THIRD-PARTY.md`.

### Keeping the secrets off the disk

Each secret, `token` and every device's `password`, may be given three ways,
one of them only:

| Key | What it is |
| --- | --- |
| `token` / `password` | the value, in the file |
| `tokenFile` / `passwordFile` | a file holding it; mode 0600 or stricter, relative paths taken from the settings file's folder |
| `tokenCommand` / `passwordCommand` | a command whose output is the secret; run once at start |
| `peerPassword` / `peerPasswordFile` / `peerPasswordCommand` | the same three forms for the peer API; every device uses the same value, and it is never mounted in the agent |

Each takes one form only: naming two of them stops the bot.

On the Mac, the login Keychain is the natural home, and macOS keeps that
item's key Encrypted with the Secure Enclave-wrapped volume key:

```sh
# -w last asks for the value, so it is never in the shell history or argv
security add-generic-password -a "$USER" -s vigo-token -U -w
security add-generic-password -a "$USER" -s vigo-macbook -U -w
# for the second, paste a value from: openssl rand -hex 32
```

The value is typed at the prompt, not on the command line. A `-w 'the value'`
form puts the secret in the shell history and in `ps`, which is why it is not
written that way here.

then the settings file holds no secret at all:

```jsonc
"tokenCommand": "security find-generic-password -s vigo-token -w",
"devices": [
  { "name": "macbook", "url": "http://vigo-opencode:4096",
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

On Linux the same idea is `systemd-creds` sealed to the TPM. The container unit unseals both values and mounts them
read-only at `/run/secrets`:

```sh
# this machine's opencode password, and the bot token, each sealed to it
printf '%s' "$(openssl rand -hex 32)" | systemd-creds encrypt --user \
    --name=opencode-password - ~/.config/vigo/opencode.cred
printf '%s' 'the bot token' | systemd-creds encrypt --user \
    --name=vigo-token - ~/.config/vigo/vigo-token.cred

./install.sh
./deploy/vigo-identity.sh
./deploy/build-container.sh
systemctl --user daemon-reload
systemctl --user enable --now vigo-ssh-agent vigo-container
```

The settings name the mounted files, never a `systemd-creds` command:

```jsonc
"tokenCommand": "cat /run/secrets/vigo-token",
"peerPasswordCommand": "cat /run/secrets/peer-password",
"devices": [
  { "name": "pc", "url": "http://vigo-opencode:4096",
    "directory": "/home/you/Projects",
    "passwordCommand": "cat /run/secrets/opencode-password" }
]
```

The container never needs `/dev/tpm*` or `systemd-creds`: it receives the values through tmpfs files that exist only
while the service runs.

Vigo's `gh` token is optional and works the same way. Create a token on the `VIGO-VI` account with `repo` and
`read:org`, then seal it into the per-user credential store the unit searches:

```sh
install -d -m 700 ~/.config/credstore.encrypted
bash -c 'umask 077; read -rsp "VIGO-VI token: " tok; echo; printf "%s" "$tok" | systemd-creds encrypt --user --name=gh-token - "$HOME/.config/credstore.encrypted/gh-token"; unset tok'
systemctl --user restart vigo-container
```

`LoadCredentialEncrypted=gh-token` has no path, so a missing credential is not an error: `gh` then runs
unauthenticated. The entrypoint exports the value as `GH_TOKEN`; it never appears in a command line.

A `.cred` file decrypts only on the machine (and user, with `--user`) that sealed it, copying it to the other device
gains nothing, which is the point.

### The two devices cannot hold the same file

Every daemon needs the password of *every* device, because it reaches each one
over HTTP. So each machine can seal **its own** password and must hold the
**peer's** password some other way, plainly in its 0600 settings file, or in a
0600 file. That asymmetry is unavoidable without a key exchange OpenCode does
not offer:

| On the Mac | On the PC |
| --- | --- |
| `macbook.passwordCommand` → Keychain | `macbook.password` → the Mac's password, in the file |
| `pc.password` → the PC's password, in the file | `pc.passwordCommand` → `cat /run/secrets/opencode-password` |

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
| `~/.local/state/vigo/state.json` | channel → the session it has on each device, permission card tickets, the allowlist message's id, the people who asked for access and when, and the turn an upgrade's restart is about to cut off (ids and times only). Folder 0700, file 0600 |
| `~/.local/share/vigo-agent` | the agent's own home: OpenCode data and settings, Vigo's public keys and git config |
| `~/.local/state/vigo` | the daemon's state, mounted only in the daemon |
| `${XDG_RUNTIME_DIR}/vigo/ssh-agent.sock` | the socket that asks this machine's TPM to sign as Vigo; gone when the agent stops |
| OpenCode sessions | the channel a session serves, and for an approved person's session the asker's id, in the session's metadata on the device |
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
tests/ci-local.sh                               # what CI runs, as far as this machine can
tests/ci-codespace.sh --machine standardLinux32gb   # the Linux half, in a Codespace
tests/in-containers.sh                          # Linux 3.12-3.14, in podman
tests/in-codespace.sh                           # Linux 3.12-3.14, inside a codespace
```

`tests/ci-local.sh` runs the CI steps here: the suite on each host Python 3.12 to 3.14 that is installed, ruff
0.17.4 in a container, the secrets check, and the Linux matrix in podman. It names what it cannot answer, which is
Windows and any Python that is not installed. It takes about a minute and a half on this Mac. It refuses to start on a
laptop that is not on AC power with its battery at 80% or more, and a desktop always runs it: the podman VM heats a
laptop, and CI is free now that the repository is public.

`tests/ci-codespace.sh` is the Linux half of CI, on a Linux machine rather than this one. A run with no terminal
there passes `--machine`, because `gh codespace create` would ask for one: this repository's plan offers
`basicLinux32gb` and `standardLinux32gb`. The script creates a Codespace,
copies this working tree into it (uncommitted changes included, so nothing has to be pushed first), runs
`tests/in-codespace.sh` there, prints the result, and deletes the codespace however the run ends. `--keep` stops it
instead, so the next run is quick; a stopped codespace still holds storage against the quota, so delete it when done.
The copy carries files and no `.git`, so the secrets check runs here, on the same bytes, before a codespace is
made. The image is `.devcontainer/Dockerfile`: Ubuntu 24.04 with Python 3.12, 3.13, 3.14 and the ruff pin, built on the
first create and cached for the next. A prebuild enabled in the repository's Codespaces settings makes even that one
fast. The Codespace's own checkout only supplies `.devcontainer`, so the run uses the branch when it exists on
`origin` and `main` otherwise.

Codespaces is Linux only, so it cannot answer macOS or Windows, and those two stay in the workflow. The gh token
needs the `codespace` scope once: `gh auth refresh -h github.com -s codespace`. The default machine is 2 cores, which
is plenty for a suite of a few seconds; Pro includes 180 core-hours and 20 GB of storage a month, and a code change
of this size costs a few minutes of that.

`tests/in-containers.sh` runs the suite in `python:3.12`, `3.13` and `3.14`
containers on the `podman machine` Linux VM, so the Linux half of the CI matrix
is answered here first. It cannot answer Windows: `podman` on a Mac is always
Linux, and Windows behaviour has already differed once (a socket read that
`shutdown` does not wake there).

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
devices, the limits an allow carries (its own session, a private answer, no
goal, no device choice, one question in flight, no saved tool permission, a
deny that reaches a waiting question), an access request from a stranger through the owner's card, the command
surface and who may use it, the permission cards (approved, refused, a stranger's click), a full ask with
streamed deltas, the gateway's handshake, heartbeats and resume, and two
daemons answering one use exactly once.

CI runs the same suite on Python 3.12, 3.13 and 3.14, plus a lint and a check
that no token is in the tree. Linux runs in a Codespace, and the two platforms
a runner alone can answer run on GitHub. It does that in two stages, so a draft
answers the cross-platform question quickly:

| stage | what runs | why |
| --- | --- | --- |
| draft pull request | `quick`: one Python (3.14, the newest in the matrix) on macOS and Windows | a path, a socket or a clock that differs by platform shows up in one job per OS instead of three |
| ready for review, or a push to `main` | `test`: macOS and Windows × 3.12, 3.13, 3.14 | version-specific breakage, which one Python cannot show |
| before the pull request, or before the merge | `tests/ci-codespace.sh`: 3.12, 3.13 and 3.14 on Linux, ruff and the secrets check | the Linux half, on a machine that is not this one |

Lint and the secrets check are the same in both, because they are cheap and
platform-free. Marking a draft ready runs the full matrix; converting it back
to a draft runs the quick one again.

The Linux row is the one nobody runs for you: the workflow has no Linux job,
so a pull request is checked there only when someone runs the script. That is
the trade a codespace makes, and it costs one manual step.
## Troubleshooting

| What you see | What it means |
| --- | --- |
| `resuming` then `closed with 4003` in the log, and the bot stays quiet | Discord ended the session, and the daemon was resuming it again on every retry; it now drops the dead session and identifies on the next connection |
| `closed with 4003` before any `ready`, and the process exits | Discord refused the token at identify; the daemon stops rather than sending the same token again, so the supervisor's restart meets the same refusal until the token is right |
| `closed with 4004` | the token is not the bot's; reset it and copy it again |
| `closed with 4014` | the bot asked for an intent Discord does not allow it; the bot asks only for `DIRECT_MESSAGES` |
| `no device answers` | no `opencode serve` is reachable from this daemon: check the URL, the port, and the password |
| `Permission needed` never arrives | the owner and the bot do not share a server that allows DMs, so no card is sent; the run says approval is needed and the tool does not run |
| `/vigo upgrade` says `git refused` and the log names `usekeychain` | the daemon's `ssh` is Homebrew's, which has no `UseKeychain`; the bot's launchd job sets `GIT_SSH_COMMAND=/usr/bin/ssh`, and `./install.sh` writes that again |
| a question says `still running` | the run passed `timeout`, so the line that question already shows, queued or got-it, ends with the note; the run keeps watching, `/vigo status` shows it, `/vigo stop` ends it |
| the answer says `never started` | the device admitted the question and its session never began the turn: `opencode serve` there is stuck, so restart it and ask again |
| the answer says the turn was stopped before it finished | the device restarted or crashed while the turn ran, so it wrote no idle and no more of the answer. The part it wrote is above, and asking again runs the turn |
| the answer says the model is rate limited, or has no quota left | the plan is spent for now: wait, or `/vigo quick` for a one-off that starts no session, and the log holds the device's own words |
| an approved person says `/vigo quick` and is refused | that command is the owner's: a one-off leaves no session, so the guest share cannot be counted for it. An ask runs in a session of their own and is capped |
| an approved person's question says the share of the plan is used, or that its usage cannot be read | the plan's meters live in `/api/experimental/usage/limits`, which OpenCode Go answers: check the version there, and raise `guests.share` or the caps only on purpose |
| the answer says the device returned no text | the turn produced nothing: the machine slept under it, or the session never ran. Ask again; a run that crossed a sleep ends like this |
| a goal says `paused` | the device restarted while the goal was active; `/vigo goal resume` starts it again |
| a question says `Queued` | one run at a time per channel; it starts when the one before it ends, or `/vigo stop` clears it |
| `/vigo upgrade` ended a question that was queued | only a turn already on a device carries over the restart; a question that waited never started, so ask it again |
| a channel has no session | the first ask creates one; `new:true` starts a fresh one on purpose |
| `python3 -m vigo --invite` says `Public Bot off` | Discord only gives a shareable install link to a public app: turn it on at the Bot page the check prints, and set Install Link to Discord Provided Link on the Installation page |
| `--invite` says the commands are registered for a context the app does not offer | the `install` key names more than the portal enables: set `"install": "user"` for a user-install-only app, or turn the other context on |
| a stranger sees no **Ask for access** button | `"requests"` is false, or the daemon answering is older code. Set it true and restart the daemon |
| a command says the allowlist may be older than the walk | the list message is buried under more messages than the bot reads (10,000 in its DM with you, 1,000 in `stateChannel`), so a new one would take over from it. Set `stateChannel` to a small private channel the bot can read, and allow those people there again |
| a request reached the bot but no card arrived | the owner and the bot do not share a DM that accepts messages. Open a DM with the bot, or set `stateChannel` |
| `/vigo` does not appear for somebody who installed it | they have not finished Settings → Apps, or the app's Installation page does not offer User Install. Discord can take up to an hour to show a changed command |
| the log says the file's owner is not the application's owner | remove `owner` from the settings, or set it to the id the application belongs to; the daemon will not trust a typed id |
| the log says the token belongs to another application | `appID` and `token` are from different apps; copy the Application ID from the same app as the token |
| an owner command says `Only this bot's owner` for the person who made the app | the app belongs to another account, or an older daemon is answering. Check the owner with `python3 -m vigo --whoami` |

## Licence

Vigo is GPL-3.0-or-later. See `LICENSE`. The icon is Wimsy, drawn by Liz in
Aogo's variant, and is covered by the same licence; see `THIRD-PARTY.md`.
