# P4-01 Final Independent Review

## Decision

`blocked`

P4-01 不满足冻结计划 v2.1.0 的退出条件，不能放行 P4-02。单一 Golden 和 evidence manifest 问题均属于 release blocker，不是可带条件放行的 follow-up。

## Review scope

- Repository: `D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\remote-integrate\devpilot-agent`
- Reviewed HEAD: `3667a5dbdd34d54adb342084c9ce4033b06a8bc2`
- Frozen plan: `DEVPILOT_FROZEN_PLAN.md` v2.1.0
- Frozen plan SHA-256: `4f06504ad3273781f7c4cff2d919aec432ea0385014c8c94c42455709efbdec9`
- Review mode: read-only except this report

## Evidence inspected

- `docs/evidence/phase-4/p4-01-acceptance.md`
- `docs/evidence/phase-4/manifest.json`
- Existing P4-01 final/rerun/closure reviews
- `src/devpilot/control_plane.py`, `workflows.py`, `artifacts.py`, `storage.py`, `nanobot_tools.py`
- `tests/test_p4_feature_delivery.py` and relevant control-plane/adapter tests
- `skills/catalog/feature-delivery/SKILL.md`, `pyproject.toml`
- Current Git status and HEAD metadata

## Findings

### R-01 — Release blocker: phase-4 evidence is not bound to the requested HEAD

`docs/evidence/phase-4/manifest.json` records `commit` and `implementation_commit` as `5df563638f47a2deb0829335848cde58ee22de83`, while this review target is `3667a5dbdd34d54adb342084c9ce4033b06a8bc2`. The manifest therefore cannot prove the state being reviewed. It also reports `implementation_complete_review_pending` and `pending_rerun`, so it is not a final gate artifact.

The worktree has two uncommitted, user-owned fixture edits:

- `tests/fixtures/node-project/src/index.js`
- `tests/fixtures/node-project/test/index.test.js`

They are explained in the manifest, but the frozen public Gate still requires a reproducible reviewed tree with no unaccounted modification. A final manifest must be regenerated for the reviewed commit and the fixture state must be explicitly isolated or the tree made clean by its owner.

### R-02 — Release blocker: G-01 is not one complete frozen Golden chain

The current evidence describes a successful Fake path and separately describes HTTP approval/rollback tests, but the principal G-01 does not prove the frozen scenario as one machine-readable chain:

- the fixture is source text containing a standalone `health()` function, not a bounded FastAPI restricted-interface addition;
- proposal evidence is represented by an inspect/run identifier without proving concrete inspect/search/read record provenance, content hashes, and binding to the plan;
- the success path uses direct `manager.approve()` rather than proving both external control-plane user approval handoffs (`r1.apply` and `r2.verify`) in the same Golden;
- immutable Preview/Diff hash, exact plan/scope/check/stop-condition binding, and no-write-before-Apply are not all asserted in the same chain;
- Fake verification, server-owned Artifact, failure Artifact, and explicit user-selected rollback are split across separate tests/evidence rather than constituting the required single G-01.

The frozen plan expressly says weaker tests cannot replace the numbered Golden. This is therefore a release blocker.

### R-03 — Release blocker: failure and Artifact-DB failure evidence is incomplete

There is evidence for a non-zero Fake verification producing a failed Run/failure Artifact, but no current manifest-backed, deterministic test evidence covering an Artifact write/database failure and proving the required terminal-state behavior. In particular, the review cannot establish from the submitted evidence that an Artifact DB failure cannot leave a completed Run without its required delivery evidence, or that a terminal Run cannot be rewritten by a later failure path.

The frozen plan requires failure to remain auditable, stable, and non-successful; an Artifact persistence failure must be represented as a deterministic failure outcome, with coherent ChangeSet/Run/Event state and no false completion. Until this path is tested and recorded against the reviewed commit, it remains a release blocker.

### R-04 — Release blocker: feature-delivery Skill is not yet publishable and its declared contract is not fully directly tested

The `feature-delivery` catalog Skill remains `status: draft` with `reviewed: false`, so its lifecycle has not reached reviewed/enabled/published/golden-passed/active. That is consistent with the acceptance note that Review is pending, but it also means P4-01's required Skill delivery is not complete.

The HTTP proposal route exists at `/v1/feature-delivery/proposals`, and the nanobot proposal adapter is present/registered. However, the evidence does not provide a direct adapter test for its request/response mapping, stable error mapping, and proposal-only behavior. The declared read-only state/Diff tool contract also must be reconciled with the actually registered tools and tested directly. Registration alone is not evidence that the Skill contract is executable.

## ControlPlaneManager / pre-P6 gate result

This review found a positive boundary in the current material: the pre-P6 workflow rejects non-Fake execution before verification, and the FC environment flag is not itself accepted as authorization. The current tree no longer has evidence of a separate callable P6 attestation object in the reviewed P4 path, so the previously reported forgeable-gate concern is not carried forward as a separate finding here.

That positive result does not authorize P4-01: the phase manifest, single G-01 Golden, failure/Artifact-DB evidence, and Skill lifecycle/direct adapter tests remain blocking.

## Required unblock evidence

1. Regenerate `docs/evidence/phase-4/manifest.json` for HEAD `3667a5dbdd34d54adb342084c9ce4033b06a8bc2` (or a new reviewed commit), with the exact test exit codes, configuration summary, migration head, lock hash, artifact hashes, commit, and an explicitly resolved worktree state.
2. Add one reproducible G-01 test using a bounded FastAPI restricted endpoint. It must record and validate inspect/search/read provenance and hashes; external approval for both capabilities; exact plan/scope/check/stop-condition binding; immutable Preview/Diff; no pre-Apply write; Fake receipt; server-owned Artifact; failure handling; and user-selected rollback.
3. Add deterministic Artifact filesystem/DB failure tests proving Run, ChangeSet, verification, events, and failure Artifact states cannot falsely report successful delivery.
4. Directly test the feature-delivery nanobot adapter and reconcile/register/test every Skill-declared tool. Advance Skill lifecycle metadata only after the independent review gate and Golden pass.
5. Re-run the full phase quality Gate and submit a new independent Review against the regenerated manifest.

## Final gate outcome

`blocked` — P4-01 is not release-ready and P4-02 must not start.
