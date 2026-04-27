# AGENTS.md — things an AI agent should know about this repo

This file is for AI coding agents (Claude Code, Codex, Cursor, etc.) working on argit.
Humans should read `README.md` and `MANIFEST.md` instead.

## What this repo is

`argit` is a standalone Python CLI that backs up and restores a per-user agent's
state into a git repository. Secrets go into a repo-local `pass` store
(dual-recipient encrypted); sanitized config + state commit as plaintext; blobs
go through git-LFS. The engine is agent-agnostic; agent-specific knowledge lives
in bundled manifest templates. MVP target: **OpenClaw**.

## Tech stack

- Python ≥ 3.10, stdlib-first (`hashlib`, `json`, `subprocess`, `pathlib`, `re`, `glob`).
- Only runtime dependency: `click>=8.1`.
- Build: `setuptools` via `pyproject.toml`.
- Lint: `ruff` (config in `pyproject.toml` — rules `E`, `F`, `I`, `B`, `UP`, line length 110).
- Tests: `pytest` unit tier + `bats`-style integration tier.

## Layout

```
src/argit/
  __init__.py           __version__ = "X.Y.Z" (bumped every PR — see below)
  cli.py                click entry points
  setup.py              argit setup — install manifest, drift detection, upgrade flow
  backup.py             argit backup — source → repo
  restore.py            argit restore — repo → source
  doctor.py             argit doctor — diagnostics (read-only)
  manifest.py           Manifest parse + validate + overlay merge + glob expansion
  path_conventions.py   Forward/inverse target+pass derivation, glob grammar
  hashing.py            Canonical-hash helper (drift classifier input)
  sanitize.py           JSON field redaction → pass
  passwrap.py           pass(1) subprocess wrapper
  gpgwrap.py            gpg(1) subprocess wrapper
  shared.py             Exclude matching, misc helpers shared across modules
  errors.py             ArgitError (diagnosis + remediation)
  manifest_templates/   Bundled manifests + hashes.json catalog
  keys/                 Bundled IT-backup GPG public keys

tests/unit/             Stdlib-only, runs on every push
tests/integration/      Needs gpg/pass/sqlite3/git-lfs on PATH (or via nix)
scripts/                rebuild_hash_catalog.py
```

## Running tests

**Unit suite (standard invocation):**

```sh
PYENV_VERSION=3.11.13 PYTHONPATH=$PWD/src rtk proxy python -m pytest tests/unit/
```

- Uses pyenv Python 3.11.13 (consistent across contributors).
- `PYTHONPATH=$PWD/src` so the package imports without a venv install.
- `rtk proxy` avoids terminal-output mangling.

**Integration suite:** prefers nix-shell (`nix develop`) but the tests work with
system `gpg / pass / sqlite3 / git / git-lfs` on PATH. Same `PYENV_VERSION` +
`PYTHONPATH` prefix:

```sh
PYENV_VERSION=3.11.13 PYTHONPATH=$PWD/src rtk proxy python -m pytest tests/integration/
```

## Version bumping (every PR)

Two files hold the version string and they MUST stay in sync:

- `pyproject.toml` — `[project] version = "X.Y.Z"`
- `src/argit/__init__.py` — `__version__ = "X.Y.Z"`

Bump BOTH in every PR, before opening it. Pattern so far has been minor-level
bumps (`1.3.0 → 1.4.0 → 1.5.0`) regardless of whether the PR is a feature or
bugfix. When in doubt, bump the minor.

## Bundled manifest workflow

Each bundled manifest is `src/argit/manifest_templates/<agent-type>-<agent-version>-<rev>.manifest.json`.

- **Never edit an already-shipped revision.** Rev bumps are additive: ship a new
  file (`openclaw-2026.4.14-7.manifest.json`), leave older revisions in place so
  the drift classifier can identify operators on older revs.
- **Regenerate the hash catalog after any manifest change**:
  ```sh
  PYENV_VERSION=3.11.13 PYTHONPATH=$PWD/src rtk proxy python scripts/rebuild_hash_catalog.py --write
  ```
  Commit the updated `manifest_templates/hashes.json` in the same PR.
- CI runs the script WITHOUT `--write` and fails if the catalog is stale.

## Branch + commit conventions

