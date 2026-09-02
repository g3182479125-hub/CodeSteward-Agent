# P4-01 feature-delivery acceptance

Date: 2026-09-02
Node: P4-01
Status: implementation complete; ready for independent Review rerun
Implementation commit: `b4bb6ac` (`fix: require durable trust for control-plane execution`)

## Delivered boundary

P4-01 adds a deterministic `FeatureDeliveryWorkflow` on top of the existing P2B control
plane. The workflow accepts bounded FilePatches plus previously collected inspection/read
evidence, creates one immutable Preview, waits for an exact user confirmation, applies the
same ChangeSet, runs one server-allowlisted Verification profile, and writes a bounded
  `feature-delivery.md` Artifact. The complete P4 contract (evidence hash, check IDs and stop
  conditions) is persisted on ChangePlan and bound into each Approval. The caller-facing preview
  is recursively immutable; server restart can recover the same contract.

The catalog Skill is now `enabled` with `reviewed: true`; its registered Tool set is contract-tested.
R1/R2 token-bearing execution classes remain source-compatible for legacy unit tests but are not
nanobot entry points. The only registered approval action records a user-confirmed intent and
returns no capability token; the control-plane operator still performs the actual approval.

Persistent projects start as `untrusted`. A separate control-plane trust action binds the current
repository fingerprint and allowed-root id. Apply, Verify, Rollback and approval requests reject
untrusted projects or any binding change.

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
mapping without returning an untrusted response body. `devpilot_request_approval` records only a
pending intent and never accepts or returns a capability token. The read-only
`devpilot_get_changeset` and `devpilot_get_run_state` adapters expose the persisted state/Diff
contract. The complete registered Tool set and Skill allowlist are checked for exact equality.

Additional tests prove that an inexact confirmation does not write and that a real-looking
remote backend is blocked before the P6 Sandbox Gate.

## Verification commands

```text
uv --no-cache run pytest --tb=no -r a -o addopts='' --basetemp E:/codex-local/pytest-temp/review-basetemp -p no:cacheprovider
uv --no-cache run ruff check --no-cache src scripts tests
uv run pyright
uv --no-cache lock --check
uv run python scripts/secret_scan.py
uv run python scripts/check_markdown.py
uv run alembic upgrade head
git diff --check
```

Recorded result: `123 passed, 3 skipped`; Ruff, Pyright, lock, Secret, Markdown, migration and
diff checks passed. The three skips are Windows symbolic-link privilege checks. P4-02 remains
subject to the independent read-only Review rerun; this acceptance file does not self-authorize it.
