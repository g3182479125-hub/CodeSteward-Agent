# P4-01 feature-delivery acceptance

Date: 2026-09-02
Node: P4-01
Status: implementation complete; independent Review rerun required
Implementation commit: `89f49e258a3def9adfe95cc6a658fb7186c269fe`

## Delivered boundary

P4-01 adds a deterministic `FeatureDeliveryWorkflow` on top of the existing P2B control
plane. The workflow accepts bounded FilePatches plus previously collected inspection/read
evidence, creates one immutable Preview, waits for an exact user confirmation, applies the
same ChangeSet, runs one server-allowlisted Verification profile, and writes a bounded
  `feature-delivery.md` Artifact. The complete P4 contract (evidence hash, check IDs and stop
  conditions) is persisted on ChangePlan and bound into each Approval. The caller-facing preview
  is recursively immutable; server restart can recover the same contract.

The new catalog Skill remains `draft` with `reviewed: false` until the independent Review
subagent approves this node. It can therefore be inspected and tested, but cannot be published
by the Skill lifecycle gate yet. This metadata is deliberately not advanced by the developer.

## Safety boundary

- No arbitrary command, shell operator, dependency installation or project discovery is added.
- Missing or non-exact user confirmation cannot reach approval or Apply.
- Before P6, the automated workflow rejects non-Fake execution backends with
  `AUTONOMOUS_REPAIR_BLOCKED`.
- The pre-P6 gate accepts only the exact `FakeExecutionBackend` class; subclasses and FC-looking
  backends are rejected before `run()` can be called.
- Approval tokens remain internal to the workflow and are not returned in the result or Artifact.
- The Artifact stores hashes, file metadata and verification receipt metadata; it does not copy
  stdout/stderr or the complete environment.

## G-01 evidence

`tests/test_p4_feature_delivery.py::test_feature_delivery_g01_completes_approved_fake_path_and_artifact`
is the single machine-readable Golden chain and proves:

`FastAPI restricted endpoint evidence (inspect/search/read) -> Preview -> external r1.apply approval + exact user confirmation -> Apply -> external r2.verify approval -> Fake Verification -> server-owned Artifact -> external r1.rollback approval -> Rollback`

The same test asserts evidence event provenance and the read SHA-256, evidence/plan/check/stop
condition binding, immutable Preview/Diff, no write before Apply, both external capability
handoffs, the Fake receipt, server-owned Artifact persistence, explicit rollback to the exact
baseline, non-zero verification with a failure Run/Artifact, and Artifact database failure with
a failure Artifact and no false `run.completed` event. The test also proves a terminal Run cannot
be rewritten as completed. Separate tests cover restart recovery, exact confirmation, and the
pre-P6 non-Fake fail-closed boundary.

The test emitted the machine-readable runtime record committed as
`docs/evidence/phase-4/g01-runtime-evidence.json`. It contains the actual success/failure
Artifact hashes, event sequences, external capability handoffs, ChangeSet delivery-failed state,
and both rollback hashes matching the baseline.

The fixture is a bounded FastAPI source project: the allowed change updates `/health` and adds
the `/internal/status` restricted endpoint. The project is never executed by P4; Fake
verification only validates the controlled ExecutionBackend contract.

## Adapter and lifecycle evidence

The nanobot `devpilot_feature_delivery_propose` adapter has direct tests for proposal-only
behavior, request mapping, strict nested patch schema, and stable allowlisted HTTP error-code
mapping without returning an untrusted response body. The read-only `devpilot_get_changeset` and
`devpilot_get_run_state` adapters expose the persisted state/Diff contract. The Skill remains
unpublished until the independent Review gate passes; the registered Tool list and the Skill's
P4 proposal-only contract are intentionally aligned.

Additional tests prove that an inexact confirmation does not write and that a real-looking
remote backend is blocked before the P6 Sandbox Gate.

## Verification commands

```text
uv run pytest -q tests/test_p4_feature_delivery.py tests/test_p2a_migration.py tests/test_p2b_control_plane.py tests/test_p2b_durable_control_plane.py
uv run ruff check src scripts tests
uv run pyright
```

The full repository regression and the phase evidence manifest must be regenerated after the
final implementation commit and before the P4-01 independent Review rerun. This document does
not itself authorize P4-02.
