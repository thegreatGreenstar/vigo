#!/bin/sh
# Run what CI runs, on a Codespace: the Linux half of the matrix, ruff and the
# secrets check, on a clean Linux machine rather than on this one.
#
#   tests/ci-codespace.sh                     # create, run, delete
#   tests/ci-codespace.sh --keep              # stop it instead, so a rerun is fast
#   tests/ci-codespace.sh --machine standardLinux8Core
#   tests/ci-codespace.sh --base main         # the ref whose .devcontainer to use
#   tests/ci-codespace.sh --name my-run       # the display name to create
#
# The tree that runs is this working tree, uncommitted changes included: it is
# copied with `gh codespace cp`, so nothing has to be pushed first.  The
# Codespace's own checkout of `--base` only supplies `.devcontainer`, which is
# where Python 3.13 and 3.14 come from; a run without it answers 3.12 alone and
# says so.
#
# Codespaces is Linux only.  macOS and Windows stay in the workflow, because no
# codespace can answer them.
#
# The gh token needs the codespace scope, once per token:
#
#   gh auth refresh -h github.com -s codespace
#
# The codespace this script creates is stopped and deleted however the run ends,
# unless --keep says to stop it instead.  A stopped codespace still holds
# storage against the quota, so `gh codespace delete -c NAME` it when done.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
repo=thegreatGreenstar/vigo
keep=0
machine=
base=
name=
created=

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) keep=1 ;;
        --machine) shift; machine=${1:?--machine needs a value} ;;
        --base) shift; base=${1:?--base needs a value} ;;
        --name) shift; name=${1:?--name needs a value} ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }

if ! command -v gh >/dev/null 2>&1; then
    say "gh is not on PATH: it is what creates and drives a codespace"
    exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
    say "gh is not logged in: run 'gh auth login' first"
    exit 1
fi
# The one check that separates "logged in" from "allowed to make a codespace".
if ! gh codespace list --limit 1 >/dev/null 2>&1; then
    say "the gh token has no codespace scope: run 'gh auth refresh -h github.com -s codespace'"
    exit 1
fi

# The branch supplies .devcontainer, so it must exist on the remote.  When it
# does not, the default branch stands in and the run names what it lost.
if [ -z "$base" ]; then
    branch=$(git -C "$here" rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'main')
    if [ "$branch" != "HEAD" ] \
        && git -C "$here" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
        base=$branch
    else
        base=$(git -C "$here" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')
        base=${base:-main}
        say "$branch is not on origin, so .devcontainer comes from $base"
    fi
fi

if [ -z "$name" ]; then
    name="vigo-ci-$(date +%m%d-%H%M%S)"
fi

# The secrets check needs a git repository, and the copy has none, so it runs
# here, on the same bytes, and a failure stops the run before a codespace is
# made.  The remote half is told to skip it.
if git -C "$here" ls-files | grep -qE '(^|/)vigo\.json$'; then
    say "no secrets: FAILED (vigo.json is tracked)"
    exit 1
elif git -C "$here" grep -qE --untracked \
    'Bot [A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}'; then
    say "no secrets: FAILED (a Discord bot token is in the tree)"
    exit 1
else
    say "no secrets: OK"
fi

tmp=$(mktemp -d)

cleanup() {
    rm -rf "$tmp"
    if [ -z "$created" ]; then
        return 0
    fi
    if [ "$keep" -eq 1 ]; then
        gh codespace stop -c "$created" >/dev/null 2>&1 || true
        say "stopped $created; delete it with 'gh codespace delete -c $created'"
    else
        gh codespace delete -c "$created" --force >/dev/null 2>&1 || true
        say "deleted $created"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

say "creating a codespace from $repo at $base ..."
set -- -R "$repo" -b "$base" --display-name "$name" \
    --idle-timeout 15m --retention-period 24h
if [ -n "$machine" ]; then
    set -- "$@" --machine "$machine"
fi
created=$(gh codespace create "$@" | tail -1 | tr -d '\r')
if [ -z "$created" ]; then
    say "gh created no codespace"
    exit 1
fi
say "created $created"

started=$(date +%s)
while :; do
    state=$(gh codespace view -c "$created" --json state --jq .state 2>/dev/null || printf 'Unknown')
    if [ "$state" = "Available" ]; then
        break
    fi
    if [ "$(( $(date +%s) - started ))" -ge 600 ]; then
        say "$created is still $state after ten minutes; the image build may have failed:"
        gh codespace logs -c "$created" 2>&1 | tr -d '\r' | tail -40 || true
        exit 1
    fi
    sleep 5
done
say "available after $(( $(date +%s) - started ))s"

# The tree, as a commit would ship it: tracked and untracked-but-not-ignored.
# `.state/`, `vigo.json` and a `.venv` are ignored, so they stay here.  `.git`
# stays here too: a worktree's `.git` is a pointer to another folder, and the
# secrets check already ran above.
files=$tmp/files
if ! git -C "$here" ls-files --cached --others --exclude-standard >"$tmp/tracked" 2>/dev/null; then
    say "cannot list the tree: is $here a git clone?"
    exit 1
fi
: >"$files"
while IFS= read -r file; do
    if [ -e "$here/$file" ] || [ -L "$here/$file" ]; then
        printf '%s\n' "$file" >>"$files"
    fi
done <"$tmp/tracked"
tar -czf "$tmp/tree.tgz" -C "$here" -T "$files"

gh codespace cp -c "$created" "$tmp/tree.tgz" "remote:/workspaces/vigo-ci.tgz"

log=$tmp/run.log
status=0
gh codespace ssh -c "$created" -- \
    'rm -rf /workspaces/vigo-ci && mkdir -p /workspaces/vigo-ci && tar -xzf /workspaces/vigo-ci.tgz -C /workspaces/vigo-ci && cd /workspaces/vigo-ci && tests/in-codespace.sh --no-git' \
    </dev/null >"$log" 2>&1 || status=$?

tr -d '\r' <"$log" | tail -80
say ""
if [ "$status" -eq 0 ]; then
    say "codespace $created: OK"
else
    say "codespace $created: FAILED (exit $status)"
fi
say "macOS and Windows: not here, they run in .github/workflows/tests.yml"
exit "$status"
