# DevPilot P4-01 独立产品/质量 Review

## Decision

`blocked`

P4-01 当前不能放行，也不能进入 P4-02。必须先补齐当前 commit 的可复现回归证据，并关闭下方的审批、P6 执行门禁、Golden 测试和 Artifact/重启契约问题，然后由独立 Review 重新检查。

本报告只评审目标 commit `97def67e3b6571a9db3a79d6b2f175a6fdba9bcc`，不采纳实现者在 acceptance、handoff 或既有 Review 中的结论作为事实；所有判断均来自目标树中的文件、Git 元数据和静态代码路径。

## Review identity and frozen-plan record

- Repository: `D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\remote-integrate`
- Target commit: `97def67e3b6571a9db3a79d6b2f175a6fdba9bcc` (`feat: add feature delivery workflow`)
- Frozen plan: `D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\outputs\DevPilot-AI-Handoff-Portable-2026-08-31-v2\plans\DEVPILOT_FROZEN_PLAN.md`
- Frozen plan version: `2.1.0`（权威冻结版；本次修订日期 2026-09-02）
- Frozen plan size: 56,441 bytes; 690 lines
- Frozen plan SHA-256: `4F06504AD3273781F7C4CFF2D919AEC432EA0385014C8C94C42455709EFBDEC9`
- Current checkout status during review: two pre-existing, uncommitted modifications were present in `devpilot-agent/tests/fixtures/node-project/src/index.js` and `devpilot-agent/tests/fixtures/node-project/test/index.test.js`; neither belongs to the target commit and neither was modified by this review.
- Review mode: read-only. No project code, tests, configuration, migration, Skill or existing document was modified, and no project code was executed.

## Evidence inspected

- Frozen plan, including P4-01/P4-02 dependencies, Section 3 approval rules, Section 4 workflow/verification rules, Section 5.4 P6 fail-closed gate, Section 7 Skill lifecycle, Section 9 Golden/Gate rules and Section 10 prohibitions.
- Target commit metadata, changed-path list and tree. The commit changes six paths: P4-01 acceptance evidence, `feature-delivery` Skill, `workflows.py`, `test_p4_feature_delivery.py`, `artifacts.py` and `control_plane.py`.
- `devpilot-agent/docs/evidence/phase-4/p4-01-acceptance.md`.
- `devpilot-agent/skills/catalog/feature-delivery/SKILL.md`.
- `devpilot-agent/src/devpilot/workflows.py`, `artifacts.py`, `control_plane.py`, `execution.py`, `verification.py`, `storage.py`, `nanobot_tools.py`, `fc_sandbox.py` and `skill_manager.py`.
- `devpilot-agent/tests/test_p4_feature_delivery.py`, the P2B durable preview/restart and approval tests, and the P3 execution-boundary tests.
- `devpilot-agent/pyproject.toml`, `config/*.example.json`, `scripts/secret_scan.py`, P2A/P2B manifests, P2B/P3 acceptance documents and prior review/handoff documents, as historical context only.
- No `devpilot-agent/docs/evidence/phase-4/manifest.json`, full-regression log, test report, or target-commit quality-gate result exists in the target tree.

The P4-01 acceptance file itself says that full repository regression is still required. Its three listed commands are not accompanied by exit codes or machine-readable results. I therefore do not claim that pytest, Ruff, Pyright, Secret Scan, Markdown/link checks, migration checks, frontend checks, or Windows checks passed at this commit.

## Acceptance summary

| Area | Result | Evidence-based assessment |
|---|---|---|
| Feature Skill exists | Partial | Catalog file exists, but it is explicitly `status: draft` and `reviewed: false`; it is not publishable/active. |
| FeatureDeliveryWorkflow exists | Partial | The class provides Preview → confirmation → Apply → Verify → Artifact in an isolated test path, but it is not wired to a production/nanobot entry point. |
| G-01 | Insufficient | Four tests cover Fake success, non-exact confirmation, one non-Fake rejection and draft Skill validation; they do not prove the frozen FastAPI/approval/evidence Golden contract. |
| Pre-P6 real automatic repair | Fail-closed in one default path, not globally | The new workflow rejects non-Fake backends by default, but a caller-controlled boolean bypasses that check, and the existing nanobot/FC path remains executable before the frozen P6 gate. |
| Approval boundary | Fail | The workflow and existing `devpilot_execute_changeset` mint approvals internally after a plain chat string instead of requiring a pre-existing user-issued control-plane grant. |
| Artifact stdout/stderr/token check | Normal generated report is clean, boundary incomplete | `_render_report` omits stdout/stderr and returned workflow data has no approval token, but Artifact writes are not secret-scanned/root-validated and RunEvent/result paths persist/return raw bounded output. |
| P2B Preview restart | Existing Preview equality preserved; new run linkage regresses | `second.get(change_set_id) == preview` remains structurally compatible, but `_load_entry()` drops `run_id`, so the new workflow cannot recover its Artifact run link after restart. |
| Full regression and Gate | Blocked | No phase-4 manifest or target-commit results; checkout also has unexplained fixture modifications. |

