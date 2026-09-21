# Spec review: remove final delivery acceptance

Date: 2026-09-21 (Asia/Shanghai)
Baseline: `8efcfbd`
Initial candidate: `eef766c`
Final candidate: `c39ff5d`

## Scope

The review checked `git diff 8efcfbd...c39ff5d` against `AGENTS.md`, ADR-0004,
CURRENT_PLAN sections 0, 6, and 7, and the P4-04 delivery decision design.

## Required behavior

- A verified or verification-failed delivery ends without a second acceptance
  step.
- `accept` is not a new intent, decision, event, API action, or report option.
- Apply, Verify, Stop, and capability-gated Rollback remain available.
- Existing persisted `accepted` values remain readable only and cannot be
  mutated by the current control plane.
- Skills, tests, and the recording runbook describe the same user flow.

## Final verdict

**PASS**

The first incremental review identified the legacy mutation boundary and the
outdated P4-04 contract. Both were corrected. The final review of
`ebc4319...c39ff5d` found no missing requirement or scope creep.
