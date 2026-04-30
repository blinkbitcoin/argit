# argit review report — ${iso}

- **Backup:** `${iso}`
- **Manifest:** `${manifest_filename}` (overlay: `${overlay_basename}.manifest.local.json` — ${overlay_status})
- **Uncovered:** ${count} path${plural}

## What this report is

Files under your `source_root` exist that aren't matched by any rule in the bundled manifest. Until each one is covered, `argit backup` warns about it but doesn't include it in the backup. To stop the warnings: either tell argit how to back each one up, or tell it to ignore.

## How to act on each path

Edit `${overlay_basename}.manifest.local.json` (NEVER the bundled manifest — it's hash-protected and `argit setup` will refuse to upgrade past local edits). The overlay merges with the bundled at load time. Pick the rule kind that fits:

### `kind: data` — plain config / state files (most common)

Use for JSON, text, or small binary files that are NOT credential material. Backed up as-is to `openclaw/data/<source>`.

```json
{
  "items": [
    { "kind": "data", "source": "plugins/foo.json", "mode": "0644" }
  ]
}
```

For a whole directory, use a trailing slash:

```json
{ "kind": "data", "source": "telegram/", "mode": "0644" }
```

### `kind: secret` — whole-file credentials extracted to `pass`

Use when the entire file IS credential material (a token, a private key, a session bearer). The file is read at backup time, stored in the repo-local `pass` store under `argit/<agent_type>/<source-without-extension>`, and replaced with a `${pass:...}`-shaped reference on restore.

```json
{ "kind": "secret", "source": "credentials/github.token.json", "mode": "0600" }
```

### `kind: sqlite` — SQLite database (snapshotted via `.backup`)

Use for `.sqlite` / `.db` files. argit runs `sqlite3 <source> .backup <tmp>` to capture a consistent snapshot (handles WAL/SHM correctly). The snapshot lands at `openclaw/state/<source>`.

```json
{ "kind": "sqlite", "source": "memory/main.sqlite", "mode": "0600" }
```

### `kind: blob` — large binaries / media (LFS-tracked dir)

Use for directories of images, audio, video, or other large binary content. Tracked via git-lfs. Source MUST end with a trailing slash.

```json
{ "kind": "blob", "source": "media/inbound/", "mode": "0644" }
```

### `sanitize` — JSON config with embedded secrets at known paths

Use when ONE field inside a config file is a secret but the rest of the file is plain config. Argit reads the source JSON, extracts the value at each rule's `path`, stores it in `pass`, and writes a sanitized copy to `openclaw/config/<file>` with `${pass:...}` placeholders.

```json
{
  "sanitize": [
    {
      "file": "openclaw.json",
      "rules": [
        { "path": ".gateway.auth.token",         "pass": "argit/openclaw/gateway/auth-token" },
        { "path": ".channels.telegram.botToken", "pass": "argit/openclaw/channels/telegram-bot-token" }
      ]
    }
  ]
}
```

Supports a single whole-segment `*` wildcard inside `path` for one-rule-many-keys (e.g., `.channels.telegram.accounts.*.botToken`); see MANIFEST.md §Wildcards.

### `exclude` — file/dir argit should NOT back up

Use for logs, caches, `.bak` files, ephemeral state, and anything else regenerable. Glob patterns (`*` is multi-segment-friendly here, unlike in `items[].source`).

```json
{ "exclude": ["plugins/*/cache/", "*.bak", "logs/"] }
```

## Uncovered paths

${uncovered_paths}

## After editing

1. Re-run `argit review` to confirm the paths you addressed are no longer flagged.
2. Commit the overlay change and push (your repo's existing cron / heartbeat handles this if it's autonomous).
3. Next backup will include the newly-covered files.

## Workspace coexistence

If you maintain a separate git-backed workspace directory (e.g., `~/workspace` referenced by `openclaw.json.workspace`), see [WORKSPACE.md](${workspace_doc_url}) for the recommended layout (default: workspace-repo-IS-argit-repo).
