# P5 final candidate handoff

## Current implementation

P5 now uses nanobot's native WebUI only. A trusted WebUI message carrying an exact tuple
selected by the user from the displayed reference is accepted only for the exact
confirmation words `执行`, `验证`, or `撤销`. The WebSocket Gateway resolves that
reference against the current loopback Control Plane ChangeSet before signing and passes
only a non-persistent confirmation context plus a verifier-only capability through
`RequestContext.attributes`.
The model never receives a key, token, metadata copy, transcript copy, or idempotency key.

The Broker validates the signature, current trusted-project snapshot, and expiry, then
atomically reserves the confirmation in SQLite before issuing a Control Plane capability.
It generates an unpredictable hidden idempotency key and persists it only for crash recovery;
the record otherwise stores hashes and redacted audit fields, with terminal status written back.
Alembic head is `0010_p5_persist_idempotency_key`. An existing Alembic database at an older head is
refused rather than silently altered by `create_all()`.

## Validation

- DevPilot full suite completed without a failure; focused P5 and migration tests pass.
- DevPilot Ruff and Pyright pass.
- nanobot confirmation/BaseChannel/gateway runtime tests: `27 passed, 1 skipped` with TEMP/TMP redirected to E:.
- `git diff --check` passes.
- Native WebUI client test covers serialization of the exact confirmation tuple. The local
  npm registry/cache was unavailable during this run, so Vite compilation is not a P5 gate
  under ADR-0003 and remains an environment-only follow-up.

## Known limitation

The nanobot WebUI caller sends a ChangeSet tuple only when the user explicitly selects or
quotes the displayed reference; callers may also pass the tuple explicitly. A caller that
sends only the word without a user-selected tuple fails closed.
Provider-backed live chat remains an environment test, not a reason to claim a fake success.
