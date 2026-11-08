# Security

Vigo holds a Discord bot token, an OpenCode password for every device it drives, and the ability to run an agent
on those devices. For a person who can reach it, that is a remote shell with a chat front end. This file says what
the code trusts, what publishing it changes, and what is a limit by design rather than a bug.

## What the code trusts

- **Discord.** The REST API and the gateway are reached over TLS, the gateway handshake must prove it read this
  client's key, and the gateway address must be Discord's. A frame has a size bound and a redirect is not followed,
  because both can carry the token to a host that answered for Discord.
- **The settings file.** The token and every password are read from it, from a file it names, or from a command it
  names. The file is mode 0600 or the bot refuses to start, and no secret is written to the tree.
- **OpenCode on each device.** The password is the real boundary. Whoever has it can run anything there. Vigo does
  not loosen OpenCode's permissions: a project whose `opencode.json` allows everything runs everything without a
  card. In the container setup the agent runs in its own rootless container with no capabilities, no new privileges
  and a read-only root, and the only host data it sees is the projects tree it was given. A tool call cannot read
  `~/.ssh`, the Keychain or the rest of the home directory, and it never holds the Discord token.
- **The owner's click.** A tool that OpenCode asks about goes to the owner's DM, and only the owner's user id can
  answer it. The card says when the question came from an approved person rather than the owner.
- **The daemon's own state.** Who may ask is read from the bot's own allowlist message, written only by the bot, in
  the state channel or the owner's DM. No prompt, answer, reaction or message text grants anything.

## What an allow means

An approved person asks a question that runs under the owner's account, in a session of its own. That is a **read
grant to everything the agent can read on that device**, not only to the project. In the container setup that is the
projects tree and the agent's own state, so another project's secrets are still readable even though the rest of the
machine is not. The share cap in `guests` limits what the plan spends, not what the agent may see. Allow only people
you would trust with those projects. The owner
still approves every tool that OpenCode asks about, but a read tool that the project allows asks nothing.

An approved person's question is answered privately: an ephemeral message in the channel, or a DM when the
interaction has expired. Their ask never becomes a goal, never chooses a device, never starts a fresh session, and
they may hold one question at a time. A tool they cause the agent to ask for gets **Allow once** and **Deny**, never
**Always allow**, because a saved rule outlives the question that asked for it.

## Known limits

- **The VM's own mounts are a second boundary.** A machine created with podman's defaults mounts all of `/Users`
  into the VM, so a container escape reaches the home even though the containers cannot. The setup in the README
  creates the machine with only the pod's paths, so the VM holds no SSH directory and no rest of the home. A machine
  created with the defaults needs that rebuild before the same sentence is true.
- **The agent's OpenCode credential lives in its container state.** It is a separate sign-in from the desktop app, so
  revoking one does not revoke the other. It sits in `~/.local/share/vigo-agent` on the host, readable by the user
  and by the daemon's container, which already holds the Discord token.
- **The OpenCode password crosses plain HTTP.** Bind `opencode serve` to loopback or a VPN, never the internet, and
  use one password for this and nothing else. The password is a shell.
- **The peer port runs code.** `POST /vigo/update` pulls the checkout and restarts the daemon. A password holder
  already has more than that, but keep `peerPort` on the tailnet or an SSH tunnel. It is off unless the settings
  name a port.
- **The peer port carries queue text.** It is authenticated and does not go to Discord, but a prompt can name a file.
- **A public answer is scrubbed by shape, not by meaning.** Keys, tokens, private key blocks and webhook URLs are
  replaced when the answer goes to a channel. A path, a hostname or a sentence about a secret is not. A hidden
  answer and a one-to-one DM are not scrubbed, because the asker may see what they asked about. A group DM is
  scrubbed, because it has more readers than the asker.
- **`/vigo upgrade` runs code from the remote.** It is `git pull --ff-only` followed by a restart. A user who can
  push to the branch the daemon tracks can run code there. That is the point of the command, and it is why the
  repository and its write keys need the same care as the machine.
