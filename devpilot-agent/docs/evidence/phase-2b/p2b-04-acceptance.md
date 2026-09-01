# P2B-04 approval-to-rollback E2E acceptance

Date: 2026-09-01

The deterministic E2E flow is repeatable:

`nanobot proposal -> persisted Preview/Diff -> operator approval -> Authorization-header apply -> persisted result -> fresh rollback approval -> conflict-aware rollback`

Evidence includes manager restart reconstruction, apply replay, concurrent one-time consumption,
expiry, revocation, cross-ChangeSet scope rejection, HTTP Header/idempotency contract and
non-loopback rejection. The full P1/P2A regression suite remains green, and the frontend production
build passes.

P2B is complete for local structured text delivery. It does not execute repository code, provide a
Shell fallback or claim OS Sandbox isolation; those belong to P3/P6 and remain fail-closed.
