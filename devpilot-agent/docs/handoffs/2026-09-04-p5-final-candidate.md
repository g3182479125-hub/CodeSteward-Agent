# P5 final candidate handoff

## Current implementation

P5 now uses nanobot's native WebUI only. A trusted WebUI message carrying the exact
displayed `change_set_id`, `plan_hash`, `patch_hash`, `scope_hash`, and `project_id`
is accepted only for the exact confirmation words `执行`, `验证`, or `撤销`.
The WebSocket Gateway signs that tuple in-process and passes only a non-persistent
confirmation context plus a restricted signer object through `RequestContext.attributes`.
The model never receives a key, token, metadata copy, transcript copy, or idempotency key.

The Broker validates the signature, current trusted-project snapshot, and expiry, then
atomically reserves the confirmation in SQLite before issuing a Control Plane capability.
The record stores only hashes and redacted audit fields; terminal status is written back.
Alembic head is `0009_p5_broker_tuple_replay`. An existing Alembic database at an older head is
refused rather than silently altered by `create_all()`.

## Validation

- DevPilot full suite completed without a failure; focused P5 and migration tests pass.
- DevPilot Ruff and Pyright pass.
- nanobot confirmation/BaseChannel tests: `14 passed`.
- nanobot gateway runtime tests: `23 passed, 1 skipped` with TEMP/TMP redirected to E:.
- `git diff --check` passes.
- Native WebUI client test covers serialization of the exact confirmation tuple. The local
  npm registry/cache was unavailable during this run, so Vite compilation is not a P5 gate
  under ADR-0003 and remains an environment-only follow-up.

## Known limitation

The nanobot WebUI caller automatically carries the latest displayed ChangeSet tuple when
the user sends the exact words `执行`, `验证`, or `撤销`; callers may also pass the tuple
explicitly. A caller that sends only the word without a displayed tuple fails closed.
Provider-backed live chat remains an environment test, not a reason to claim a fake success.