## Findings

### F-01 — Blocker: current-commit regression and evidence Gate are missing

The only phase-4 evidence is `p4-01-acceptance.md`. Lines 43–49 list commands, while lines 51–52 explicitly state that full repository regression remains required. There is no phase-4 `manifest.json`; the existing P2A/P2B manifests point to older commits (`1301964` and `e49654b`) and therefore cannot establish the state of `97def67`.

This prevents verification of the frozen public Gate: full tests and static checks, secret/license/Markdown checks, migration checks, Windows behavior, rollback evidence, a clean worktree, and an evidence manifest tied to the reviewed commit. The current checkout additionally reports two uncommitted Node fixture changes, which cannot be silently treated as part of the target.

Recommendation: run the complete required quality matrix from a clean checkout of `97def67e3b6571a9db3a79d6b2f175a6fdba9bcc`; preserve the user-owned fixture changes outside that checkout or document them explicitly; add a phase-4 manifest containing commands, exit codes, timestamps, configuration summary without secrets, migration/lock hashes, artifact hashes and the exact commit. Re-run independent Review after the manifest and all failures are resolved.

### F-02 — Blocker: the workflow bypasses the frozen approval boundary

`FeatureDeliveryWorkflow.execute()` accepts only the strings in `_CONFIRMATIONS` (lines 145–149), then calls `self.manager.approve()` for Apply (line 164) and `self.manager.approve(..., VERIFY_CAPABILITY)` for Verify (line 170) before consuming the newly minted tokens (lines 165–175). `ControlPlaneManager.approve()` itself creates a random token and an approval record (lines 206–254); it does not validate that an independent user approval already exists.

That is materially different from the frozen plan: `approved` must be produced by an explicit control-plane UI/API user action, the capability must be bound to the exact plan/patch/scope/check set, and model/Skill execution must not trigger the control-plane action. A plain `user_confirmation` argument is not an approval capability and is not cryptographically or durably bound to the exact proposal. The Skill also omits `devpilot_request_approval` from `allowed_tools`, while the frozen tool contract includes it.

The repository-wide path has the same issue: `nanobot_tools.py` `_execute_changeset()` calls `/approve` for `r1.apply` and `r2.verify`, then `/apply` and `/verify` (lines 467–487); `ExecuteChangeSetTool` exposes this after a chat confirmation. The target README and handoff explicitly describe chat confirmation as replacing the approval page. Those claims cannot override the current frozen plan.

Recommendation: make the feature Skill/Workflow proposal-only until a control-plane user action creates a grant. The Apply/Verify entry point should accept only an opaque, already-issued, exact-scope grant or an approval record reference whose one-time consumption is enforced server-side; it must not call `approve()` or mint tokens itself. Remove or quarantine the legacy nanobot execution path unless it is brought under the same frozen contract. If chat confirmation is intended to replace control-plane approval, stop and obtain a formal plan/ADR decision before changing the frozen boundary; it must not be a silent implementation choice.

### F-03 — Blocker: P6 execution gate is caller-controlled and real FC execution remains open before P6

The new workflow constructor accepts `sandbox_gate_passed: bool` (lines 92–101). The only protection is `if not self.sandbox_gate_passed and not isinstance(backend, FakeExecutionBackend)` (lines 150–157). Passing `True` allows any object implementing the backend protocol, including an unverified or network-capable backend. The flag is not derived from a server-side verified capability record, P6 evidence, backend version, capability hash or verification-suite version. The sole new negative test exercises only the default `False` case (lines 92–111 of `test_p4_feature_delivery.py`).

Independently, `create_app()` registers the FC backend when `DEVPILOT_ENABLE_FC_SANDBOX=1` (control_plane.py lines 653–673), and the existing `devpilot_execute_changeset` path can then perform real Apply plus FC Verify after chat confirmation (nanobot_tools.py lines 467–487). The target P3 acceptance/handoff calls this path live, but the frozen plan says P6 is the only gate for a real Sandbox-backed automatic-repair verification claim. The existing P3 FC review is not a P6 security/real-fixture acceptance and cannot substitute for it.

Recommendation: remove the boolean override. Require a server-owned, fail-closed P6 gate record containing verified backend identity/version, capability hash, suite version, platform and expiry; reject every backend other than the exact verified adapter until that record exists. Add tests proving that `sandbox_gate_passed=True` cannot authorize a rogue backend, that the FC environment flag cannot expose a pre-P6 autonomous path, and that missing/expired/changed capability evidence returns `EXECUTION_BACKEND_UNAVAILABLE` without Apply or process start.

