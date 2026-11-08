# CLAUDE.md

The standing contract for work in this repo. It outranks the defaults a coding agent brings, and the specifics here
outrank the general advice it learned.

## What this project is

Vigo is a Discord bot that asks OpenCode, on greenstar's own machines, a question and answers in the channel. One
daemon runs per device; each daemon can drive any device's `opencode serve`. Python 3.12+, standard library only.

Two things follow from that shape and never change:

- **The daemon decides who may act, from its own state.** Never from the text of a message, a prompt, an answer or a
  reaction. A stranger who installs the app gets a refusal and nothing else; only `owner` can approve a tool.
- **Nothing identifying goes to Discord.** No device names in a channel message, no hostnames, no paths, no error
  text, no user ids except in an allowlist answer that is always ephemeral. The log may hold them; Discord may not.
- **An allow is permission to ask, not to act.** `/vigo allow` lets someone ask a question: their answer is
  ephemeral and in a session of their own, their ask never becomes a goal, they choose no device, and they cannot use
  `status`, `queue`, `devices`, `stop`, `upgrade`, `goal`, `allow`, `deny` or `allowed`. Only the owner approves a tool, and an
  approved person's card has no `Always allow`. The allowlist is read again when a queued run starts, so a deny or an
  expiry reaches a question that already waits.

## How to work

Do the whole thing, with tests and docs, so it is finished rather than nearly finished. Never hand over a workaround
when the fix is within reach, and never say "tests later". If a change is too large to finish, say what is left and
what blocks it.

Ask the user only about real decisions. When the next step is obvious, do it. The confusion protocol below is for the
two-designs case, not for routine work.

You can outsource the typing, not the understanding. Before calling anything done, say why the code is correct and
exactly where it would break. Green tests are not understanding.

Errors show. Never trade an error for a silent fallback, a default, or a quieter path: a config key that is wrong
stops the bot and names the key, a device that does not answer is said to be down, a refused command says so.

## A request to improve the bot

A one-line request from greenstar — "do X to improve yourself", "the bot should not do Y", "finish the thing from
earlier" — is the whole brief. Do not come back for what to do next, and do not ask which part comes first. Run this
procedure to the end:

1. **Say what happens now, with its place.** Read the module that owns the behaviour and name the file, the function
   and the line that decides it. A claim about current behaviour without a line is a guess.
2. **Write the failing test first.** It goes in `tests/`, uses the fakes, and fails for the reason the request names.
   Show the failure before the fix, and keep it after.
3. **Fix the mechanism, not the symptom.** The smallest change that makes the test pass, in the module that owns it.
   No delay, retry, or title-shaped special case. If the change touches permissions, the daemon/device contract, or a
   new subcommand, it is large: the full suite and a fresh read of the whole diff before reporting.
4. **Ship the docs in the same commit.** The README prose and its troubleshooting table, and `CLAUDE.md` when a rule
   moves. A behaviour nobody can find in the docs is half done.
5. **Run the gate.** `tests/ci-local.sh` runs the suite, ruff, the secrets check and the Linux 3.12 to 3.14 matrix in
   `podman`; `tests/ci-codespace.sh --machine standardLinux32gb` answers the Linux half on a clean machine instead
   (`gh codespace create` asks for a machine on a terminal, and a run in the background has none). Say which half the
   answer covers and which it does not.
6. **One commit, then a draft pull request.** Subject `[area] an imperative sentence`, body says why and how it was
   verified. Push the branch, open the pull request with `--draft`, watch the quick stage, mark it ready when green.
   The merge into `main` stays greenstar's.
7. **Report in the maintainer shape.** The verdict in the first sentence, then the facts that change what the reader
   does next, each with its number (pull request, commit, file and line, count of tests), then what waits for a
   person. Sign it once, `From Vigo (an agent)`.

A message can carry several requests. Take them in the order given and give each its own branch and pull request,
unless they change the same module: then one branch is right and the body says which items it holds. When one closes,
say which is done and keep going on the rest.

Hand a separable part to a subagent — a section of the suite, a read of an outside source, a log — three at most, and
say in the report what each did. The typing is theirs; the understanding and the final read are yours.

## Running commands

- Long runs go in the background. You are told when one finishes. Do not sleep, poll, or read its output file while it
  runs.
