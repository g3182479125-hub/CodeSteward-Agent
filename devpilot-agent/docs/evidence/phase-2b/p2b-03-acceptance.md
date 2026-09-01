# P2B-03 API, nanobot adapter and approval UI acceptance

Date: 2026-09-01

P2B-03 delivers:

- a localhost-only FastAPI control plane with ASGI request-boundary loopback enforcement;
- preview, get, approve, apply, rollback and revoke routes;
- a proposal-only `devpilot_propose_changeset` nanobot adapter;
- a React approval prototype that displays Diff and requires separate apply/rollback actions.

The proposal adapter strips approval tokens and never calls mutating routes. The UI keeps the
operator token in browser memory only, sends it in the Authorization header, creates fresh
Idempotency-Key values and visibly warns that no OS Sandbox is connected.

P2B-03 is accepted by API contract, non-loopback rejection, plugin-discovery and frontend build
checks. The control-plane CLI remains explicitly opt-in and requires an Alembic head.
