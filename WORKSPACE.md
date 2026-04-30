# WORKSPACE.md — argit + your workspace dir

How to coexist between argit's backup repo (the dotfile-shaped state at
`.argit/`, `secrets/`, `openclaw/`) and the operator's working directory
(typically `~/workspace`, often referenced by `openclaw.json.workspace`).

This is operator-facing prose. argit itself is **agnostic** to your
workspace layout — you can put it anywhere. This doc names a recommended
default for new installs and migration paths from common existing setups.

---

## Section 1 — Starting fresh (recommended)

If you don't yet have a workspace git repo OR you're willing to merge it
with argit's, **the simplest layout is one repo for everything**:

1. Pick where your workspace lives — typically `~/workspace`. Use that
   as your argit backup repo too: `cd ~/workspace && git init` (or skip
   if it's already a git repo).
2. Run `argit setup` in that same repo. argit installs `.argit/`,
   `secrets/`, and the bundled manifest alongside whatever was already
   at the root.
3. Make sure `secrets/` and `.argit/in-progress` / `.argit/lock` are
   handled correctly (`argit setup` writes the right `.gitignore`
   entries; verify with `cat .gitignore`).

**Why this layout:**

- One git remote, one push. The cron/heartbeat that commits your
  workspace also commits argit's dehydrated state.
- No symlinks, no bind mounts, no path indirection.
- You see all your files at the repo root the way you expect; argit's
  machinery is one obvious dir away (`.argit/`).
- `openclaw.json.workspace` continues to point wherever it pointed
  before — argit doesn't redirect it.

**What NOT to do:**

- Don't `git add secrets/*.gpg` manually. Let argit handle pass-store
  state via `argit backup`. Direct edits bypass the dual-recipient
  encryption invariants.
- Don't hand-edit `.argit/manifest/*.manifest.json` (the bundled
  manifest). Per AGENTS.md, operator extensions go in
  `<basename>.manifest.local.json` overlay. argit's backup-time drift
  warning will flag bundled-manifest edits on every backup cycle.

---

## Section 2 — Migrating from an existing setup

### From "two separate repos" (workspace + argit are different repos)

You probably have `~/workspace` as one git repo (your day-to-day
content) and a separate argit backup repo elsewhere (`~/argit-backup`,
or in a tmpfs dir, or wherever you ran `argit setup`).

To migrate to the recommended single-repo layout:

1. Stop scheduled backups while you migrate. Interrupted state is hard
   to recover from mid-move.
2. In `~/workspace`, run `argit setup`. argit creates `.argit/`,
   `secrets/`, etc. alongside your workspace content.
3. Copy your existing argit-backup repo's `secrets/` directory and
   `.argit/manifest/<basename>.manifest.local.json` (if any) into
   `~/workspace`.
4. Run `argit doctor`. Verify all checks ✓. Fix anything flagged.
5. Run `argit backup` once manually. Inspect the diff with `git diff
   --cached` before committing — confirm no sensitive content lands
   plaintext, no plaintext secret leaks, no missing files.
6. Commit + push.
7. Decommission the old argit-backup repo: archive it locally, retire
   the cron entry, eventually delete after enough successful backup
   cycles in the new home.

### From "argit lives at `.argit/workspace/`" (hidden-mode)

You ran `argit setup` in a backup-only repo and treat `.argit/workspace/`
as the working directory. To bring workspace content to the repo root:

1. `cd <backup-repo>`
2. `git mv .argit/workspace/* ./`
3. Update `openclaw.json.workspace` if it pointed at the old location.
4. Run `argit backup` once and verify the diff.

This may be a substantial diff (every workspace file moves). Consider
doing this on a fresh branch first, reviewing, then merging.

### From "workspace/ peer-at-root in argit repo"

If you already have `<argit-repo>/workspace/` as a peer to `.argit/`
and `secrets/`, you're functionally at the recommended layout — the
only delta is whether the argit repo IS your workspace (single-repo,
recommended) or a separate dir you `cd` into. If you want to consolidate,
move workspace content one level up and retire the `workspace/` subdir.

---

## Appendix — Layout spectrum (background reading)

For completeness, four observed layouts in the wild:

### Mode 1: Hidden mode (`.argit/workspace/`)

```
backup-repo/
├── .argit/
│   ├── manifest/
│   └── workspace/        ← operator's working dir, hidden under dotfile
├── secrets/
└── openclaw/
```

**Pros:** workspace tucked away under a dotfile; argit-as-backup-tool
first, workspace-as-operator-tool second. No clutter at root if the
operator never `cd`s into the repo directly.

**Cons:** unintuitive — the working tree lives under `.argit/`, which
operators expect to be argit-internal state. Editor tooling (VS Code,
JetBrains "open project at root") points at the wrong place.

### Mode 2: Peer-at-root (`workspace/` next to `.argit/`)

```
backup-repo/
├── .argit/
├── secrets/
├── openclaw/
└── workspace/            ← peer of openclaw/
```

**Pros:** workspace visible at repo root; argit's machinery is one
obvious dir away. Plays well with editors that treat the repo root as
the project root. Tech-spec-01's existing `openclaw/` layout already
implies this — `workspace/` is just another peer.

**Cons:** operator must `cd backup-repo/workspace` to do their day-to-day
work. Path indirection adds friction. Two distinct conceptual roots
("the backup repo" vs "the workspace") collapse onto one filesystem
location.

### Mode 3: Workspace's existing repo IS the argit repo (RECOMMENDED)

```
~/workspace/                ← operator's existing git repo
├── <existing workspace files>
├── .argit/                 ← argit setup's output
├── secrets/
└── openclaw/               ← dehydrated agent state
```

**Pros:** zero migration. `argit setup` slots in alongside whatever the
operator already had. Single git remote, single push, single cron entry.
The repo has dual identity — both the working tree and the argit backup
target.

**Cons:** argit's machinery is co-mingled with the operator's content
at the repo root. `.gitignore` rules need to be tight (don't accidentally
stage `secrets/*.gpg` from a heuristic `git add -A`). `argit setup`
already writes the right entries; verify before relying on them.

### Mode 4: Two repos (status quo for many operators)

Workspace repo at `~/workspace`, argit backup repo at `~/argit-backup`
(or `/tmp/argit-backup` for tmpfs-on-shutdown setups).

**Pros:** complete separation of concerns. The argit backup repo holds
strictly argit state; the workspace repo holds strictly content.

**Cons:** two pushes (or two cron entries). Two remote credentials.
Future-argit's multi-manifest model would let you compose them into one
repo cleanly; today, they stay separate.

---

## See also

- [README.md](README.md) — argit setup + commands reference.
- [MANIFEST.md](MANIFEST.md) — manifest authoring + overlay convention.
- [AGENTS.md](AGENTS.md) — repo-wide conventions for AI agents
  contributing to argit.