- Branches: `kn/argit-<topic>` (the repo owner's initials prefix). Use a
  topic-scoped name if the PR is narrow (`kn/argit-version-parse-fix`), or a
  generic name if the PR will accumulate multiple fixes
  (`kn/argit-manifest-fixes`).
- Always branch from a fresh `origin/main`:
  ```sh
  git fetch origin && git checkout -b kn/argit-<topic> origin/main
  ```
- Commits: imperative, concise. Subject line ≤ 72 chars. Bullet-point body for
  non-trivial changes. Co-authored-by trailer if pair-programmed with an AI.
- NEVER skip hooks (`--no-verify`) — investigate and fix.

## PR + Copilot review workflow

- Open PRs against `main`.
- Copilot posts review comments on the `pulls/<N>/comments` endpoint.
- **Reply to each Copilot comment** with the commit hash that fixed it (or a
  rationale if deferred). The correct API is `in_reply_to` on the CREATE
  comments endpoint — NOT `/replies`:

  ```sh
  gh api repos/blinkbitcoin/argit/pulls/<N>/comments -X POST \
    -F in_reply_to=<comment-id> \
    -f body="$(cat <<'EOF'
  Fixed in <sha>. <one-sentence explanation>.
  EOF
  )"
  ```

- **Use heredocs for the body.** zsh command-substitution eats backticks inside
  inline `-f body="..."` strings. Always pipe through the heredoc.

## Triage: HIGH vs LOW

When processing a review, triage before implementing:

- **HIGH**: correctness, safety, silent data loss, API-contract mismatch, bypass
  of an invariant.
- **LOW**: unused imports, stale comments, docstring drift, stylistic nits.

Report the triage to the human BEFORE applying fixes; wait for go-ahead. Apply
HIGH fixes first; bundle all fixes into one commit per review round.

## Error handling philosophy

From CLAUDE.md (global), reiterated here because it's load-bearing for argit:

- Never catch an error you don't know how to handle — let it propagate.
- Catch-and-ignore is a code smell.
- When catching: capture diagnostic info, enrich context, inform caller.
- Expected behavior shouldn't throw — change the mechanism instead.
- Preserve error detail when escalating — don't flatten to generic types.

`argit.errors.ArgitError` always carries two strings: **diagnosis** (what
failed) + **remediation** (what the operator should do). Construct it that way.

## Key invariants

- **Exactly one `<...>.manifest.json` per repo in `.argit/manifest/`** (v1
  constraint; multi-manifest-per-repo composition is QS5+ out-of-scope). At most
  one sibling `<basename>.manifest.local.json` overlay.
- **Bundled manifests are strictly-valid**; operator extensions go in the
  overlay. The hash catalog covers bundled only — overlays are hash-invisible.
- **Atomic upgrades**: write `<name>.manifest.json.new`, unlink old (rev-bump
  case) BEFORE `os.replace`. Never a window where two `*.manifest.json` files
  exist simultaneously.
- **Path derivation is strict**: `items[].pass` / `items[].target` /
  `sanitize[].target` / `sanitize.rules[].pass` are derived by
  `path_conventions.py` and MUST NOT appear in the manifest. Derivation is
  lossless-inverse (backup→restore round-trip).
- **Globs in `items[].source`**: single-component `*` only (no `**`).
  Grammar validated by `path_conventions.validate_glob_source`. Expansion by
  `manifest.expand_globbed_item`.

## Spec reference

The multi-track tech-spec driving recent work:
`/Users/kim/src/blink-specs/.claude/worktrees/kn+argit-localisations/argit/implementation-artifacts/tech-spec-argit-manifest-handling.md`

Tracks D (conventions) / A (hash catalog + drift) / C (overlay) / B (globs) are
all merged. Open follow-ups tracked as GitHub issues.

## Don't do these

- Don't edit a shipped bundled manifest revision; add a new one.
- Don't forget to regenerate `hashes.json` after any manifest change.
- Don't use `/replies` for PR comment replies — it doesn't exist; use
  `in_reply_to`.
- Don't skip pre-commit hooks.
- Don't create a commit without bumping the version.
- Don't flatten exceptions — preserve origin attribution in `ArgitError`.
- Don't add backwards-compat shims for code paths nobody uses.
- Don't create documentation files unless the user asks (this one is explicitly
  requested).
