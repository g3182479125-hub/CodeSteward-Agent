# P4-01 独立产品 Review Closure

## Decision

`blocked`

P4-01 仍未放行，P4-02 不允许进入。以下为本次复核后仍存留的阻断项；用户已知的两处 Node fixture 未提交修改视为外部修改，不归因于实现。

## Review identity

- Repository: `D:\Users\tianmin.li\Documents\Codex\2026-08-10\new-chat\work\remote-integrate\devpilot-agent`
- Implementation commit: `c304d586cbce0d4487c9c6030d389a189ecbc711`
- Current HEAD: `2bf69a6fdfae29ee65b402fa6262cd5f24d3bc60`
- Frozen plan: `DEVPILOT_FROZEN_PLAN.md`, version `2.1.0`
- Frozen plan SHA-256: `4f06504ad3273781f7c4cff2d919aec432ea0385014c8c94c42455709efbdec9`
- Review mode: read-only except this closure report.

## Remaining blockers

### B1 — P6 gate is still forgeable and does not validate the attested capability

`src/devpilot/execution_gate.py:8-35` uses an importable module-level `_SERVER_ATTESTATION` object. A caller can import that object and construct an arbitrary `P6ExecutionGate`; `permits()` then authorizes solely by backend name and expiry, without checking `capability_hash`, suite version, backend version, or a durable server-owned verification record. Final verification: an ephemeral command constructed `P6ExecutionGate("rogue", "0" * 64, "unverified", future, _attestation=_SERVER_ATTESTATION)` and returned `permits_rogue: True`.

Although `create_app()` currently leaves the FC backend without a gate, `DEVPILOT_ENABLE_FC_SANDBOX=1` still registers the FC backend (`src/devpilot/control_plane.py:733-755`). The default path fails closed, but the supplied gate object is not an unforgeable server attestation, so the P6 requirement is not closed globally.

Required to unblock: make gate issuance exclusively server-owned and non-forgeable, bind it to the verified backend identity/version, capability hash, verification-suite version, platform and expiry, and add a negative test proving an arbitrary same-name/rogue backend cannot execute. Preserve the FC environment-variable fail-closed test.

### B2 — G-01 evidence is still not a single, fully bound FastAPI Golden chain

The current tests cover the HTTP proposal handoff and explicit rollback, plus a Fake success path, but the principal G-01 test still uses source text as the fixture and only passes an inspect run id as evidence. `FeatureDeliveryWorkflow.propose()` checks project ownership and `run kind == "repo-analysis"` (`src/devpilot/workflows.py:152-165`), but does not bind concrete inspect/search/read evidence records, their content hashes, or their provenance to the plan. The success path also calls `manager.approve()` directly; the HTTP test separately exercises approval endpoints, so no one machine-readable Golden test proves the complete chain with the exact external `r1.apply` and `r2.verify` approvals, evidence binding, immutable Diff hash, Fake receipt, Artifact and explicit rollback.

Required to unblock: add one reproducible G-01 test for a bounded FastAPI restricted-interface addition that records and validates inspect/search/read evidence provenance and hashes, external control-plane approval for both capabilities, immutable preview/Diff hash, no pre-Apply write, Fake verification, server-owned Artifact and user-selected rollback.

### B3 — Phase-4 manifest is not complete under the frozen Gate schema

`docs/evidence/phase-4/manifest.json` correctly records implementation commit `c304d586...`, plan version/hash, timestamps, command exit codes, migration head, dependency lock hash, artifact hashes and the explained external fixture changes. However, it has no explicit configuration summary (with secrets excluded), while frozen plan Section 9.1 requires the manifest to record a configuration summary. The documented `clean_tree: false` is acceptable only as the explicitly accounted-for external fixture state; it must not be used to claim an actually clean implementation tree.

Required to unblock: add a secret-free `config_summary` (or equivalent) covering the effective P4 verification configuration, retain the exact implementation commit and external-change explanation, then regenerate artifact hashes and re-review the resulting tree.

## Gate outcome

Because B1 is a security gate failure, and B2/B3 leave the frozen G-01 and evidence Gate unproven, P4-01 remains `blocked`. P4-02 is not authorized.
