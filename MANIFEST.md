# argit Manifest Specification

A manifest is the contract between argit's agent-agnostic engine and a specific
agent type. It declares what state lives where, which fields are secret, how
state should be snapshotted, and (optionally) how the agent's lifecycle is
controlled. argit core contains no agent-specific knowledge; everything that
distinguishes "OpenClaw backup" from "Hermes backup" lives in a manifest.

This document is the canonical reference for manifest authors.

---

## Filename and versioning

Manifest files live in the backup repo at `.argit/manifest/<filename>.manifest.json`.
Filenames follow Debian-style three-component naming:

```
<agent-type>-<agent-version>-<manifest-revision>.manifest.json
```

Example: `openclaw-2026.4.14-1.manifest.json`.

- **agent-type** — lowercase identifier: `openclaw`, `hermes`, `paperclip`, etc.
- **agent-version** — the upstream agent release this manifest targets.
- **manifest-revision** — integer starting at `1`, incremented when the manifest
  itself is corrected (fixed sanitize rule, added missed path, etc.) for an
  unchanged agent version.

One manifest per repo for MVP. Multi-manifest composition is post-v1.

---

## Top-level structure

```json
{
  "schema_version": 1,
  "agent_type": "openclaw",
  "agent_version": "2026.4.14",
  "manifest_revision": 1,
  "source_root": "~/.openclaw",
  "source_root_mode": "0700",
  "blob_backend": "git-lfs",
  "sanitize": [ ... ],
  "items": [ ... ],
  "exclude": [ ... ],
  "lifecycle": { ... }
}
```

### Required fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Must be `1` for MVP. argit rejects unknown values. |
| `agent_type` | string | Lowercase identifier. Must match the filename prefix. |
| `agent_version` | string | Semver-ish version string. Must match the filename middle component. |
| `manifest_revision` | integer | ≥ 1. Must match the filename trailing number. |
| `source_root` | string | Absolute path (with `~` expansion) to the agent's state directory. |
| `source_root_mode` | string | Octal mode string (e.g., `"0700"`) applied to `source_root` on restore. |
| `blob_backend` | string | `"git-lfs"` for MVP. Sole supported value. |
| `sanitize` | array | See *Sanitize Rules* below. May be empty. |
| `items` | array | See *Items* below. Must have at least one entry. |
| `exclude` | array | Glob patterns relative to `source_root`. May be empty. |

### Optional fields

| Field | Type | Meaning |
|---|---|---|
| `lifecycle` | object | See *Lifecycle* below. When absent, argit does not probe, stop, or start the agent. |

---

## Sanitize rules

Sanitize rules handle **files that mix config and secrets** — the rule extracts
secret fields into the pass store and writes a sanitized copy to the repo. On
restore, placeholders are re-injected from pass.

