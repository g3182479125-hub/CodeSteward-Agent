# P2B local control-plane and nanobot proposal adapter

Date: 2026-08-31  
Status: approved for implementation

## Goal

Connect the P2A structured-patch service to the local nanobot runtime without
giving the model a write capability. The user should be able to ask nanobot to
propose a bounded file change, inspect the generated diff, explicitly approve
it through the local DevPilot control plane, apply it, and roll it back.

## Architecture

The control plane is a localhost-only FastAPI application started with
`devpilot serve`. It owns the in-process proposal registry and is the only
component allowed to call `StructuredPatchService.apply` or `rollback`.

The nanobot adapter is a proposal-only Tool. It sends a JSON request to the
control plane and returns the ChangeSet id, hashes, file summary, and unified
diff. It never receives an approval token and never calls an apply endpoint.

The operator flow is:

```text
nanobot Tool -> POST /v1/change-sets/preview -> diff response
user/control UI -> POST /v1/change-sets/{id}/approve -> one-time token
user/control UI -> POST /v1/change-sets/{id}/apply -> apply + hash receipt
user/control UI -> POST /v1/change-sets/{id}/rollback -> rollback receipt
```

The first increment keeps the active plan registry in the control-plane
process. P2B records lifecycle events through the existing store; durable
reconstruction of patch bodies across a process restart is a later persistence
increment and must not weaken the approval boundary.

## API and data contracts

- `POST /v1/change-sets/preview` accepts `project_path`, `project_id`, and a
  bounded list of structured patches.
- `GET /v1/change-sets/{id}` returns the immutable preview and current status.
- `POST /v1/change-sets/{id}/approve` validates the request is local and emits
  an expiring, one-time operator token. The token is never included in a
  nanobot Tool response.
- `POST /v1/change-sets/{id}/apply` requires the operator token and rechecks
  project, scope, patch hash, and file preconditions before applying.
- `POST /v1/change-sets/{id}/rollback` requires the apply receipt held by the
  control plane and performs conflict-aware rollback.

All errors use stable `PatchError.code` values. Tokens are stored only as
digests and are excluded from logs and model-visible payloads.

## Security and failure behavior

- Bind defaults to `127.0.0.1`; no public or nanobot-facing write route is
  added.
- Proposal requests reuse P2A path, secret-file, symlink, size, and operation
  validation.
- Approval is explicit, scoped to one ChangeSet, expires, and is consumed once.
- Stale plans produce zero writes.
- Apply failures trigger the existing rollback path; rollback conflicts or
  rollback failures remain fail-closed.
- The proposal Tool returns only safe summaries and Diff text, never secrets or
  approval tokens.

## Tests

Add contract tests for preview, proposal Tool behavior, approval token scope,
one-time apply, stale-hash zero-write, rollback, and rejection of non-local
control-plane configuration. Retain the complete P1/P2A regression suite.