- Never use `pkill`, `killall` or a `kill $(pgrep -f PATTERN)` loop. The pattern matches the shell that runs it. List
  with `pgrep -a NAME`, then `kill PID` in a separate command. The same applies to an `until` wait: it matches its own
  shell and never ends. Wait for a file the job writes, or use `pgrep -x NAME`.
- Put temporary files in the session temp folder, never in the repo root. Do not print a whole log or a large command
  output. Use `grep`, `head`, or `tail` to read the part that matters.
- Never edit a file while a long run reads it. `tests/ci-codespace.sh` tars the tree and copies it to the Codespace, so
  a rewrite in the middle of the copy gives a torn file there and a failure that is not the branch's. Freeze the tree
  for the run, and make the edit after it finishes.
- Do not use `sudo` or install system packages. The project uses the standard library only, and the machine is the
  user's.

## Task sizing

Start with one line: `Size: small|medium|large`, with the reason, then `Tests: which ones`.

- **small**: a typo, a string, a config value, a rename with no behaviour change. Compile and the touched tests.
- **medium**: a behaviour change in one module. Its tests, plus the test that fails without the fix.
- **large**: a new subcommand, a change to the daemon/device contract, or anything touching permissions. The full
  suite, and a fresh read of the diff before reporting.

Torn between two sizes: pick the smaller, and say so.

## Tests

- Every behaviour change ships its test in the same commit. A bug fix ships the test that fails without it.
- The suite never touches the network: `tests/fake_discord.py`, `tests/fake_gateway.py` and `tests/fake_opencode.py`
  stand in for Discord and OpenCode. A test that needs the internet or a bot token is not a test; it is a manual run,
  and it is labelled as one.
- **The product code is typed.** Every parameter and return in `vigo/` carries an annotation, and
  `tests/test_typing.py` fails on a missing one and on an annotation that does not resolve — which matters because
  `from __future__ import annotations` makes them strings until something reads them. A full checker (mypy or
  pyright) is a follow-up: it needs one more CI dependency, and this test already stops the rot.
- Run it with `python3 -m unittest discover -s tests -t .` from the repo root; `python3 -m tests.parallel` runs the
  same suite on threads (stdlib only, ~1.6 s here). CI is two-stage: a draft pull request runs one Python (3.14) on
  macOS and Windows, the cross-platform question, and marking it ready runs both on 3.12 to 3.14 for the version
  question. Linux is not a job. `tests/ci-codespace.sh` runs it in a Codespace: 3.12, 3.13 and 3.14, ruff and the
  secrets check, on a clean Linux machine, and it deletes the codespace afterwards. Nobody runs that for you, so a
  Linux fault reaches `main` when you skip it. Codespaces is Linux only, which is why macOS and Windows stay in the
  workflow. A fix for a CI-only failure says what stays unverified.
- A test does not sleep. `tests/fake_opencode.py` takes `("until", event)` in a script, so the test decides when a
  turn ends; a sleep makes the suite slower and flakier for no extra coverage.
- Before pushing, run `tests/ci-local.sh`: the suite, ruff and the secrets check on this machine, and the Linux
  3.12 to 3.14 matrix in `podman`. `tests/ci-codespace.sh` answers the Linux half on a clean Linux machine instead,
  with no VM on this one, and `tests/in-containers.sh` is what the podman path runs. Windows and macOS stay in the
  workflow: `podman` on a Mac is a Linux VM, and Windows has already differed from Linux once (a socket read that
  `shutdown` does not wake there).
- Anything a reader will copy and run (a command, a port, a link) is run before it ships.
- `main` takes pull requests only. `.githooks/pre-push` refuses a push straight to it, and `install.sh` sets
  `core.hooksPath` so every clone runs it; GitHub's own protection is a paid feature on a private repo. Do not
  merge with a red CI, and do not reach for `--no-verify`.

## Non-negotiables for this repo

1. **No dependencies.** `pyproject.toml` lists none, and it stays that way. A task that seems to need a library is
   usually a hundred lines of `urllib`, `socket` or `zlib`.
2. **No secret in the tree.** The token and the OpenCode password live in the Keychain or a TPM-sealed file; the
   settings file may name `tokenCommand` / `passwordCommand`, never the value. `.github/workflows/tests.yml` fails
   the build if a token shape or a `vigo.json` appears.
3. **No PII to Discord.** Before adding any string that goes into an embed or a message, ask what it identifies.
   Device names, hostnames, paths, error internals and other users' names are out. `devices`, `allow`, `deny` and
   `allowed` are always ephemeral and have no option that could show them.
