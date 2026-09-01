# P2B-02 approval and idempotency acceptance

Date: 2026-09-01

The control plane now issues scoped, expiring approvals whose raw token is returned only to the
operator response and whose database representation is a SHA-256 digest. `r1.apply` and
`r1.rollback` are independent capabilities. Consumption is conditional and atomic; expired,
revoked, replayed, cross-ChangeSet and hash-mismatched approvals fail closed.

Apply and rollback accept the capability through `Authorization: DevPilotGrant <token>`. Mutating
HTTP requests require `Idempotency-Key`; same-key same-content requests replay the stored result,
while same-key different-content requests conflict. Approval and idempotency state survives manager
restart. Rollback always needs a fresh rollback approval.

P2B-02 is accepted by durable manager, expiry, revoke, scope, replay, conflict and concurrency
tests. Raw tokens are not in ChangeSet response payloads, event payloads or apply results.
