# P2A-04 Product Review

## Decision

`approve-with-follow-ups` — the durable plan and crash-uncertainty lock satisfy the P2A-04
implementation boundary, but the complete P2A/P2B release Gate remains blocked by evidence and
control-plane work listed below.

## Evidence reviewed

- Frozen P2A-04 recovery requirements and S-03/S-10 cases.
- Alembic `0003_p2a04_recovery`, SQLAlchemy storage records and patch lifecycle integration.
- Persistence, restart recovery, rollback and migration tests.
- Full backend/frontend quality Gate and nanobot entry-point discovery.
- Prior import review, P2A-02/P2A-03 reviews and default-off control-plane guard.

## Acceptance mapping

| Acceptance point | Evidence | Result |
|---|---|---|
| Persist plan before write | `persist_patch_plan` stores bodies, snapshots and manifests during planning | Pass |
| Persist lifecycle | apply/rollback tests assert `applied` and `reverted` database states | Pass |
| Detect uncertain crash | startup scans `applying`; restart test proves durable project lock | Pass |
| Fail closed after uncertainty | locked project rejects a subsequent apply and leaves target unchanged | Pass |
| Preserve stale/user-change safety | existing precondition and `ROLLBACK_CONFLICT` tests remain green | Pass |
| Upgrade existing P1 data | Alembic empty, downgrade and P1-preservation tests pass | Pass |
| Complete release evidence | phase manifest, full failure matrix and production recovery runbook | Follow-up |

## Cross-functional review

- Product: a proposal has durable identity and can survive process loss without silently resuming a
  write.
- Architecture: storage owns recovery facts; the patch engine remains separate from nanobot and
  future Sandbox/ExecutionBackend adapters.
- Security: uncertain state locks the project; rollback refuses to overwrite user changes.
- QA: migration and restart-lock behavior are automated; symlink cases need a privileged CI runner.
- Operations: status and failure reason are persisted, but a full operator unlock/recovery runbook
  is still required before production write exposure.

## Must-fix before P2B acceptance

1. Replace `create_all` startup behavior with an Alembic-head check.
2. Persist Approval/Idempotency records and consume grants atomically across restart/concurrency.
3. Move capability tokens to the `Authorization` header and require a fresh rollback grant.
4. Add failure-injection coverage for every replacement step and a machine-readable phase manifest.

## Gate

P2A-04: approved with follow-ups. P2A-05 is the next planned evidence/fixture node; P2B remains
blocked and default-off.