```json
{
  "file": "openclaw.json",
  "target": "openclaw/config/openclaw.json",
  "mode": "0600",
  "rules": [
    { "path": ".gateway.auth.token",         "pass": "argit/openclaw/gateway/auth-token" },
    { "path": ".channels.slack.appToken",    "pass": "argit/openclaw/channels/slack-app-token" },
    { "path": ".env",                        "pass": "argit/openclaw/env",        "subtree": true }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `file` | string | Path relative to `source_root` of the source file to sanitize. |
| `target` | string | Path relative to the repo root where the sanitized output is written. |
| `mode` | string | Octal mode applied to the restored file. |
| `rules[]` | array | One or more extraction rules. |
| `rules[].path` | string | Dotted JSON path (`.foo.bar.baz`). Single whole-segment `*` wildcard supported (see §Wildcards). |
| `rules[].pass` | string | pass-store entry path (under `argit/<agent-type>/...`). |
| `rules[].subtree` | boolean (optional, default `false`) | When `true`, the whole JSON subtree at `path` is serialized and stored as one pass entry. When `false` (default), only the leaf value is stored. |

### Subtree rules

Use `subtree: true` for map-like configs where the keyspace grows over time
(e.g., `.env`, where OpenClaw can add new `ENV_VAR_NAME` keys without a manifest
bump). The entire object is stored as a JSON string in pass; argit deserializes
on restore.

Leaf rules are safer when the field is stable (single token, single bearer).

### Placeholder format

Sanitized files contain `${pass:<pass-path>}` placeholders at every rule's
`path`. The placeholder is a literal string in the JSON value position — not a
JSON keyword:

```json
{
  "gateway": {
    "auth": {
      "token": "${pass:argit/openclaw/gateway/auth-token}"
    }
  }
}
```

### Wildcards

A single whole-segment `*` is supported in `rules[].path`. At sanitize-time
the rule expands to one concrete rule per matched key in the dict at the
wildcard depth, each with its own derived `pass_path`.

```json
{ "path": ".channels.telegram.accounts.*.botToken" }
```

Resolves against:
```json
{ "channels": { "telegram": { "accounts": {
  "default": { "botToken": "tok-d", "allowFrom": "..." },
  "erbot":   { "botToken": "tok-e" }
} } } }
```

…producing two pass entries:
- `argit/openclaw/openclaw/channels/telegram/accounts/default/bot-token` ← `tok-d`
- `argit/openclaw/openclaw/channels/telegram/accounts/erbot/bot-token` ← `tok-e`

Non-secret keys in each subtree (e.g. `allowFrom`) remain visible in the
sanitized JSON.

Constraints:
- `*` MUST be a whole segment — `foo*` and `*bar` are rejected.
- At most one `*` per path. Nested wildcards: split into multiple rules.
- `*` MUST NOT be the first segment.
- Zero matches at the wildcard depth are treated as "skipped" (warn-and-
  continue), the same as a fixed path that doesn't exist in the source.
- A non-dict prefix raises (author/config bug).

For top-level fan-out, store the whole file as a `kind: secret` item.

---

## Items

Items cover every path under `source_root` that needs backing up AND is NOT
handled by a sanitize rule. Four kinds:

### `kind: secret`

File is stored whole in pass (encrypted). Intended for pure-secret files
(private keys, bearer-token JSON with wildcard paths).

```json
{ "kind": "secret", "source": "identity/device.json", "pass": "argit/openclaw/identity/device", "mode": "0600" }
```

### `kind: data`

File or directory is copied into the repo as plaintext. Safe after sanitization.

```json
{ "kind": "data", "source": "telegram/", "target": "openclaw/data/telegram/", "mode": "0644" }
```

File items write to a single path; directory items (trailing `/`) copy
recursively.

### `kind: sqlite`

SQLite database, captured via `sqlite3 <source> ".backup <tmp>"` and committed
as a binary snapshot. Handles WAL/SHM siblings transparently. On restore, the
committed binary is copied back to source.

```json
{ "kind": "sqlite", "source": "memory/main.sqlite", "target": "openclaw/state/memory-main.sqlite", "mode": "0600" }
```

### `kind: blob`

Binary media. Tracked via git-lfs (per `blob_backend`).

```json
{ "kind": "blob", "source": "media/browser/", "target": "openclaw/media/browser/", "mode": "0644", "blob_backend": "git-lfs" }
```

### Common fields

| Field | Required | Meaning |
|---|---|---|
| `kind` | yes | `secret`, `data`, `sqlite`, or `blob`. |
| `source` | yes | Path relative to `source_root`. Directories end with `/`. |
| `target` | yes for `data`/`sqlite`/`blob` | Path relative to repo root. |
| `pass` | yes for `secret` | pass-store entry path. |
| `mode` | yes | Octal mode applied on restore. |
| `blob_backend` | optional, only for `kind: blob` | Currently must be `"git-lfs"` if set. |

---

## Exclude

Glob patterns relative to `source_root`. Matching paths:

- Are NOT backed up.
- Do NOT count toward the unspecified-files warning in `argit backup` (default)
  or error (`--strict`).

Typical exclusions: regenerable output (shell completions, session logs),
ephemeral state (delivery queues), redundant on-disk backups (`.bak*`), SQLite
WAL/SHM siblings, plugin state out of MVP scope.

```json
"exclude": [
  "openclaw.json.bak*",
  "agents/main/sessions/",
  "completions/",
  "logs/",
  "*.sqlite-wal",
  "*.sqlite-shm",
  "qqbot/"
]
```

---

## Lifecycle (optional)

Three primitives so argit core stays agent-agnostic. argit executes each as
subprocess argv with 30s timeout.

```json
"lifecycle": {
  "detect_running": {
    "description": "OpenClaw gateway HTTP health endpoint responds",
    "command": ["sh", "-c", "curl -sSf --max-time 2 http://127.0.0.1:10001/health > /dev/null"],
    "running_exit_code": 0
  },
  "stop": {
    "description": "Stop OpenClaw (systemd, launchd, or SIGTERM fallback)",
    "command": ["sh", "-c", "systemctl --user stop openclaw-gateway 2>/dev/null || launchctl unload ~/Library/LaunchAgents/openclaw.plist 2>/dev/null || pkill -TERM -f 'openclaw.*gateway'"]
  },
  "start": {
    "description": "Start OpenClaw",
    "command": ["sh", "-c", "systemctl --user start openclaw-gateway 2>/dev/null || launchctl load ~/Library/LaunchAgents/openclaw.plist 2>/dev/null || echo 'Start manually, then run: argit doctor'"]
  }
}
```

| Field | Type | Meaning |
|---|---|---|
| `detect_running` | object | Optional probe. When absent, argit does not check if the agent is running. |
| `detect_running.command` | array | argv for subprocess. Typical: health probe, port check, `pgrep`. |
| `detect_running.running_exit_code` | integer | Exit code indicating "running". Default `0`. |
| `stop` | object | Optional stop command. Called by `argit restore` when `detect_running` reports running. |
| `start` | object | Optional start command. Called by `argit restore` at the end of a successful restore (unless `--target <dir>` was used). |

### Restore flow with lifecycle

1. Pre-flight checks pass.
2. If `lifecycle.detect_running` is defined, probe.
   - Not running → proceed.
   - Running AND `--force` → warn loudly, proceed without stopping.
   - Running AND `--force` NOT passed AND `lifecycle.stop` defined → run `stop`;
     re-probe `detect_running` at `poll_interval_ms` intervals up to
     `timeout_sec` (defaults 500ms / 30s); if still running after the budget,
     fail.
   - Running AND `lifecycle.stop` NOT defined → fail with "stop it manually or
     pass --force".
3. Restore phases (config + secrets + data + sqlite + blob + permissions +
   verify).
4. If `lifecycle.start` is defined AND target is `source_root` (not `--target
   <dir>`), run `start`. Non-zero exit = warning, not failure.

### Trust boundary

`lifecycle` commands execute arbitrary shell in the operator's user context.
Only install manifests from trusted sources. The MVP trust model assumes:

- argit is installed only from the official `blinkbitcoin/argit` GitHub repo.
- Bundled manifests (shipped inside argit's Python package) are the only trusted
  manifests.
- Operators do not hand-edit or download manifests from elsewhere.

Post-v1 may introduce signed manifests to tighten this boundary.

---

## Invariants argit enforces

argit validates these on manifest load; violations raise `ArgitError` pointing
at the specific field:

- `schema_version == 1`.
- Filename matches `<agent_type>-<agent_version>-<manifest_revision>.manifest.json`
  AND the three components match the top-level fields.
- Exactly one manifest in `.argit/manifest/`.
- All required top-level fields present and non-empty.
- Every `items[]` entry has a valid `kind`.
- `sanitize[]` rules have non-empty `rules[]`.
- `path` in sanitize rules: at most one whole-segment `*`, never the first segment (see §Wildcards).
- `mode` strings parse as 3- or 4-digit octal values (e.g. `"700"` or `"0700"` —
  both accepted, normalized internally).
- `lifecycle.*.command` is a non-empty argv list when defined.

---

## Authoring a new manifest

Adding support for a new agent type (Hermes, Paperclip, a next-gen successor):

1. **Inventory.** Get a real-world copy of the agent's state directory.
   Enumerate every top-level path.
2. **Classify.** For each path, apply the classification rubric:
   - **secret** — contains credential material (keys, tokens, bearers).
   - **data** — structured state, safe in plaintext git after sanitize.
   - **sqlite** — SQLite database file (any file with `.sqlite`/`.db` extension
     and `SQLite format 3` magic bytes).
   - **blob** — binary media (images, archives, browser profiles).
   - **exclude** — regenerable, ephemeral, redundant.
3. **Sanitize rules.** Open every config file that might mix secrets with
   non-secrets. Identify secret fields. Decide leaf-vs-subtree per field. Write
   `sanitize[]` entries.
4. **pass namespace.** Use `argit/<agent-type>/...` consistently. Subnamespaces
   match directory structure.
5. **Lifecycle.** If the agent has an HTTP health endpoint, port, or PID file,
   write `detect_running`. If it has a stop/start idiom (systemd unit, launchd
   agent, `docker stop`), write `stop` + `start`. All three are optional — omit
   what doesn't apply.
6. **Exclude list.** Add patterns for every regenerable / ephemeral path found
   in step 2.
7. **Canonical form.** Serialize with sorted keys, 2-space indent, trailing
   newline. Compute `jq -S . | sha256sum` for the hash QS4 will eventually
   consume.
8. **Test with fixture.** Build a round-trip fixture (see tech-spec-01 §Testing
   Strategy) and prove sanitize → dehydrate → destroy → rehydrate yields
   byte-identical state.
9. **Ship.** Drop the manifest into `src/argit/manifest_templates/`. Bump
   `manifest_revision` if correcting an existing agent-version manifest.

### Hermes sessions policy

The bundled Hermes manifest intentionally excludes `sessions/` by default.
Session JSONL transcripts power cross-session recall (`session_search`) but are
not required for a functional disaster recovery restore: config, secrets,
memories, workspace, skills, cron/scripts, pairing data, and SQLite/fact-store
state are enough to boot an operational agent. On active instances, sessions can
dominate backup size and push time.

Operators who want transcript recall can opt in with a local overlay next to the
bundled manifest, for example
`.argit/manifest/hermes-2026.5.4-1.manifest.local.json`:

```json
{
  "items": [
    { "kind": "blob", "source": "sessions/" }
  ]
}
```

Other possible future treatments are an explicit optional tier in the manifest
schema or a retention policy such as "last N days of sessions"; neither exists
in schema version 1, so the current supported override is the overlay item.

---

## Example: full OpenClaw MVP manifest

The bundled manifest at `src/argit/manifest_templates/openclaw-2026.4.14-1.manifest.json`:

```json
{
  "schema_version": 1,
  "agent_type": "openclaw",
  "agent_version": "2026.4.14",
  "manifest_revision": 1,
  "source_root": "~/.openclaw",
  "source_root_mode": "0700",
  "blob_backend": "git-lfs",

  "sanitize": [
    {
      "file": "openclaw.json",
      "target": "openclaw/config/openclaw.json",
      "mode": "0600",
      "rules": [
        { "path": ".gateway.auth.token",          "pass": "argit/openclaw/gateway/auth-token" },
        { "path": ".channels.telegram.botToken",  "pass": "argit/openclaw/channels/telegram-bot-token" },
        { "path": ".channels.slack.botToken",     "pass": "argit/openclaw/channels/slack-bot-token" },
        { "path": ".channels.slack.appToken",     "pass": "argit/openclaw/channels/slack-app-token" },
        { "path": ".env",                         "pass": "argit/openclaw/env", "subtree": true }
      ]
    },
    {
      "file": "exec-approvals.json",
      "target": "openclaw/config/exec-approvals.json",
      "mode": "0600",
      "rules": [
        { "path": ".socket.token", "pass": "argit/openclaw/exec-approvals/socket-token" }
      ]
    }
  ],

  "items": [
    { "kind": "secret", "source": "identity/device.json",                  "pass": "argit/openclaw/identity/device",            "mode": "0600" },
    { "kind": "secret", "source": "identity/device-auth.json",             "pass": "argit/openclaw/identity/device-auth",       "mode": "0600" },
    { "kind": "secret", "source": "credentials/github-copilot.token.json", "pass": "argit/openclaw/credentials/github-copilot", "mode": "0600" },
    { "kind": "secret", "source": "agents/main/agent/auth-profiles.json",  "pass": "argit/openclaw/agents/auth-profiles",       "mode": "0600" },
    { "kind": "secret", "source": "devices/paired.json",                   "pass": "argit/openclaw/devices/paired",             "mode": "0600" },

    { "kind": "data", "source": "update-check.json",                        "target": "openclaw/data/update-check.json",                        "mode": "0644" },
    { "kind": "data", "source": "agents/main/agent/auth-state.json",        "target": "openclaw/data/agents/main/agent/auth-state.json",        "mode": "0644" },
    { "kind": "data", "source": "agents/main/agent/models.json",            "target": "openclaw/data/agents/main/agent/models.json",            "mode": "0644" },
    { "kind": "data", "source": "credentials/slack-pairing.json",           "target": "openclaw/data/credentials/slack-pairing.json",           "mode": "0644" },
    { "kind": "data", "source": "credentials/slack-default-allowFrom.json", "target": "openclaw/data/credentials/slack-default-allowFrom.json", "mode": "0644" },
    { "kind": "data", "source": "devices/pending.json",                     "target": "openclaw/data/devices/pending.json",                     "mode": "0644" },
    { "kind": "data", "source": "canvas/index.html",                        "target": "openclaw/data/canvas/index.html",                        "mode": "0644" },
    { "kind": "data", "source": "telegram/",                                "target": "openclaw/data/telegram/",                                "mode": "0644" },

    { "kind": "sqlite", "source": "memory/main.sqlite",      "target": "openclaw/state/memory-main.sqlite",    "mode": "0600" },
    { "kind": "sqlite", "source": "tasks/runs.sqlite",       "target": "openclaw/state/tasks-runs.sqlite",     "mode": "0600" },
    { "kind": "sqlite", "source": "flows/registry.sqlite",   "target": "openclaw/state/flows-registry.sqlite", "mode": "0600" }
  ],

  "exclude": [
    "openclaw.json.bak*",
    "agents/main/sessions/",
    "completions/",
    "logs/",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "qqbot/"
  ],

  "lifecycle": {
    "detect_running": {
      "description": "OpenClaw gateway HTTP health endpoint responds on default port 10001",
      "command": ["sh", "-c", "curl -sSf --max-time 2 http://127.0.0.1:10001/health > /dev/null"],
      "running_exit_code": 0
    },
    "stop": {
      "description": "Stop OpenClaw (systemd user unit, launchd agent, or SIGTERM fallback)",
      "command": ["sh", "-c", "systemctl --user stop openclaw-gateway 2>/dev/null || launchctl unload ~/Library/LaunchAgents/openclaw.plist 2>/dev/null || pkill -TERM -f 'openclaw.*gateway'"]
    },
    "start": {
      "description": "Start OpenClaw (same fallback chain as stop). Authors must keep this idempotent against an already-running agent (systemctl is; pkill+exec is not).",
      "command": ["sh", "-c", "systemctl --user start openclaw-gateway 2>/dev/null || launchctl load ~/Library/LaunchAgents/openclaw.plist 2>/dev/null || echo 'Could not auto-start OpenClaw. Start manually, then run: argit doctor'"]
    }
  }
}
```
