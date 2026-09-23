# ShopSphere Personal-Agent Bug-Fix Challenge Design

## Status

- Date: 2026-09-23
- Status: awaiting user review
- Base repository: <https://github.com/yongjunmun/E-Commerce-Website>
- Pinned upstream baseline: `1bdb758791b2b28a8a2548a3a18f9e034330e25f`
- License: MIT; retain the upstream license and attribution.

## 1. Goal

Prepare a realistic, moderately sized business application for evaluating the user's own DevPilot personal agent. Preserve an upstream baseline, create a separate challenge state with one intentionally introduced business defect, then use the personal agent's normal project-analysis, test, ChangeSet, approval, Apply, and Verify flow to repair that challenge. The challenge must be reproducible and must not alter the upstream repository or DevPilot implementation.

## 2. Project choice

ShopSphere is a Flask + SQLite marketplace with server-rendered pages and two runtime dependencies (Flask and Pillow), with no Node build step. Its modules cover catalog, cart, checkout, stock and variants, order fulfilment, seller commission, reporting, and security. The upstream README reports 222 standard-library `unittest` tests, runnable with `python -m unittest discover -s tests -t .`.

This scope is large enough to exercise repository analysis and cross-cutting business invariants, while avoiding a cloud database, payment gateway, frontend build chain, or live email service.

## 3. Workspace and provenance

- Use an isolated workspace outside the DevPilot source repository, under `E:\codex-local\agent-demo-projects\shopsphere`.
- Fetch the pinned upstream baseline and retain its commit and MIT license for provenance.
- Keep an immutable `upstream-baseline` ref and create a separate `agent-challenge` branch from that exact commit.
- Never modify upstream branches, push to the author's repository, or mix challenge files into the DevPilot implementation repository.
- Point the personal agent and its project allowlist only at the challenge worktree. Keep SQLite data, uploads, logs, and test temporary files inside a disposable local state directory.

## 4. Seeded defect

Introduce a reproducible concurrency defect in order cancellation: two administrator requests that cancel the same eligible order at the same time can both observe the pre-cancel state and return the same sold quantities to inventory. The resulting stock is overstated; variant stock roll-ups and stock-dependent catalog/report views must remain consistent as well.

The challenge must include a deterministic regression test that coordinates the concurrent requests at the state-read boundary, then asserts that cancellation has a single effective inventory effect. The test should expose the business symptom without naming the source function or prescribing compare-and-set/transaction implementation. No unrelated defect is to be introduced.

If the pinned implementation and SQLite transaction behavior do not allow this race to be reproduced reliably with a bounded test, do not ship a flaky seeded defect. Reassess the scenario with the user before substituting a different defect.

## 5. Personal-agent repair flow

1. Run the upstream suite and record its result before seeding.
2. Add the challenge-only regression and intentional defect; verify that the new test fails repeatably while the pre-existing suite remains green.
3. Start the user's existing personal-agent stack with the challenge project as the only FC-allowlisted project for this run. Do not broaden persistent roots or reuse production data.
4. In natural language, ask the agent to investigate intermittent inventory inflation after administrators cancel orders, reproduce it, and fix it without changing other order or reporting behavior.
5. Let the personal agent inspect, run tests, prepare a ChangeSet, show the diff, and verify the repair through its normal workflow. Apply only the ChangeSet scoped to the challenge project.
6. Re-run the deterministic regression repeatedly and the complete test suite; check database stock, order state, variant aggregate stock, and cancelled-order report exclusion.
7. Capture run/change-set/test evidence and clean only disposable runtime data. Do not commit or push the repaired challenge unless separately requested.

## 6. Acceptance criteria

- Upstream baseline is pinned and its full test suite passes before mutation.
- The introduced concurrency regression fails deterministically on the challenge branch and passes after the personal agent's repair.
- All pre-existing tests pass both before and after the repair (apart from the intentional regression being red before repair).
- Concurrent duplicate cancellation causes at most one inventory restoration; order state is terminal and report totals exclude the cancelled order.
- The personal agent produces an auditable ChangeSet and test-backed Verify result; no direct manual fix is substituted for the agent's repair.
- The upstream baseline, DevPilot source, credentials, live payment/email integrations, and unrelated user data remain untouched.
- No unrequested commit, remote push, fork, deployment, or publication occurs.

## 7. Risks and boundaries

- Concurrency behavior depends on the app's SQLite connection and transaction configuration; the regression must use synchronization rather than timing sleeps and must be proven repeatable before demo use.
- A challenge directory is deliberately defective until repaired. Label it clearly and never use it as a live shop or with real customer data.
- If the agent's configured FC path uploads source to a cloud provider, use only this public, synthetic challenge project and keep the allowlist scoped to its exact path for the test process.
- The demo accounts and seed data are fixtures, not production credentials or data.
