#!/bin/sh
# Make Vigo's own TPM identity, and the files its container's OpenCode needs.
#
#   ./deploy/vigo-identity.sh
#
# It creates two TPM-sealed keys in ~/.ssh-vigo (authentication and signing),
# and puts only the public halves and the git configuration in the container's
# home under ~/.local/share/vigo/home.  The private blobs never enter the
# container; its ssh client asks this machine's vigo-ssh-agent.service.
#
# It prints the two public keys.  Add both to the VIGO-VI GitHub account:
#   https://github.com/settings/keys
# the ecdsa key as an Authentication Key, the signing key as a Signing Key.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=${VIGO_CONTAINER_ROOT:-$HOME/.local/share/vigo}
keys=${VIGO_SSH_KEYS:-$HOME/.ssh-vigo}
home=$root/home
name=${VIGO_GIT_NAME:-Vigo}
email=${VIGO_GIT_EMAIL:-77800979+VIGO-VI@users.noreply.github.com}

for tool in ssh-tpm-keygen ssh-keygen git; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "vigo-identity: $tool is not installed" >&2
        exit 1
    }
done

install -d -m 0700 "$keys" "$home/.ssh" "$home/.config/opencode" \
    "$home/.config/gh" "$home/.local/share/opencode" "$home/.local/state/opencode"
install -d -m 0700 "$root" "$HOME/.config/credstore.encrypted"

make_key() {
    file=$1
    comment=$2
    if [ ! -f "$keys/$file.tpm" ]; then
        echo "vigo-identity: making $keys/$file.tpm" >&2
        ssh-tpm-keygen -t ecdsa -f "$keys/$file" -C "$comment" >/dev/null
    else
        echo "vigo-identity: keeping $keys/$file.tpm" >&2
    fi
    install -m 0644 "$keys/$file.pub" "$home/.ssh/$file.pub"
}

make_key id_ecdsa_vigo "vigo@pc"
make_key id_ecdsa_vigo_signing "vigo@pc-signing"

# The container only ever needs the public keys and the host's GitHub key.
install -m 0644 "$keys/id_ecdsa_vigo.pub" "$home/.ssh/id_ecdsa_vigo.pub"
install -m 0644 "$keys/id_ecdsa_vigo_signing.pub" "$home/.ssh/id_ecdsa_vigo_signing.pub"
if [ -f "$HOME/.ssh/known_hosts" ]; then
    grep -E '(^|,)github\.com[ ,]' "$HOME/.ssh/known_hosts" > "$home/.ssh/known_hosts" || true
fi
if ! grep -q 'github\.com' "$home/.ssh/known_hosts" 2>/dev/null; then
    ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> "$home/.ssh/known_hosts" 2>/dev/null || true
fi
chmod 0644 "$home/.ssh/known_hosts"

{
    printf '# Vigo signs as Vigo, with the TPM key held by this machine.\n'
    printf '[user]\n'
    printf '\tname = %s\n' "$name"
    printf '\temail = %s\n' "$email"
    printf '\tuseconfigonly = true\n'
    printf '\tsigningkey = /home/vigo/.ssh/id_ecdsa_vigo_signing.pub\n'
    printf '[gpg]\n'
    printf '\tformat = ssh\n'
    printf '[commit]\n'
    printf '\tgpgsign = true\n'
    printf '[tag]\n'
    printf '\tgpgsign = true\n'
    printf '[gpg "ssh"]\n'
    printf '\tallowedSignersFile = /home/vigo/.ssh/allowed_signers\n'
    printf '[init]\n'
    printf '\tdefaultBranch = main\n'
    printf '[url "git@github.com:"]\n'
    printf '\tinsteadOf = https://github.com/\n'
} > "$home/.gitconfig"
chmod 0644 "$home/.gitconfig"

{
    printf '%s namespaces="git" ' "$email"
    cut -d' ' -f1-2 "$keys/id_ecdsa_vigo_signing.pub"
} > "$home/.ssh/allowed_signers"
chmod 0644 "$home/.ssh/allowed_signers"

cat > "$home/.ssh/config" <<'EOF'
Host github.com
	User git
	IdentityAgent /run/vigo/ssh-agent.sock
	IdentityFile /home/vigo/.ssh/id_ecdsa_vigo.pub
	IdentitiesOnly yes
	StrictHostKeyChecking yes
	UserKnownHostsFile /home/vigo/.ssh/known_hosts
EOF
chmod 0644 "$home/.ssh/config"

if [ ! -f "$home/.config/opencode/opencode.json" ]; then
    printf '{}\n' > "$home/.config/opencode/opencode.json"
fi
if [ ! -f "$home/.config/opencode/AGENTS.md" ]; then
    cat > "$home/.config/opencode/AGENTS.md" <<'EOF'
# Vigo

You are OpenCode working for Vigo, inside Vigo's rootless container.

- Commit as Vigo and let git sign every commit with the TPM key. Do not change
  `user.name`, `user.email`, or `commit.gpgsign`.
- Only `/home/you/Projects` is writable outside this home. The rest of
  the host is absent, the root filesystem is read-only, and `/run/secrets` is
  not yours to read. Do not try to install packages or change the system.
EOF
fi

if [ ! -s "$home/.ssh/known_hosts" ]; then
    echo "vigo-identity: warning: no github.com host key; ssh-keyscan was blocked" >&2
fi

echo
echo "Add these to https://github.com/settings/keys as VIGO-VI:"
echo
echo "  Authentication key:"
echo "    $(cat "$keys/id_ecdsa_vigo.pub")"
echo
echo "  Signing key:"
echo "    $(cat "$keys/id_ecdsa_vigo_signing.pub")"
echo
echo "The container already trusts the signing key in its allowed_signers."
echo
echo "For gh, make a token on VIGO-VI (repo, read:org) and seal it to this TPM:"
echo "  read -rsp 'VIGO-VI token: ' tok; echo"
echo "  printf '%s' \"\$tok\" | systemd-creds encrypt --user --name=gh-token - \\"
echo "      ~/.config/credstore.encrypted/gh-token"
echo "  unset tok"
echo "Then: systemctl --user restart vigo"
