# P4-04 Delivery Decision Acceptance Evidence

Implementation commit: `01c8e4804ceeccc2461f3e14c0aad9fa123448b7`
Review target: `01c8e4804ceeccc2461f3e14c0aad9fa123448b7`
Plan: frozen DevPilot plan `2.0.0`
Date: 2026-09-03 (Asia/Shanghai)

## Delivered outcome

P4-04 adds one server-owned delivery report and one explicit final-decision
workflow over the existing `feature-delivery`, `bug-fix`, and `test-repair`
ChangeSets. The report is compact and redacted. Acceptance and stop require an
exact user confirmation and an idempotency key. Acceptance only changes the
ChangeSet to `accepted` and never writes project files. Stop records the reason,
does not claim success, and never rolls files back. Rollback remains the
existing R1 capability-gated control-plane operation.

The public surfaces are:

- `GET /v1/change-sets/{change_set_id}/delivery-report`;
- `POST /v1/change-sets/{change_set_id}/decision` for `accept` or `stop`;
- `devpilot_get_delivery_report`, a read-only nanobot Tool;
- `DeliveryDecisionWorkflow.rollback()`, which returns instructions only and
  does not mint or consume a rollback capability.

## P4 acceptance matrix

- success report includes status, file/hash manifests, verification metadata,
  Artifact metadata, and available decisions;
- failed verification preserves `failure_reason` through acceptance;
- stop preserves the failed status and records `success_claimed=false`;
- repeated accept/stop is rejected as a final decision, while the same
  idempotency key replays the original response;
- invalid decision and mismatched/empty confirmation return stable errors;
- report and nanobot adapter exclude stdout/stderr, tokens, and environment
  values;
- rollback instruction exposes `r1.rollback` and its existing endpoint but
  cannot perform rollback;
- existing P4-01/P4-02/P4-03 suites remain in the full regression run.

## Explicit boundary

P4-04 is Fake-only. It does not add real Python/Node/Shell execution, FC/E2B
connectivity, automatic repair, automatic approval, or automatic rollback.
Those remain outside P4 and gated by P6 or later work.

## Gate decision

P4-04 implementation and local quality checks pass in the C-drive working copy.
The phase evidence manifest, verification transcript, and two-axis subagent
review are recorded alongside this acceptance file. The E-drive source checkout
is not modified by this session because it is outside the writable workspace.
