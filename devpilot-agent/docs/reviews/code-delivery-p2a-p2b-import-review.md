# DevPilot Imported P2A/P2B Product Review

## Decision

`request-changes` — the imported implementation is valuable and its current deterministic tests
pass, but it does not satisfy the frozen P2A/P2B Gate. P3 must not start yet.

## Evidence inspected

- Remote GitHub commit `036ce19` and its `devpilot-agent/` subtree.
- Local baseline commit `835cfdc` and ADR commit `ec43f16`.
- Frozen plan P2A/P2B nodes and ADR-0001.
- Domain, migration, patch, control-plane and nanobot adapter source.
- Imported tests and P2A evidence/spec documents.
- Local verification: `47 passed, 1 skipped`; Ruff, Pyright, Secret scan and Markdown passed.

## Node decisions

| Node | Decision | Reason |
|---|---|---|
| P2A-01 | approve-with-follow-ups | Core entities, transition tables and `0002` migration exist and P1 upgrade is tested; persistence repository methods and complete Project trust state remain. |
| P2A-02 | request-changes | `devpilot_search_code` and `devpilot_read_code` are not implemented. |
| P2A-03 | request-changes | Patch hashes and budgets exist, but Windows case aliases, reserved device names, trailing dot/space and NTFS ADS are not rejected. |
| P2A-04 | request-changes | Apply/rollback works in-process, but manifests and snapshots are memory-only; crash recovery and durable unknown-state locking are absent. |
| P2A-05 | request-changes | Evidence is not tied to the imported Git commit, lacks the frozen manifest, and required S-01/S-02/S-03/S-10 coverage is incomplete. |
| P2B-01 | request-changes | Preview and patch bodies are stored only in `_entries`; restart loses the immutable proposal and cannot reconstruct it. |
| P2B-02 | request-changes | Apply token is accepted in JSON, rollback has no approval, consumption is memory-only, and revocation/idempotency/concurrency persistence is absent. |
| P2B-03 | request-changes | Proposal-only Tool is directionally correct, but loopback is only guarded by CLI bind choice and the runtime Tool schema is weaker than its decorator schema. |
| P2B-04 | request-changes | No restart, concurrent consume, cross-project, idempotency, rollback authorisation or localhost-client E2E evidence. |

## Blockers

1. `POST .../rollback` performs an R1 write without a fresh capability grant. This violates the
   frozen control-plane boundary.
2. Approval secrets are posted in JSON bodies even though the frozen contract requires
   `Authorization: DevPilotGrant <token>` and excludes secrets from model/loggable payloads.
3. `create_app()` calls `ProjectStore.initialize()` (`create_all`) instead of requiring the
   Alembic head, allowing production/control-plane schema creation outside migrations.
4. Active plans, patch bodies, approvals, token consumption and receipts are memory-only. A
   restart destroys the source of truth and cannot support the required crash recovery.

## Must fix before next node

1. Deliver P2A-02 bounded search/read before claiming P2A completion.
2. Canonicalise Windows paths case-insensitively and reject reserved devices, ADS, trailing dots
   and spaces; add duplicate-alias tests.
3. Persist immutable ChangePlan/ChangeSet/FileChange content and lifecycle events through
   SQLAlchemy/Alembic repositories.
4. Add durable Approval/Idempotency records with atomic one-time consumption, expiry and revoke.
5. Require independently approved rollback and move capability tokens to the Authorization header.
6. Enforce loopback at the ASGI request boundary as well as CLI bind validation.
7. Replace control-plane `create_all` with an Alembic-head schema check/startup failure.
8. Add failure-injection and restart recovery tests; update evidence with a manifest tied to the
   final local commit.

## Follow-ups

- Remove the obsolete `TestAuthorizationGrant` name once the production grant repository owns
  authorisation.
- Ensure rollback removes empty directories created solely by the ChangeSet or explicitly records
  them as a known residual effect.
- Standardise the API under `/api/v1` and the common result/error envelope before P5 contracts freeze.

## Unanswered questions

None require user input. The frozen plan already determines the safer choices above.

## Unblock evidence

The review may move to `approve` only after all blockers and must-fix findings have tests, the full
quality Gate passes, and `docs/evidence/phase-2/manifest.json` references the actual merged commit.