- **A public repository exposes history.** A token removed in a later commit still works. The `secrets` job checks
  the tree and every commit, and fails on a Discord token shape. Rotate a token that was ever committed. Redacting
  a log is not rotation.

## The audit behind this file

The code was reviewed on 2026-11-07 in three attack waves before the repository was made public. The useful sources
were CVE-2026-25253 (an agent control panel that accepted a gateway URL and sent its token to it), CVE-2025-26604
(a bot framework that executed a user's module), GitHub's writing on DNS rebinding and localhost services, and
NVIDIA's writing on indirect prompt injection into coding agents. The findings below are fixed in the code and
covered by tests.

| Wave | Attack | Finding | Fix |
| --- | --- | --- | --- |
| 1 | Forge the allowlist in a shared state channel | A message that opened with the marker was read from any author, so a member could allow themselves | the bot's own user id is required before a message is read (`vigo/allowlist.py`) |
| 1 | Answer a request with a redirect | `urllib` followed it and carried `Authorization` to the new host | no redirect is followed, and an answer has a size bound (`vigo/httpc.py`) |
| 1 | Answer the gateway handshake without the key's proof | A 101 without `Sec-WebSocket-Accept` was accepted, then the identify frame carried the token | the header is required and the handshake head is bounded (`vigo/gateway.py`) |
| 1 | Point the REST gateway answer at another host | The URL was used as given | only `wss://` on Discord's hosts, or loopback for a local server (`gateway_url_ok`) |
| 2 | Make the agent echo a credential into the acknowledgement | The phrase was posted without the answer's check | the phrase is scrubbed and refused when it names a secret or a path |
| 2 | Echo a key in the bot's own token shape | The scrubber knew OpenAI, GitHub, AWS and Slack shapes, not Discord's | Discord tokens, webhooks, bearer tokens, Stripe and Google keys are redacted (`vigo/text.py`) |
| 2 | Use a group DM as a private conversation | The answer was not scrubbed and the prompt said only the asker reads it | the channel kind is read, a group DM is scrubbed and the prompt says so |
| 2 | Hide a path in a task title or a goal | The title and the goal's words were appended to a public answer | they appear only when the text names nothing of a person or a machine |
| 2 | Approve a tool for somebody else's question | The owner's card did not say whose work it was | the card and the question card say it came from an approved person |
| 2 | Get the owner to save a standing rule from a guest's question | The guest's card offered **Always allow** | a guest's card offers Allow once and Deny, and a forged always choice is refused |
| 2 | Steer the owner's conversation or plan with `/vigo goal` | An approved person could set, pause or clear the channel's goal, in the owner's session, with no share cap | `/vigo goal` is the owner's; a guest's ask never becomes a goal, runs in their own session and is capped |
| 2 | Leave access in force after it was taken back | The allowlist was read when the command arrived, not when a queued run started | the run reads it again and refuses when it has ended |
| 3 | Read a credential from a URL in a log line | The netloc kept a `user:password@` prefix | it is cut, and the settings refuse such a URL |
| 3 | Bury the allowlist under a flood in the state channel | The list was read from the newest 50 messages only, and a missing list wrote a new one | the walk pages back, and a new list is refused when the old one cannot be reached |
| 3 | Guess the peer password a byte at a time | The header comparison stopped at the first different byte | constant-time comparison, and a non-ASCII header is refused (`vigo/peer.py`) |
| 3 | Take over a release action | Actions were named by tag, and a tag can move | every action is pinned to a commit, and a test fails if one is not |
| 3 | Read a token from an earlier commit | Only the checked-out tree was scanned | CI scans the history too, and needs `fetch-depth: 0` |

The limits above are the remaining risk. The largest is the one the design accepts: an approved person runs the
agent as the owner, so an allow is trust in that person and not only in their questions.

## Reporting

Use GitHub's private vulnerability reporting on the repository. Do not open a public issue for a live token or a
password. If a token may have leaked, rotate it in the Discord Developer Portal and rotate that device's OpenCode
password. Redacting a log is not rotation.

The suite never touches the network, so a report can name the fake that reproduces the fault: `tests/fake_discord.py`,
`tests/fake_gateway.py` and `tests/fake_opencode.py`.
