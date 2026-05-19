---
name: argit-backup
description: Set up and manage argit backups for OpenClaw agent state. Use when setting up automated workspace backups, configuring argit for the first time, creating a workspace-git-sync cron job, troubleshooting backup issues, handling uncovered paths, or restoring agent state from a git-backed backup. Covers initial setup, manifest overlays, GPG key configuration, cron automation, and disaster recovery.
---

# argit Backup

argit backs up OpenClaw agent state into the workspace git repo:
- **Secrets** (auth tokens, device keys) → GPG-encrypted pass store
- **Config** (openclaw.json) → sanitized (secrets redacted)
- **SQLite DBs** → binary snapshots
- **Data/blobs** (media, lancedb) → git / git-lfs

## Prerequisites

- GPG key for the agent user (encrypts secrets in the backup)
- `pass` (password-store), `gpg`, `git-lfs`, `sqlite3` on PATH
- Workspace must be a git repo with a remote configured
- GitHub access to `blinkbitcoin/argit` (private repo — deploy key bundled in install script)

## Installation

Full install instructions including the bundled deploy key are in the repo README:
<https://github.com/blinkbitcoin/argit#quick-install--upgrade>

### Quick install (copy-paste)

The repo bundles a read-only deploy key for SSH cloning. The one-liner from the README:

```sh
# Requires uv or pipx
# Installs from main branch — replace 'main' with a tag (e.g. v1.7.0) to pin
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# ... (full SSH key setup block is in the README)
# Then:
argit_install main
```

If you already have SSH access to the repo:
```sh
uv tool install git+ssh://git@github.com/blinkbitcoin/argit.git@main
```

Or via `gh`:
```sh
gh repo clone blinkbitcoin/argit /tmp/argit && pipx install /tmp/argit
```

Verify: `argit --version`

### Host dependencies

If missing, argit errors include the install line. For reference:
- `pass`: `apt install pass` / `brew install pass`
- `gpg`: `apt install gnupg` / `brew install gnupg`
- `git-lfs`: `apt install git-lfs && git lfs install` / `brew install git-lfs && git lfs install`
- `sqlite3`: `apt install sqlite3` / `brew install sqlite`

## Commands

| Command | Purpose |
|---------|---------|
| `argit setup` | One-time bootstrap inside an existing git repo |
| `argit backup` | Run backup (sanitize, snapshot, stage) |
| `argit backup --push` | Backup + commit + push in one step |
| `argit review` | List uncovered paths (files not in any manifest) |
| `argit doctor` | Diagnostic report (read-only) |
| `argit restore` | Re-inject secrets, rehydrate SQLite, verify |

## Initial Setup

1. Ensure the workspace is a git repo with a remote:
   ```
   cd ~/workspace && git remote -v
   ```
2. Run setup (accepts manifest upgrades automatically):
   ```
   argit setup --yes
   ```
3. If multiple GPG keys exist, specify the agent key:
   ```
   argit setup --yes --agent-key <FINGERPRINT>
   ```
4. Verify with `argit doctor`.

## Manifest System

argit uses a manifest to classify every file under `~/.openclaw` (the `source_root`):

- **Bundled manifest**: `.argit/manifest/openclaw-<version>.manifest.json` — ships with argit, covers standard OpenClaw paths. *Never edit this file.*
- **Local overlay**: `<bundled-basename>.manifest.local.json` — your customizations. Only needs `items[]` and/or `exclude[]` arrays.

### Item Kinds

| Kind | Treatment |
|------|-----------|
| `secret` | GPG-encrypted into pass store |
| `data` | Copied as-is (plain files/dirs) |
| `sqlite` | Binary snapshot (handles WAL safely) |
| `blob` | For large/binary content (git-lfs recommended) |

### Local Overlay Example

For agent-specific paths not in the bundled manifest, create a local overlay:

```json
{
  "items": [
    { "kind": "secret", "source": "agents/*/agent/auth.json", "mode": "0600" },
    { "kind": "sqlite", "source": "memory/custom.sqlite", "mode": "0600" },
    { "kind": "data", "source": "workspaces/myagent/", "mode": "0644" }
  ],
  "exclude": [
    "cron/jobs.json.*.tmp"
  ]
}
```

Save as `.argit/manifest/<bundled-manifest-basename>.manifest.local.json`.

### Handling Uncovered Paths

When `argit backup` warns about uncovered files:

1. Run `argit review` to see the list
2. Classify each path: is it a secret, data, sqlite, blob, or noise?
3. Add to the local overlay: secrets → `kind: "secret"`, data → `kind: "data"`, noise → `exclude[]`
4. Re-run `argit backup` to confirm clean

## Automated Backup Cron Job

Set up a cron job to run argit backup + git sync on a schedule. See `references/cron-setup.md` for the full cron job task prompt and configuration.

Key settings:
- **Schedule:** every 30 minutes (`every 30m`)
- **Session:** isolated (doesn't pollute main session history)
- **Delivery:** none (silent unless errors)
- **Timeout:** 180 seconds

## Restore

To restore agent state from a backup:

```
argit restore --merge    # overlay onto existing state
argit restore --dry-run  # preview what would be restored
```

Flags:
- `--merge` — overlay restored files onto existing target (safe, non-destructive)
- `--overwrite` — *destructive*: wipes target first, then restores
- `--force` — skip the "is OpenClaw running?" check
- `--skip-lifecycle` — bypass auto stop/start of the agent

⚠️ Always use `--dry-run` first to preview the restore.

## Troubleshooting

- `argit doctor` — shows manifest coverage, GPG status, git state
- `argit review` — lists uncovered paths that need classification
- `argit backup --dry-run` — preview backup actions without executing
- If GPG errors occur, verify the agent key is trusted: `gpg --list-keys`
