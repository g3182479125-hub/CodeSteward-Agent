# P4-01 独立产品/质量 Review（rerun）

## Decision

`blocked`

P4-01 不允许进入 P4-02。

## Review scope and identity

- Repository: `D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\remote-integrate\devpilot-agent`
- Target commit: `068ac07dc36dbae726bddce8dff810b57c0e78fc`
- Frozen plan: `D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\outputs\DevPilot-AI-Handoff-Portable-2026-08-31-v2\plans\DEVPILOT_FROZEN_PLAN.md`
- Frozen plan: version `2.1.0`, 690 lines, SHA-256 `4f06504ad3273781f7c4cff2d919aec432ea0385014c8c94c42455709efbdec9`
- Review mode: read-only; no application code, tests, configuration, migration or Skill was changed.
- Current worktree has the two pre-existing user changes in `tests/fixtures/node-project/src/index.js` and `tests/fixtures/node-project/test/index.test.js`; these are not attributed to 068ac07. The untracked phase-4 manifest records them clearly, but is not part of 068ac07.

## Current conclusion

Positive evidence exists for the new Workflow’s normal Fake path and for ChangeSet-to-Run persistence. The decision remains blocked because the product/runtime boundary is not fail-closed before P6, the Skill/Workflow contract is not executable end-to-end, failure states are incomplete, and the target commit has no committed phase-4 manifest tied to a clean reproducible tree.

## Findings

### P1 — Blocker: real execution is not globally fail-closed before P6

`FeatureDeliveryWorkflow.verify()` rejects non-`FakeExecutionBackend` implementations, which is a correct narrow guard. However, the product control-plane path remains open: `create_app()` enables the FC backend when `DEVPILOT_ENABLE_FC_SANDBOX=1` (`src/devpilot/control_plane.py:668-690`), and `ControlPlaneManager.verify()` runs any configured `ExecutionBackend` without a server-attested P6 capability/gate record (`src/devpilot/control_plane.py:262-360`). The registered `devpilot_verify_changeset` Tool can reach that path with an externally supplied `r2.verify` token.

This does not satisfy the frozen requirement that real execution/autonomous repair remain fail-closed until P6. Add a server-owned, immutable P6 capability record and reject all non-Fake backends until it exists, or explicitly quarantine the real backend path from P4.

### P1 — Blocker: P4-01 Skill/Workflow is not an executable product path

`skills/catalog/feature-delivery/SKILL.md` is intentionally still `draft`/`reviewed: false`, and exposes only inspect/search/read/propose. It does not expose the frozen plan/request-approval/state/diff contract; there is also no registered `devpilot_request_approval`, `devpilot_get_run_state`, or `devpilot_get_diff` Tool. The `FeatureDeliveryWorkflow` is referenced by its test and implementation, not wired into the nanobot entry-point path.

The runtime Tools require an externally issued approval token, but the current target has no end-to-end Skill-visible handoff for obtaining that token. Existing README/runbook text describes chat confirmation/internal approval, which conflicts with the target implementation and frozen independent-approval boundary. Resolve the contract and add an executable integration test before P4-02.

### P1 — Blocker: G-01 evidence is weaker than the frozen Golden

`tests/test_p4_feature_delivery.py:34-113` now uses FastAPI source text and proves a normal Fake success path, but it changes the response of an existing `/health` route rather than adding a restricted interface. The test invokes `manager.approve()` directly, does not prove the control-plane/API user approval handoff, and does not assert persisted Preview/Diff immutability, plan/check/stop-condition binding, or user-selected rollback. It is a useful component test, not sufficient evidence for the frozen G-01 full chain.

Add a real bounded FastAPI fixture Golden test with actual inspect/search/read evidence, exact plan/scope/check binding, external `r1.apply` and `r2.verify` approvals, immutable Preview/Diff, Fake receipt, server-owned Artifact, no pre-Apply write, failure and explicit rollback paths.

