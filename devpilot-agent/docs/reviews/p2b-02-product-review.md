# P2B-02 Product Review

## Decision

`approve` — approval is scoped, one-time, expiring and durable, with separate apply and rollback
capabilities and persistent idempotency protection.

## Evidence

- P2B-02 acceptance and storage approval/idempotency repositories.
- Expiry, revoke, cross-scope, replay, same-key conflict and concurrent consumption tests.
- Raw-token exclusion from results/events and Authorization-header tests.

## Findings

- A failed scope check does not consume the valid approval.
- Rollback requires a new `r1.rollback` approval.
- A token digest, not the raw token, is stored.

## Gate

P2B-02 approved. P2B-03 may proceed.
