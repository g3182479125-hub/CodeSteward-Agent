# P4-04 final acceptance removal

Date: 2026-09-21 (Asia/Shanghai)

This is a corrective supplement to the historical P4-04 evidence. The original
`p4-04-acceptance.md` records the behavior that existed when P4-04 was first
implemented; it is retained as immutable historical evidence and is not the
current runtime contract.

## Current contract

- Verification ends the delivery flow directly. There is no new `accept`
  decision, `delivery.accepted` event, acceptance intent, or acceptance API
  path.
- The delivery report exposes only the recovery actions that still exist:
  `stop` and `rollback` where their state rules allow them.
- `stop` remains an explicit user action and never claims success or rolls back
  files. Rollback remains capability-gated by the existing control plane.
- The `accepted` enum value remains readable so an old database can be opened.
  A legacy accepted ChangeSet is read-only: new approval, apply, verification,
  stop, rollback, revoke, and recovery mutations are rejected with
  `CHANGESET_LEGACY_READ_ONLY`.

## Verification

The focused regression command passed:

```powershell
$env:PYTHONPATH = (Resolve-Path ..\nanobot).Path
..\nanobot\.venv\Scripts\python.exe -m pytest -q tests/test_p4_delivery_decision.py tests/test_user_intent.py
```

Result: 9 passed. Ruff and `git diff --check` also passed. The focused tests
cover the removed HTTP decision, natural-language rejection of acceptance,
legacy read-only behavior, stop idempotency, and the remaining report/stop/
rollback contract.