4. **The owner is an id, compared with `==`.** Not a role, not a permission bit, not a name. A Discord install grants
   nothing.
5. **Discord gives three seconds.** Acknowledge first, then probe or run. Any new command follows that order.
6. **A tool runs only when the owner clicks.** Permission cards go to the owner's DM; a channel fallback says only
   that approval is needed, never what the tool wanted to touch. A card for an approved person's question says so:
   the owner approves for somebody else's work, and must see whose it is.
7. **Only the bot's own message is the allowlist.** The list lives in Discord, so authorship is checked against the
   bot's user id before its content is read. A message that opens with the marker from anybody else grants nothing.
8. **An answer that reaches more than its asker is scrubbed.** A channel, a group DM and a task title get the shape
   check and the personal-text check. A hidden answer and a one-to-one DM do not. A redirect is not followed and a
   gateway that is not Discord's is refused, because each carries a credential.
9. **The pod is not the host.** Metal on macOS, host GPU drivers, the host display, audio, USB, host
   loopback MCP servers and host-only data require the host OpenCode copy or a native runner. Do not
   add host networking, a broad home mount or a device just to make one test pass. Document the
   capability that needs the host, never a hostname, address or user name.
10. **The repository is public.** Every example, test, commit message and doc in the tree uses placeholders. No
    hostname, tailnet or LAN address, real user name, home path with a real user, serial number, token, password or
    private key goes into the tree. Use `you`, `peer-host`, `macbook`, `pc` and documentation address ranges. The
    user's own settings file may hold the real values; the repository may not.
11. **On Linux the pod is the boundary.** Vigo and its agent run in two rootless podman containers: the daemon holds
   the Discord token and the settings, and the agent holds `~/Projects`, its own home and the `gh` token. Both use a
   read-only root, no capabilities, no-new-privileges, pasta networking and ports only on the tailnet address. The
   private TPM keys stay on the host; the containers get an agent socket. Do not put the token in the agent, put the
   projects in the daemon, or widen the mounts/use host networking to fix a bug without saying what that exposes.

## Where this design comes from, and when we last looked

