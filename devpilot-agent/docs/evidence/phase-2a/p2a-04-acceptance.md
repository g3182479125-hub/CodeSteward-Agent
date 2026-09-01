# P2A-04 durable patch lifecycle acceptance

Date: 2026-09-01

## Delivered boundary

P2A-04 persists the structured patch plan before any file replacement, including project identity,
immutable patch bodies, before snapshots, file-change hashes, before/after/rollback manifests and
ChangeSet status. Status transitions for applying, applied, apply-failed and reverted are written
to SQLite.

On service startup, any persisted ChangeSet still in `applying` is treated as an uncertain crash.
The associated project is durably locked and subsequent apply attempts fail closed. Rollback keeps
the existing post-apply hash check, so a user change produces `ROLLBACK_CONFLICT` rather than an
overwrite.

This node does not expose a nanobot write Tool or claim OS Sandbox isolation. The imported P2B HTTP
control plane remains experimental and default-off.

## Migration and verification

- Alembic `0003_p2a04_recovery` adds recovery columns and `project_locks`.
- Empty-database upgrade, downgrade to P1 and upgrade with existing P1 records pass.
- Persistence, restart-lock, normal lifecycle and failure-path tests pass.
- Full result: 71 tests collected; 68 passed and 3 skipped because this Windows account cannot
  create symbolic links.
- Ruff, Pyright, secret scan, Markdown link check and frontend TypeScript/Vite build passed.
- nanobot plugin discovery still returns only the four approved R0/P1 entry points.

## Gate decision

P2A-04 is accepted for the durable plan/lifecycle and fail-closed recovery boundary. Before P2A-05
or P2B can be accepted, the next work must add a machine-readable evidence manifest, complete
failure-injection coverage, durable approval/idempotency records, Authorization-header tokens,
independent rollback authorization and an Alembic-head startup check.
