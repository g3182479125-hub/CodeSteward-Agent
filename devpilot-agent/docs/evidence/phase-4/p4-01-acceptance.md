# P4-01 feature-delivery acceptance

Date: 2026-09-02
Node: P4-01
Status: implementation complete; independent Review pending

## Delivered boundary

P4-01 adds a deterministic `FeatureDeliveryWorkflow` on top of the existing P2B control
plane. The workflow accepts bounded FilePatches plus previously collected inspection/read
evidence, creates one immutable Preview, waits for an exact user confirmation, applies the
same ChangeSet, runs one server-allowlisted Verification profile, and writes a bounded
`feature-delivery.md` Artifact.

The new catalog Skill is intentionally `draft` with `reviewed: false` until the independent
Review subagent completes. It can therefore be inspected and tested, but cannot be published
by the Skill lifecycle gate yet.

## Safety boundary

- No arbitrary command, shell operator, dependency installation or project discovery is added.
- Missing or non-exact user confirmation cannot reach approval or Apply.
- Before P6, the automated workflow rejects non-Fake execution backends with
  `AUTONOMOUS_REPAIR_BLOCKED`.
- Approval tokens remain internal to the workflow and are not returned in the result or Artifact.
- The Artifact stores hashes, file metadata and verification receipt metadata; it does not copy
  stdout/stderr or the complete environment.

## G-01 evidence

`tests/test_p4_feature_delivery.py::test_feature_delivery_g01_completes_approved_fake_path_and_artifact`
passes and proves:

`Preview -> exact user confirmation -> Apply -> Fake Verification -> feature-delivery Artifact`

The same test asserts that Preview does not write the project, the final file is present only
after Apply, the verification receipt is `verified`, the Artifact is persisted, and the Fake
output is not copied into the Artifact.

Additional tests prove that an inexact confirmation does not write and that a real-looking
remote backend is blocked before the P6 Sandbox Gate.

## Verification commands

```text
uv run pytest -q tests/test_p4_feature_delivery.py
uv run ruff check src scripts tests
uv run pyright
```

The full repository regression remains required before the P4-01 node can be submitted for
independent Review.
