# argit — per-user agent backup & restore

`argit` is a standalone Python CLI that backs up and restores a per-user agent's
state into a git repository. Secrets live in a repo-local `pass` store (dual-recipient
encrypted with the operator's GPG key + an IT backup key); sanitized config and state
commit as plaintext. MVP targets **OpenClaw** only.

## Quick Install / Upgrade

The argit repo is private. Installation uses a bundled read-only deploy key
to clone over SSH. Copy-paste this block (it sets up the SSH key + Host
alias once, then installs/upgrades to whatever ref is at the end —
**re-run the same block to upgrade**):

```sh
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/config && chmod 600 ~/.ssh/config

cat > ~/.ssh/argit-deploy <<'EOF'
-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACCbHQz3D+jQgiaKYDWXdsf/LGvw0GJWm3y6h3snQNVNzwAAAJjbdI4O23SO
DgAAAAtzc2gtZWQyNTUxOQAAACCbHQz3D+jQgiaKYDWXdsf/LGvw0GJWm3y6h3snQNVNzw
AAAEAiLVilcVJz2bSoI5QY4qH5W4ECMGmNWl4jGeBLuwhO4JsdDPcP6NCCJopgNZd2x/8s
a/DQYlabfLqHeydA1U3PAAAAFWFyZ2l0LWRlcGxveS1yZWFkb25seQ==
-----END OPENSSH PRIVATE KEY-----
EOF
chmod 600 ~/.ssh/argit-deploy

if ! grep -q "^Host github-argit$" ~/.ssh/config 2>/dev/null; then
  cat >> ~/.ssh/config <<EOF

Host github-argit
  Hostname github.com
  User git
  IdentityFile ~/.ssh/argit-deploy
  IdentitiesOnly yes
EOF
fi

# Pin GitHub's ED25519 host key (idempotent). Avoids the non-interactive
# "Host key verification failed" error on fresh hosts where pipx/uv can't
# accept the key on first connection. Key fetched from api.github.com/meta.
touch ~/.ssh/known_hosts && chmod 600 ~/.ssh/known_hosts
if ! grep -q "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" ~/.ssh/known_hosts 2>/dev/null; then
  echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" >> ~/.ssh/known_hosts
fi

argit_install() {
  local URL="git+ssh://git@github-argit/blinkbitcoin/argit.git@${1:-main}"
  if command -v uv >/dev/null 2>&1; then
    uv tool install --force "$URL" || return $?
  elif command -v pipx >/dev/null 2>&1; then
    pipx install --force "$URL" || return $?
  else
    echo "neither uv nor pipx found — install one first" >&2; return 1
  fi
  argit --version
}; argit_install main
```

The trailing word — `main` — is the git ref. To install or upgrade to a
different ref, hit **Ctrl-E** to jump to end-of-line, replace `main` with
`v1.2.0` (or any tag/branch/SHA), and hit Enter. Re-running with the same
ref reinstalls (use this to pick up new commits on `main`).

If `argit` isn't on PATH after install, run `pipx ensurepath` and `source ~/.bashrc` (or `~/.zshrc`).

### About the bundled deploy key

The key above has **read-only** access scoped to `blinkbitcoin/argit` (no write,
no other repos, no user). Anyone with the README contents can clone argit;
that's the trade-off chosen for `curl|paste|bash` UX over a private repo.
GitHub permits this pattern for read-only deploy keys. If the key needs
rotation, ship a new argit release with an updated README and revoke the old
deploy key on the repo.

### Alternative install methods

If you have your own SSH key registered with GitHub and read access to the repo:

```sh
uv tool install git+ssh://git@github.com/blinkbitcoin/argit.git@main
```

If you have `gh` authenticated:

```sh
gh repo clone blinkbitcoin/argit /tmp/argit && pipx install /tmp/argit
```

## Six-Step Bootstrap

```sh
curl -fsSL https://raw.githubusercontent.com/blinkbitcoin/argit/main/install.sh | bash
mkdir openclaw-backup && cd openclaw-backup && git init
argit setup                                              # copies manifest, .gitattributes, imports IT key, prints pass-init command
cd secrets && PASSWORD_STORE_DIR=. pass init <agent-fpr> 1107BD74F292CD3EAB0CF59D49F2D3353A88D34E && cd ..
argit backup
git add -A && git commit -m 'initial backup' && git push # or: argit backup --push on subsequent runs
```

## Commands

### `argit setup`

One-time (idempotent) bootstrapping inside an existing git-init'd repo. Copies the
bundled OpenClaw manifest, appends the git-lfs line to `.gitattributes`, creates
`secrets/`, imports the bundled IT backup public key (after interactive confirmation
unless `--yes`), and prints the exact `pass init` command for the operator to run.

Flags:

- `--yes` — skip the IT-key-import confirmation.
- `--agent-key <fpr>` — pick the operator's GPG fingerprint explicitly. **Required**
  when `gpg --list-keys` returns more than one non-IT key.
- `--dry-run` — print actions without executing.

### `argit doctor`

Diagnostic-only status report. No mutations. Exits 0 if every check passed, 1 on
any failure. Each failed check prints the exact remediation command. Also previews
lifecycle commands declared in the manifest — useful for auditing what a restore
would execute.

### `argit backup`

Reads the manifest, extracts sanitized secrets to the repo-local pass store,
dumps SQLite DBs via `sqlite3 .backup`, copies data trees, syncs media via
git-lfs. Default is **write-only** (no git operations).

Flags:

- `--commit` — stage + commit managed paths (excluding the manifest file itself,
  `.argit/in-progress`, and `.argit/lock`).
- `--push` — implies `--commit`, then `git push`.
- `--strict` — fail hard on unspecified files in `source_root` (files not covered
  by any `items[]`, `sanitize[]`, or `exclude[]` rule).
- `--dry-run` — print actions without executing.

### `argit restore`

Reverses a backup. Re-injects secrets from pass, rehydrates the source tree,
runs the final verify phase (no `${pass:}` placeholders, every pass path
resolves, SQLite `PRAGMA integrity_check` is `ok`, modes match manifest).

Flags:

- `--target <dir>` — restore somewhere other than the manifest's `source_root`.
  **Always test against `/tmp/argit-scratch` first** before restoring over a live install.
- `--overwrite` — `rm -rf` the target first. **Destroys EVERY file in the target**,
  including ones the backup doesn't know about. Prompts unless `--yes`.
- `--merge` — overlay restored state; files already in target are preserved.
  Mutually exclusive with `--overwrite`.
- `--yes` — skip interactive confirmations.
- `--force` — skip the lifecycle running-check; restore proceeds even if the
  agent is live (loud warning).
- `--skip-lifecycle` — bypass `detect_running` / `stop` / `start` entirely.
- `--dry-run` — print actions without executing.

## Destructive-Restore Warning

`argit restore --overwrite` calls `shutil.rmtree(<target>)` before rehydrating.
If the target is your live `~/.openclaw/` and something outside the manifest lives
there (plugin state, operator-added notes, anything else), **it is gone**.

**Always dry-run first into a scratch dir**:

```sh
argit restore --target /tmp/argit-scratch --overwrite --yes
# inspect /tmp/argit-scratch; if correct, only then:
argit restore --overwrite
```

## Troubleshooting

### gpg-agent not caching the passphrase

If the first `argit backup` or `argit restore` hangs for 30s and then prints the
pinentry hint, gpg-agent hasn't cached your GPG passphrase. Prime it:

```sh
echo test | gpg --decrypt --armor --quiet --recipient <your-fpr> 2>/dev/null || gpg --edit-key <your-fpr> trust quit
```

Easier: run any `pass show <some-entry>` interactively once.

### Missing host binaries

argit errors include the exact install line. For reference:

- `pass`: `brew install pass` / `apt install pass`
- `gpg`: `brew install gnupg` / `apt install gnupg`
- `git-lfs`: `brew install git-lfs && git lfs install` / `apt install git-lfs && git lfs install`
- `sqlite3`: `brew install sqlite` / `apt install sqlite3`

### LFS pointer files after clone

If `argit restore` fails the verify phase with "git lfs pointer", the repo was
cloned without LFS content. Run:

```sh
git lfs pull
argit restore
```

### git push authentication fails

Two recommended setups:

1. **GitHub CLI**: `gh auth login` — handles HTTPS remotes out of the box.
2. **Personal SSH key**: set up GitHub with SSH as usual; `git push` Just Works.

Advanced (deploy-key-in-pass pattern): see
[bot-provisioning-poc/scripts/on_host/secrets_setup.sh](https://github.com/blinkbitcoin/bot-provisioning-poc/blob/main/scripts/on_host/secrets_setup.sh).
Automated deploy-key setup is planned for a later QS; out of MVP scope.

### `UnicodeEncodeError` mid-command (`LANG=C` containers)

argit's status output uses `✓ → ✗` characters. In containers without a UTF-8 locale,
Python defaults to ASCII and chokes. Fix:

```sh
export PYTHONIOENCODING=utf-8
# or set a locale: export LANG=C.UTF-8
```

### Running `argit restore` while OpenClaw is live

Default behavior: argit probes `lifecycle.detect_running`. If the agent is running
and `lifecycle.stop` is defined, argit stops it (polling `detect_running` up to 30s)
before restoring. Lifecycle commands are printed to stderr before execution so you
can audit what's about to run.

- `--force` — skip the stop phase; restore while the agent is live. Loud warning.
  **Corruption risk**: a live SQLite DB writer can interleave with the restore.
- `--skip-lifecycle` — skip probe + stop + start entirely. Manage the lifecycle
  yourself.

## Trust Boundary

argit lifecycle commands (`lifecycle.detect_running` / `.stop` / `.start`) execute
arbitrary shell in your user context. Install only from the official
`blinkbitcoin/argit` repo; never apply a manifest from an untrusted source.

argit's security model assumes:

1. Your local user environment is trusted.
2. You installed argit from the official `blinkbitcoin/argit` GitHub repo.
3. GPG-protected secrets in the backup repo are safe against attackers who do
   not have the private key.

argit does **not** defend against: push-access attackers tampering with
`lifecycle.*` commands in the repo-copy (mitigation: lifecycle argv is echoed to
stderr before exec, `argit doctor` previews commands, `--skip-lifecycle`
bypasses), supply-chain compromise of argit itself, or compromised GPG private
keys.

See [tech-spec-01-mvp.md](argit/implementation-artifacts/tech-spec-01-mvp.md)
and [MANIFEST.md](MANIFEST.md) for design rationale and manifest authoring.

## License

MIT.