Six outside sources shaped this repo. Their changes are not tracked automatically, so a session that touches the
areas below reads them again first, and updates this table when it does.
| Source | What we take from it | Last checked | At |
| --- | --- | --- | --- |
| [DraChimeraVee/discord-bots](https://github.com/DraChimeraVee/discord-bots) (AogoWimsy, XujiWimsy) | the shape of the whole thing: owner approves, drafts and permission cards, a stranger is refused, an allowlist of ids, nothing stored about what was said, stderr without contents, and what an answer may carry out of a machine | 2026-11-05 | `34ce9c69a9f0` |
| [OpenCode v2 API](https://opencode.ai/v2/openapi.json) and docs | every endpoint used: `/api/info`, sessions, prompt, message, permission reply, `session.command` (`goal`), the `opencode.goal` RPC, the form list and reply the question tool uses, `experimental/generate/text`, `/api/event` SSE, `serve --hostname/--port`, `OPENCODE_PASSWORD` | 2026-11-05 | `opencode v2.2.5` |
| [DeepSeek dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) | the authorization stance: the host decides from its own state, and message text is input to a decision, never a grant | 2026-11-04 | `8c32a9dc5d` |
| [Accord/CLAUDE.md](https://accord.dravee.dev/Accord/CLAUDE.md) | the structure of this contract: task sizing, tests ship with the change, completion status, self-rating, confusion protocol, safety | 2026-11-05 | commit `a18354b1` |
| [OpenCode agents and tools](https://opencode.ai/docs/agents/), [tools](https://opencode.ai/docs/tools/), [config](https://opencode.ai/docs/config/) | how a subagent runs: `mode: subagent` is used only in a child session through the `subagent` tool, the default nesting depth is one so a subagent cannot start another, `general` cannot launch subagents, only primary (or `all`) agents can run a session, and `background: true` returns before the child finishes | 2026-11-07 | `opencode v2 docs` |
| [scarletnx/ScarletNX](https://github.com/scarletnx/ScarletNX), Aogo's replies on its pull requests and issues | how GitHub work is reported: the verdict in the first sentence, the facts that matter with their numbers, what waits for a maintainer, the artifacts named before the prose (`vigo/github.py`, `RULES` in `vigo/ask.py`). The mark is `From Vigo (an agent)`, once: a pull request and an issue end with it, a comment starts with it, and it never names the model that ran the turn (`#1277` signed one as the model's name). The replies measured 2026-11-05: 19 comments, median 47 words and one line, four bullets at most, so the rule carries that size | 2026-11-05 | `#1218`, `#1246`, `#1143`, `#1277` |

Re-read one when a task touches it:

```sh
# the reference bot's README, which states its behaviour in full
curl -s https://raw.githubusercontent.com/DraChimeraVee/discord-bots/main/README.md | less

# the API surface this bot calls (the paths and schemas above)
curl -s https://opencode.ai/v2/openapi.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['info'])"

# what has moved since the recorded commit
curl -s https://api.github.com/repos/DraChimeraVee/discord-bots/commits/main | grep -m1 '"sha"'
```

A change matters when it moves something we depend on: a new OpenCode endpoint or a changed request shape, a Discord
API revision, or a safety rule in the reference bot that we do not have. Cosmetic upstream changes are not a reason to
edit anything here.

## How the pieces fit

| File | Holds |
| --- | --- |
| `vigo/config.py` | the settings, checked strictly, one error named at a time |
| `vigo/discord_api.py` | REST v10: commands, interactions, messages, DMs |
| `vigo/gateway.py` | the WebSocket client: handshake, frames, heartbeat, resume |
| `vigo/opencode_api.py` | sessions, prompts, permissions, the SSE event stream, and the agent a task runs under |
| `vigo/router.py` | which device answers, and what may be said about it |
| `vigo/allowlist.py` | who may ask, in a Discord message so both daemons agree |
| `vigo/app.py` | interactions, commands, permission cards, the election of one answerer, and a DM reply that is a question |
| `vigo/ask.py` | one question: session, the got-it line, the answer, the agent's own questions |
| `vigo/goal.py` | when an ask is work to be done, and when it is a question |
| `vigo/notify.py` | a DM to the owner: the update the daemon pulled, or `vigo dm` |
| `vigo/lease.py` | one channel's run at a time across daemons, kept in the session's metadata |
| `vigo/state.py` | what this device remembers between runs: sessions, tickets, answers, and the turn an upgrade's restart cut off |
| `vigo/presence.py` | the status Discord shows (online, idle, do not disturb, invisible) from what the daemon and the servers do |

A direct message that replies to a message vigo posted is an ask in that DM's session. Only the daemon that posted
the referenced message acts on the reply, which is how the two daemons agree on one answerer. It runs through the same
queue and lease as any other ask, is acknowledged once, and its answer is posted as a message, not an edit. The bot
asks for `DIRECT_MESSAGES` only: no privileged intent, and no guild message is read. A plain DM and a reply to
somebody else's message are ignored, and a stranger is refused.

`deploy/` holds the service units and the launchers that unseal the password (Keychain on macOS, systemd-creds on
Linux). `deploy/container/` holds the rootless container: the images, the launcher, the SSH client configuration, the
service files and `SETUP.md`, which lists the host-only packages, secrets, keys and mounts. `art/` holds the icon pipeline.

On Linux the pod runs the daemon and its agent in two containers. `deploy/container/run.sh` uses systemd credentials,
the `vigo-ssh-agent` TPM socket, pasta networking, read-only roots, no capabilities and no-new-privileges; it
publishes 4096 and 4098 only on the tailnet address. The daemon mounts the checkout, settings and state; the agent
mounts `~/Projects`, its own home and the `gh` token. `deploy/build-container.sh` makes the minimal daemon image and
the tools-rich agent image from the pinned release checksums.

On macOS the same two-container pod runs inside the podman machine. The token and the OpenCode password are podman
secrets, because the Keychain cannot enter the VM; the agent gets the password and the optional gh token, and the
daemon gets the token, password and the one SSH key it needs to pull. `~/Library/LaunchAgents/dev.vigo.container.plist`
runs `deploy/container/run.sh`. The launcher starts the podman machine when it does not answer, because the Homebrew
package does not start that VM at login. A rebuild must keep the pinned `ssh_config`, `known_hosts` and the key path,
or `/vigo upgrade` fails inside the container.

## Commits

Batch the work: **one commit per request, or fewer**. Never one per change, and never while changes are still landing.
Run the full suite once, then stage everything and commit.

Vigo signs every commit as `VIGO-VI`. On Linux the signing key is a TPM key held by `vigo-ssh-agent`; on macOS it is
the container's SSH key. The signature carries Vigo's identity and costs no YubiKey and no Touch ID, because it is
that machine's key and not greenstar's. Do not sign as greenstar for the agent, and do not turn signing off to make a
commit look like the owner's. Every commit the agent pushes must show `Verified` on GitHub. The signing key is the one
registered to `VIGO-VI`, never the owner's global signing key.

Every GitHub action runs as `VIGO-VI` too, never as greenstar's account. The commit author, the `gh` token and the
pull request all carry that account. Use the `VIGO-VI` token from the sealed `gh-token` credential for every `gh`
command. Never open, edit, mark ready or close a pull request as greenstar. A pull request authored by greenstar's
account is a mistake even when its content is right, so close it and open the work again as `VIGO-VI`.

## Pull requests

Every change reaches `main` through a pull request, and the first push opens it **as a draft**. A draft runs the quick
stage: one Python, the newest in the matrix, on macOS and Windows. That is where a path, a socket or a clock that
differs by platform shows up, in two jobs instead of six.

Mark the pull request **ready for review** once that stage is green. Ready runs the full matrix, macOS and Windows on
3.12, 3.13 and 3.14, and that is the matrix a merge waits for. Converting back to a draft runs the quick stage again.

Linux is the third platform and no job covers it: run `tests/ci-codespace.sh --machine standardLinux32gb` before the push, and put its answer in
the body. Say what it could not answer when it could not run.

```sh
tests/ci-codespace.sh --machine standardLinux32gb   # Linux 3.12 to 3.14, ruff, secrets
gh pr create --draft --base main --title "…" --body-file …
gh pr checks <number> --watch          # the quick stage
gh pr ready <number>                   # the full matrix
gh pr merge <number> --merge           # the owner runs this, never the agent
```

Do not open a pull request ready, and do not merge with a red matrix. The agent never merges into `main`, whatever
the colour of the matrix; the owner does, every time. Inside an internal fork, the agent may merge branches it created
in the same session onto each other. The reason the stages exist is to answer the platform question in two jobs
before paying for six.

## Finishing

- Status: **DONE**, **DONE_WITH_CONCERNS** (each with its severity), **BLOCKED** (what blocks it, what was tried), or
  **NEEDS_CONTEXT**. "Partially done" is not a status.
- Self-rating before reporting: a score out of 10 from a fresh read, each missing point named, and an honest "am I
  proud of it". If not, fix it and rate again.
- Say what must be restarted for the change to take effect, with the command. A supervisor owns the daemon, so
  restart the job, not the process: `launchctl kickstart -k gui/$(id -u)/dev.vigo.bot` on macOS,
  `systemctl --user restart vigo` on Linux. A `kill` works too, because both units are kept alive; starting a second
  `python3.14 -m vigo` by hand would run two daemons. The settings reload on the next start.
- The agent commits and pushes the branch, and opens the pull request as a draft. The merge into `main` is
  greenstar's, every time: do not run `gh pr merge`, whatever the colour of the matrix.

## Confusion protocol

High-stakes ambiguity: two plausible designs, a request against an established pattern, a destructive operation of
unclear scope, missing context that would change the approach: stop, name it in one sentence, offer two or three real
options with their trade-offs, and ask. Not for routine work.

## Safety

- Never commit secrets. Check `git status` before a commit that touches config or `deploy/`.
- No `rm -rf`, `git reset --hard`, or force-push without explicit confirmation. Prefer moving a file aside to
  deleting it.
- Never skip hooks.
- The bot's token and password are live credentials: if one appears anywhere it should not, say so at once and say
  which rotation is needed. Redacting a log is not rotation.
- Nothing that touches greenstar's machines runs from a channel message. Only a click.

## Text for users

README, commit messages, and the messages the bot posts to Discord are plain prose. They have no emoji, no em dash
between clauses, no semicolon between sentences, and no spaced hyphen as a dash. They have no rule-of-three
flourishes. This rule does not apply to code comments.

## How greenstar wants to be talked to

- English, direct, concrete. Name files, functions and line numbers. Short answers; no praise, no preamble.
- Say the thing that matters first, then the evidence. If something is wrong, lead with that.
- When a requirement is about exposure or permissions, treat it as absolute: no "mostly hidden".
- State what is verified and what is assumed, and never present a passing test as proof of something the test does
  not cover.
