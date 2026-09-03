# P5 Implementation Plan

## Delivery order

P5 is delivered as four vertical slices. Each slice keeps the existing domain
manager as the deep module and adds only the adapter needed by the next
consumer.

### P5-01 — HTTP contract adapter

- add canonical `/api/v1` aliases while retaining `/v1`;
- add bounded request IDs to JSON bodies and response headers;
- normalize validation and unexpected errors;
- add OpenAPI and HTTP contract tests.

### P5-02 — durable event projection and SSE

- expose append-only run events with stable event IDs and per-run sequence;
- add `GET /api/v1/runs/{id}/events` using `Last-Event-ID`;
- emit a finite current projection so clients can reconnect safely;
- add tests for ordering, replay, redaction, and invalid cursors.

### P5-03 — local React console

- restore a small, optional DevPilot web workspace under `devpilot-agent/web`;
- implement Dashboard, Projects, ChangeSet/Diff, Approvals, Run Timeline, and
  Artifacts views against `/api/v1`;
- keep approval and rollback actions inside the ChangeSet detail flow with
  explicit confirmation; do not add an approval-only page or touch nanobot's
  chat WebUI;
- add keyboard-visible focus, semantic buttons, loading/error states, and a
  polling fallback when SSE is unavailable.

### P5-04 — skills/security and evidence

- add Skills and Security/Backends views;
- surface unavailable/denied states honestly;
- add browser build and smoke checks;
- record P5 evidence and a reproducible local runbook.

## Verification gates

After each backend slice: focused tests, Ruff, Pyright, and `git diff --check`.
After the web slice: TypeScript build and a browser smoke test against a fake
API fixture. Before closing P5: full Python suite, web build, secret scan,
OpenAPI route check, and two parallel subagent reviews (Standards and Spec),
recorded under `docs/reviews/`.

## Explicit safety boundaries

The console never receives or stores approval tokens in local storage, URLs,
SSE payloads, or logs. It calls the localhost API only, does not access SQLite
or project files directly, and never claims a run succeeded when the API
reports a failure or unavailable backend.
