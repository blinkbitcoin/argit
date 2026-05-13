# Cron Job Setup for argit + Workspace Sync

## Creating the Cron Job

Use `openclaw cron add` to create an automated backup + sync job.

### Task Prompt

The cron job runs as an isolated agent session. Use this task prompt (adapt workspace paths and repo names):

```
Run these exact steps in order:

**Step 1: argit backup**
1. cd <WORKSPACE_PATH> && argit backup 2>&1
2. If backup output contains "! not backed up" warnings or exits non-zero, run `argit review` and read the review report
3. If there are uncovered paths in the review, update the local manifest overlay to cover them (add items[] or exclude[] entries as appropriate — secrets get kind:secret, data files get kind:data, noise gets excluded)
4. Re-run `argit backup` to confirm clean
5. If you updated the local manifest, note what changed in the commit message

**Step 2: Git sync**
1. cd <WORKSPACE_PATH> && git add -A
2. git diff --cached --quiet (if exit code 0, skip — nothing to commit)
3. git commit -m "auto-sync workspace"
4. source ~/.profile && git push

Do not reply or explain anything. Just run the commands. If argit review finds uncovered paths, fix them silently and proceed.
```

If backing up multiple workspaces, add additional steps for each one (same git add/commit/push pattern).

### Cron Add Command

```bash
openclaw cron add \
  --name workspace-git-sync \
  --schedule "every 30m" \
  --session isolated \
  --delivery none \
  --timeout 180 \
  --task "<paste task prompt above>"
```

### Configuration Notes

| Setting | Value | Why |
|---------|-------|-----|
| `schedule` | `every 30m` | Frequent enough for continuity, light enough on resources |
| `session` | `isolated` | Doesn't pollute main session history |
| `delivery` | `none` | Silent on success; errors surface through cron status |
| `timeout` | `180` | Generous for backup + git push (typically completes in ~60s) |

### Verifying

After creating the cron job:

```bash
openclaw cron list              # confirm it appears
openclaw cron run workspace-git-sync   # test run
openclaw cron runs workspace-git-sync  # check run history
```

### Monitoring

- `openclaw cron show workspace-git-sync` — current state, next/last run
- `openclaw cron runs workspace-git-sync` — recent run history with status
- If `consecutiveErrors` climbs, check `argit doctor` and git remote access

### Adjustments

```bash
openclaw cron edit workspace-git-sync --schedule "every 1h"   # change frequency
openclaw cron disable workspace-git-sync                       # pause
openclaw cron enable workspace-git-sync                        # resume
```