### F-04 — Blocker: G-01 does not prove the frozen Golden scenario

The frozen G-01 requires a “FastAPI 新增受限接口” chain with plan, approval, immutable patch, Diff, Fake verification and Artifact. The new test creates a file containing a standalone `health()` function (lines 30–35), not a FastAPI fixture or restricted API; it supplies arbitrary strings such as `inspection-run-1` as evidence IDs (lines 44–48) without verifying that they came from inspect/search/read or belong to the project; it does not retrieve/assert the persisted Diff, check plan fields, stop conditions or an independent approval action. It also has no failure/rollback/artifact-failure path.

Static search found no production call site for `FeatureDeliveryWorkflow`; the only references are the new test and the class itself. The catalog Skill is therefore not an active product path, while the actual nanobot path is the legacy approval/FC path described in F-02/F-03.

Recommendation: implement G-01 against a real bounded FastAPI fixture and assert the entire machine-readable chain, including actual inspection evidence, exact plan/scope/check binding, approval-request/user-approval/one-time consumption, immutable Preview/Diff, Fake success, Artifact metadata and no project write before Apply. Add negative assertions for stale hashes, scope drift, forbidden files, nonzero verification, backend unavailable and user-selected rollback.

### F-05 — Must-fix before next node: Skill lifecycle and Tool contract do not match

`feature-delivery/SKILL.md` lines 6–8 deliberately leave `status: draft` and `reviewed: false`; `validate_skill()` requires `reviewed: true` before publication, and the new test asserts the Skill is invalid for publication (lines 114–121). That is appropriate while review is pending, but it means P4-01 is not yet a reviewed/publishable Skill and cannot be used as the completed dependency for P4-02.

The Skill advertises `devpilot_get_run_state` and `devpilot_get_diff` (lines 9–15 and 43–48), but neither appears in `pyproject.toml` nanobot entry points and no corresponding Tool class is present in `nanobot_tools.py`. Conversely, the frozen feature-delivery tool table includes an approval-request operation, which the Skill omits. This leaves the declared Skill contract non-executable and pushes execution to the legacy path.

Recommendation: either register and test the declared read-only tools and the correct approval-request handoff, or revise the Skill to the actually supported contract through an approved implementation change. After the approval and G-01 fixes, update lifecycle metadata only as the result of this independent review and re-run Skill validation; do not claim `active` while it remains draft.

### F-06 — Must-fix before next node: P2B Preview equality survives, but the new run link is not restart-safe

The existing P2B test still checks `second.get(change_set_id) == preview`, and the commit did not change `plan_payload()`, so the original Preview response shape/equality contract is not directly broken. This is the positive backward-compatibility result.

However, P4 adds `ControlPlaneManager.get_run_id()` (lines 201–204) and uses it to populate the proposal (workflows.py lines 128–136). On restart, `_load_entry()` reconstructs `_PendingChangeSet(plan)` without a `run_id` (control_plane.py lines 532–606); the persisted `ChangeSetRecord` also has no run-link field. Consequently, a reconstructed entry returns `None` from `get_run_id()`, and the workflow rejects Artifact delivery with `WORKFLOW_ARTIFACT_UNAVAILABLE` (workflows.py lines 158–162). There is no P4 restart test for this new requirement.

Recommendation: persist or deterministically recover the ChangeSet→Run relation and add a restart test that reconstructs the proposal/run link and completes the Artifact path. Keep the existing `second.get(...) == preview` assertion unchanged to protect the P2B contract.

### F-07 — Must-fix before next node: output redaction and Artifact ownership are not enforced at the new boundary

There is a real positive: `_render_report()` deliberately omits stdout/stderr (workflows.py lines 218–220 and 236–249), and `FeatureDeliveryResult.to_dict()` does not include either approval token. The G-01 test confirms that the benign Fake string `fake-ok` is absent from the generated Artifact (test lines 70–74). On the normal path, the Artifact therefore does not copy the Fake stdout or token.

The enforcement boundary is incomplete. The Workflow calls `ArtifactService.write_markdown()` directly (lines 177–183), but that service only validates the filename, UUIDs and size; it performs no secret scan and accepts any caller-provided `artifacts_root` (artifacts.py lines 23–43). The report interpolates caller-provided evidence IDs (workflows.py lines 242–243), so the new path is not protected by the secret scan used by the P1 Tool and does not prove that the root is a server-owned, non-project directory. Separately, `ControlPlaneManager.verify()` puts bounded but raw stdout/stderr into the RunEvent payload and response (control_plane.py lines 314–342), while `ProjectStore.append_event()` persists that payload without redaction (storage.py lines 236–245). Bounded is not the same as secret-safe.

