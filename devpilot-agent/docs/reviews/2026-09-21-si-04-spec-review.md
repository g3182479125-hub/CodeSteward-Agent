# SI-04 Spec Review

- Baseline: `8a778d1`
- Final candidate: `7c53b86`
- Reviewer: Spec subagent
- Result: **PASS**

## Exit conditions checked

- Real Git detached worktrees are isolated, use the captured commit, and leave
  the source project unchanged; two replays receive different worktrees.
- Dirty, non-Git and missing-commit snapshots each create `not_replayable` Cases
  and are absent from `eligible` selection.
- Registered/trusted project, repository identity, allowed file scope and
  verification Profile are bound to the snapshot.
- Symlink and Windows junction private roots are rejected; unavailable symlink
  creation is explicitly skipped by the environment-aware test.
- Cleanup failure is visible and persisted for later cleanup.
- Legacy snapshots and Cases without an explicit replay proof are rejected by
  replay, candidate and scheduler gates.

## Evidence

- SI-04 focused tests: `24 passed, 1 skipped`.
- `git diff --check 8a778d1..7c53b86`: passed.
- Working tree was clean at review time.

