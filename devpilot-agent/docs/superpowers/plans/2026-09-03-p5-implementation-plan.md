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

### P5-03 — nanobot WebUI event bridge and action broker

- do not create or restore a DevPilot web workspace;
- expose proposal, plan, Diff summary, risk, approval state, progress, result,
  Artifact and rollback outcome through nanobot's existing WebUI conversation;
- bridge internal DevPilot RunEvent/SSE records into redacted structured
  nanobot messages without changing nanobot's core Agent Loop;
- implement a trusted local action broker for chat confirmation. It may hold a
  capability token only inside the trusted process and must consume it without
  returning it to the model, Tool result, Session, WebUI text, SSE or logs.

### P5-04 — nanobot integration and evidence

- surface Skills, security/backend capability, unavailable/denied states and
  next steps as structured nanobot chat messages;
- add nanobot adapter contract tests, Fake backend event tests, replay/fallback
  tests and a real nanobot chat E2E when the provider is available;
- record P5 evidence and the nanobot-only local runbook; no browser console or
  frontend build is required.

## Verification gates

After each backend slice: focused tests, Ruff, Pyright, and `git diff --check`.
After the nanobot integration slice: adapter contract, event redaction/replay,
and Fake backend tests. Before closing P5: full Python suite, secret scan,
OpenAPI route check, nanobot integration evidence, and two parallel subagent
reviews (Standards and Spec), recorded under `docs/reviews/`.

## Explicit safety boundaries

The nanobot WebUI never receives or stores approval tokens in messages, Session,
URLs, SSE payloads, or logs. The trusted local action broker calls the localhost
API only, does not expose SQLite or project files to the model, and never claims
a run succeeded when the API reports a failure or unavailable backend.
