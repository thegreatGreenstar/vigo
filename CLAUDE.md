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

## Running commands

- Long runs go in the background. You are told when one finishes. Do not sleep, poll, or read its output file while it
  runs.
- Never use `pkill`, `killall` or a `kill $(pgrep -f PATTERN)` loop. The pattern matches the shell that runs it. List
  with `pgrep -a NAME`, then `kill PID` in a separate command. The same applies to an `until` wait: it matches its own
  shell and never ends. Wait for a file the job writes, or use `pgrep -x NAME`.
- Put temporary files in the session temp folder, never in the repo root. Do not print a whole log or a large command
  output. Use `grep`, `head`, or `tail` to read the part that matters.
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
  same suite on threads (stdlib only, ~1.6 s here). CI is two-stage: a draft pull request runs one Python (3.14)
  on Linux, macOS and Windows — the cross-platform question — and marking it ready runs every OS on 3.12–3.14 for
  the version question. A fix for a CI-only failure says what stays unverified.
- A test does not sleep. `tests/fake_opencode.py` takes `("until", event)` in a script, so the test decides when a
  turn ends; a sleep makes the suite slower and flakier for no extra coverage.
- Before pushing, run `tests/in-containers.sh`: the suite on Linux 3.12–3.14 in `podman`, which is the fastest CI
  matrix half to answer. Windows and macOS stay CI-only — `podman` on a Mac is a Linux VM, and Windows has
  already differed from Linux once (a socket read that `shutdown` does not wake there).
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
   that approval is needed, never what the tool wanted to touch.

## Where this design comes from, and when we last looked

Four outside sources shaped this repo. Their changes are not tracked automatically, so a session that touches the
areas below reads them again first, and updates this table when it does.

| Source | What we take from it | Last checked | At |
| --- | --- | --- | --- |
| [DraChimeraVee/discord-bots](https://github.com/DraChimeraVee/discord-bots) (AogoWimsy, XujiWimsy) | the shape of the whole thing: owner approves, drafts and permission cards, a stranger is refused, an allowlist of ids, nothing stored about what was said, stderr without contents | 2026-11-04 | `475b229619` |
| [OpenCode v2 API](https://opencode.ai/v2/openapi.json) and docs | every endpoint used: `/api/info`, sessions, prompt, message, permission reply, `session.command` (`goal`), the `opencode.goal` RPC, the form list and reply the question tool uses, `experimental/generate/text`, `/api/event` SSE, `serve --hostname/--port`, `OPENCODE_PASSWORD` | 2026-11-05 | `opencode v2.2.5` |
| [DeepSeek dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) | the authorization stance: the host decides from its own state, and message text is input to a decision, never a grant | 2026-11-04 | `8c32a9dc5d` |
| [Accord/CLAUDE.md](https://accord.dravee.dev/Accord/CLAUDE.md) | the structure of this contract: task sizing, tests ship with the change, completion status, self-rating, confusion protocol, safety | 2026-11-05 | commit `a18354b1` |

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
| `vigo/opencode_api.py` | sessions, prompts, permissions, the SSE event stream |
| `vigo/router.py` | which device answers, and what may be said about it |
| `vigo/allowlist.py` | who may ask, in a Discord message so both daemons agree |
| `vigo/app.py` | interactions, commands, permission and question cards, the election of one answerer |
| `vigo/ask.py` | one question: session, the got-it line, the answer, the agent's own questions |
| `vigo/goal.py` | when an ask is work to be done, and when it is a question |
| `vigo/notify.py` | a DM to the owner: the update the daemon pulled, or `vigo dm` |
| `vigo/lease.py` | one channel's run at a time across daemons, kept in the session's metadata |

`deploy/` holds the service units and the launchers that unseal the password (Keychain on macOS, systemd-creds on
Linux). `art/` holds the icon pipeline.

## Commits

Greenstar signs every commit by hand, with a YubiKey and Touch ID, so a commit is a cost he pays each time. Batch the
work: **one commit per request, or fewer**. Never one per change, and never while changes are still landing. Run the
full suite once, stage everything, and give him the single message he should sign. If he cancels a signature, the work
stays staged: leave it staged, do not retry, and do not commit again until he says so.

That cost is **macbook's** only. Its key is Greenstar's own, so every commit there waits for his hand (YubiKey,
Touch ID). On **pc** a commit is signed by that machine's TPM key with no hand involved, so it adds no such
user cost. Batch on both anyway: the history is shared, and the Mac reads what the PC wrote.

## Pull requests

Every change reaches `main` through a pull request, and the first push opens it **as a draft**. A draft runs the quick
stage: one Python, the newest in the matrix, on Linux, macOS and Windows. That is where a path, a socket or a clock that
differs by platform shows up, in three jobs instead of nine.

Mark the pull request **ready for review** once that stage is green. Ready runs the full matrix, every OS on 3.12,
3.13 and 3.14, and that is the matrix a merge waits for. Converting back to a draft runs the quick stage again.

```sh
gh pr create --draft --base main --title "…" --body-file …
gh pr checks <number> --watch          # the quick stage
gh pr ready <number>                   # the full matrix
gh pr merge <number> --merge           # only when ready and green
```

Do not open a pull request ready, and do not merge with a red matrix. The reason the stages exist is to answer the
platform question in three jobs before paying for nine.

## Finishing

- Status: **DONE**, **DONE_WITH_CONCERNS** (each with its severity), **BLOCKED** (what blocks it, what was tried), or
  **NEEDS_CONTEXT**. "Partially done" is not a status.
- Self-rating before reporting: a score out of 10 from a fresh read, each missing point named, and an honest "am I
  proud of it". If not, fix it and rate again.
- Say what must be restarted for the change to take effect, with the command. A supervisor owns the daemon, so
  restart the job, not the process: `launchctl kickstart -k gui/$(id -u)/dev.vigo.bot` on macOS,
  `systemctl --user restart vigo` on Linux. A `kill` works too, because both units are kept alive; starting a second
  `python3.14 -m vigo` by hand would run two daemons. The settings reload on the next start.
- Greenstar commits and pushes himself: on macbook with his YubiKey and the Secure Enclave key, on
  pc with that machine's TPM key. Prepare the tree and hand him the command; never `git push` for him.

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