### P1 — Blocker: phase-4 manifest is not part of the reviewed commit

The current untracked `docs/evidence/phase-4/manifest.json` clearly records the two known external fixture modifications and points to `068ac07`, which is a positive fact. It cannot serve as target-commit evidence because it is untracked. It also omits the frozen manifest fields for timestamps, migration version, dependency-lock hash, artifact hashes, secret/license/Markdown checks, and clean-tree status; it uses only the short commit id.

Add and commit a complete phase-4 manifest tied to the exact full commit, with all command exit codes and configuration/Artifact/migration/lock evidence. The target tree must be reproducible and its known external changes must remain explicitly accounted for outside the implementation commit.

### P2 — Must fix: failure terminal states are incomplete

The successful path now leaves the Run open through Apply and finishes it during Verify, and the nonzero Fake test records a failed Run plus failure Artifact (`tests/test_p4_feature_delivery.py:138-166`). The negative paths are incomplete:

- P6 blocking in `FeatureDeliveryWorkflow.verify()` raises before consuming the token or recording a denied/unavailable terminal event, leaving the applied Run open.
- `build_verification_request()` failures leave the ChangeSet applied and Run open; unexpected backend exceptions are not converted to a failed/unavailable state.
- If Artifact writing or Artifact DB recording fails after `ControlPlaneManager.verify()` finishes the Run, the Run can say `completed` while delivery evidence is missing.

Define and test deterministic failed/unavailable terminal behavior, preserve failure evidence, and require a separate user-selected rollback path.

### P2 — Must fix: evidence binding and Artifact/log ownership are only partial

`FeatureDeliveryWorkflow.propose()` checks that each evidence id belongs to the target project, but does not prove that it is an inspect/search/read evidence record or bind the evidence content/hash to the proposal. `ArtifactService` redacts known credential patterns and the Workflow checks the requested Artifact root against the manager root and target project; `ControlPlaneManager.verify()` also redacts bounded stdout/stderr before its event/response. These are positive controls.

The boundary is not centralized: `ProjectStore.append_event()`/`finish_run()` serialize caller payloads without redaction, `ArtifactService` itself accepts any root when directly constructed, and `create_app()` does not validate that configured data/Artifact roots cannot overlap an allowed project. Add server-side root validation, centralized event/result redaction, and evidence provenance/hash tests.

## Passed checks / evidence inspected

- `src/devpilot/workflows.py`, `control_plane.py`, `storage.py`, `artifacts.py`, `redaction.py`, `nanobot_tools.py`, `verification.py`, `execution.py`.
- `skills/catalog/feature-delivery/SKILL.md` and `pyproject.toml` entry points.
- `migrations/versions/0005_p4_run_link.py`; the isolated fresh-database `alembic upgrade head` reached `0005_p4_run_link (head)`.
- `tests/test_p4_feature_delivery.py` and relevant P2B durable control-plane tests.
- `uv run pytest -q tests/test_p4_feature_delivery.py tests/test_p2a_migration.py tests/test_p2b_control_plane.py tests/test_p2b_durable_control_plane.py` — passed.
- `uv run pytest -q -ra` — exit 0; three Windows symlink tests skipped because the current user lacks symlink privilege.
- `uv run ruff check src scripts tests` — passed.
- `uv run pyright` — 0 errors, 0 warnings, 0 informations.
- `uv run python scripts/secret_scan.py` — passed.
- `uv run python scripts/check_markdown.py` — passed.
- `uv lock --check` — passed.
- `git diff --check` — passed.

## Positive compatibility result

The P4 migration adds `change_sets.run_id`, `PatchPlan` persists it, `_load_entry()` restores it, and the dedicated restart test passes. The existing P2B Preview equality contract remains covered. This item does not block the review.

## Gate decision

`blocked`. P4-02 is not authorized. A new independent Review is required after the P1 blockers are closed, the failure/ownership gaps are tested, and a complete phase-4 manifest is committed for the exact reviewed tree.