Recommendation: centralize secret-pattern scanning/redaction before every RunEvent, result cache and Artifact write; return only safe summaries/references to the workflow caller; validate `artifacts_root` against the server-configured Artifact root and reject overlap with any project/data root. Add tests using token-shaped stdout, stderr, evidence IDs and path values, plus assertions that neither Artifact nor persisted event contains them.

### F-08 — Must-fix before next node: failure state is not a complete delivery result

`ControlPlaneManager.apply()` finishes the linked Run as `completed` immediately after file application (control_plane.py lines 373–375), before the Workflow performs Verify. A Fake nonzero exit is represented as `verification_failed` by `verify()` (lines 304–313), but the Run has already been completed. If verification raises `ExecutionError`, the Workflow does not catch it to produce a failure Artifact or a structured stop result; if Artifact writing fails after Apply/Verify, the file change remains applied while the workflow raises from the write path. The current tests cover only successful Fake verification and confirmation rejection.

Recommendation: define and test the P4 failure state machine: keep the Run open until verification and Artifact persistence finish, mark failed/unavailable deterministically, preserve failure evidence, never claim success, and require a separate user-selected rollback path. Add tests for nonzero Fake output, backend unavailable, Artifact write failure, and restart during the failure state.

## Security and boundary review

- Permissions and approvals: project patching still delegates path/hash enforcement to the existing P2B structured patch service, and the normal successful test leaves no file after Preview. The new Workflow nevertheless self-issues Apply/Verify approvals, so the user-confirmation boundary is not acceptable under the frozen plan (F-02).
- Paths: no new arbitrary shell/path input is added to the patch payload, and the existing `AllowedRoots`/structured patch path checks remain in the Preview path. The caller-controlled Artifact root is a separate server-owned-path violation (F-07).
- Network: the server-built verification request sets `network_allowed=False`, and the FC adapter passes that request policy to the SDK. This does not make the P6 gate valid: a caller-controlled gate boolean and the legacy FC environment flag still permit a real backend path without a server-attested P6 record (F-03).
- Secrets/credentials: static inspection found no committed high-confidence API-key/private-key literal in the target tree; FC credentials are read from environment variables. This is not runtime proof: raw verification output is persisted/returned without central redaction, and the new Artifact path bypasses the P1 Tool's content scan (F-07).
- Backward compatibility: the existing P2B Preview restart equality assertion is preserved. The newly added run-link lookup is not restart-safe, and the Feature Workflow has no corresponding compatibility test (F-06).
- Scope: the six files in the target commit are plausibly related to P4-01 and no migration/config/frontend file was changed by the commit. The actual product path still uses the pre-existing nanobot execution flow, and the checkout has two unexplained fixture edits; both prevent a clean P4 Gate (F-01/F-04).
- External dependencies: Fake execution is local and deterministic, but the target tree also includes optional FC dependencies and a live FC path. No phase-4 evidence proves that external execution is either disabled or governed by the frozen P6 gate.

## Unanswered questions

1. Is chat text `执行` intended to be a product approval, despite frozen plan Sections 3.1–3.2 and 4 requiring explicit control-plane approval? If yes, who owns the required plan/ADR change and its re-freeze?
2. What immutable, server-owned record is authoritative for P6 completion? No target evidence identifies a verified backend record or capability hash that can replace `sandbox_gate_passed`.
3. Is `FeatureDeliveryWorkflow` intended to be reachable from nanobot/production runtime, or is it only a library prototype? If reachable, which registered Tools provide `get_run_state`, `get_diff` and approval request?
4. Has the full quality matrix been run from a clean checkout of the exact target commit? If so, where are its exit codes, logs and manifest? If not, which owner will produce them?
5. Are the two modified Node fixture files user-owned work that must be preserved, or should the implementation branch be reviewed in a separate clean checkout?
6. What is the intended Run terminal state when Apply succeeds but Verify or Artifact persistence fails, and what exact user action is required for rollback?

## Concrete recommendations and release gate

Before P4-02:

1. Close F-02 and F-03 first: enforce independent user-issued capability grants and a server-attested, fail-closed P6 gate; remove the legacy pre-P6 real-execution route or place it behind an explicitly compliant manual-only contract.
2. Replace the current G-01 with a real FastAPI fixture Golden test covering evidence, plan/scope/check binding, approval, immutable Diff, Fake verification, Artifact safety, failure and user-selected rollback.
3. Fix Artifact root ownership and central redaction; test stdout/stderr/token/evidence-ID leakage in Artifact, RunEvent and result paths.
4. Persist/recover the run link without changing the P2B Preview response and add Workflow restart/failure tests.
5. Resolve the Skill/tool registration mismatch, then transition the Skill from draft only after validation and independent re-review.
6. Produce a clean target-commit phase-4 manifest and complete regression evidence; explicitly account for the two existing fixture modifications.

Final gate decision: `blocked`. P4-02 is not authorized by this review. A new independent Review is required after the blockers and required evidence are closed.
