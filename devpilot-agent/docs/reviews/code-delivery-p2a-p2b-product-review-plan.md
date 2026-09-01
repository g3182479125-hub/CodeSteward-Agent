# DevPilot P2A/P2B Product Delivery Review Plan

## Review authority

This review uses `DevPilot-Code-Delivery-Agent-冻结版实施计划.md`, ADR-0001, the P1
acceptance evidence, and the imported remote implementation at GitHub commit
`036ce19` as evidence. It does not redefine the product plan.

## Intended user outcome

The user can inspect a bounded, immutable code-change proposal, explicitly approve the exact
scope and patch, apply it atomically, inspect the resulting diff and hashes, and perform a
conflict-aware authorised rollback. The model never receives a write capability or approval
secret.

## Explicit non-goals

- No arbitrary shell, dependency installation, Git push/deploy, or R2 execution.
- No real Sandbox claim.
- No public network binding, multi-user control plane, or remote marketplace.
- No P3 ExecutionBackend implementation.
- No model-triggered apply or rollback.

## Node acceptance questions

| Node | Acceptance questions |
|---|---|
| P2A-01 | Are framework-free entities, state guards, persistence records and Alembic upgrades complete and P1-compatible? |
| P2A-02 | Are bounded search/read capabilities delivered with path, binary, secret and output limits? |
| P2A-03 | Is the proposed patch canonical, immutable, bounded and safe against Windows path aliases and stale hashes? |
| P2A-04 | Are apply, rollback and crash recovery atomic, durable and fail-closed without hidden writes? |
| P2A-05 | Do fixtures and evidence cover S-01/S-02/S-03/S-10 and a real diff-to-rollback exercise? |
| P2B-01 | Are ChangePlan and ChangeSet previews versioned, immutable and durably reconstructable? |
| P2B-02 | Are approval/grant expiry, revocation, one-time atomic consumption, rollback approval and idempotency enforced? |
| P2B-03 | Is the model adapter proposal-only, is the control plane loopback-only, and do contracts hide capability secrets? |
| P2B-04 | Does the end-to-end flow survive concurrency, replay, restart, cross-project misuse and stale content? |

## Evidence required

- Unit tests for all legal and illegal state transitions.
- Empty-database and P1-to-head Alembic upgrade/downgrade tests.
- Security tests for traversal, symlink, Windows case aliases, reserved devices, ADS, forbidden files,
  binary content, stale hashes, duplicate targets and patch budgets.
- Failure injection across staging/replacement/rollback and restart recovery evidence.
- API contract tests for loopback enforcement, approval header handling, expiry, revocation,
  idempotency, atomic concurrent consumption and rollback authorisation.
- A machine-readable evidence manifest tied to the local Git commit and migration revision.
- P1 regression, pytest, Ruff, Pyright, Secret scan and Markdown checks.

## Security and compatibility checks

- Secrets never enter Prompt, Session, Tool payloads, SSE, logs or Artifact content.
- R1 actions require an exact immutable patch/scope/plan binding.
- Model-visible Tools cannot approve, apply, rollback or select capabilities.
- Control-plane startup does not bypass Alembic with `create_all`.
- Existing P1 Project/Run/Artifact data and Tool contracts remain compatible.
- Failure or restart does not silently turn an unknown ChangeSet into success.

## Scope-creep and blockers

- P3/P4/P5 work is out of scope until the P2A/P2B Gate passes.
- The future Sandbox is not required for P2A/P2B and cannot be used to defer their file and
  approval safety requirements.
- Provider/network availability is not acceptance evidence for deterministic Domain and Policy logic.

## Severity rules

- **Blocker**: exposes unauthorised write/rollback, leaks a capability, bypasses migration/safety,
  corrupts or cannot recover project state.
- **Must-fix-before-next-node**: required contract/evidence is absent or materially weaker than plan.
- **Follow-up**: safe bounded improvement that does not invalidate the current node outcome.
- **Observation**: documentation or maintainability note with no Gate impact.

## Decision states

- `approve`: all required outcomes and evidence pass.
- `approve-with-follow-ups`: Gate passes; only non-blocking follow-ups remain.
- `request-changes`: one or more blocker/must-fix findings prevent the next node.
- `blocked`: external evidence or owner decision is required before review can conclude.

## Report template

Each report records decision, node status, evidence inspected, findings by severity, unanswered
questions, exact remediation and the evidence required to unblock the next node.
