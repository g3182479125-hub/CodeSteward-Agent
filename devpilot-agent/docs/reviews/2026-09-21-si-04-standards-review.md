# SI-04 Standards Review

- Baseline: `8a778d1`
- Final candidate: `7c53b86`
- Reviewer: Standards subagent
- Result: **PASS**

## Scope checked

- Task-start Git facts are immutable; placeholder migration cannot upgrade a dirty,
  non-Git or missing-commit task.
- Project registration, trusted state, repository fingerprint and allowed-root ID
  are verified by the server-owned store.
- Replay worktrees use a trusted root, reject symlinks/junctions and repository-
  contained paths, and recheck source identity and detached commit.
- Cleanup failures are persisted as pending cleanup records.
- Legacy or invalid replay snapshots are excluded from Replay, Candidate and Cron
  selection, including final lock-held rechecks.
- Source evidence references are append-only during placeholder completion.

## Verification

- SI-04 focused tests: `23 passed, 1 skipped`.
- Nanobot Skill/task regressions: `23 passed`.
- Ruff: passed.
- Pyright for new replay and adapter modules: 0 errors.
- `git diff --check`: passed.

