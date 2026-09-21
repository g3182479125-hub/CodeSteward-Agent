# Standards review: remove final delivery acceptance

Date: 2026-09-21 (Asia/Shanghai)
Baseline: `8efcfbd`
Initial candidate: `eef766c`
Final candidate: `c39ff5d`

## Scope

The review checked `git diff 8efcfbd...c39ff5d` against `AGENTS.md`, the
vendored engineering workflow, ADR-0004, and CURRENT_PLAN sections 0, 6, and
7. The relevant contract was P4-04 delivery reporting and decision handling.

## Initial findings and resolution

- The delivery Skills still instructed the model to ask for acceptance. The
  instructions now say verification ends the flow and only stop remains an
  explicit delivery decision.
- The P4-04 design still documented `accept`. The design now documents only
  stop/rollback and explicitly rejects `accept` as an unknown decision.
- Legacy `accepted` ChangeSets were not protected from new mutation. The
  control plane now keeps them readable but rejects mutation with
  `CHANGESET_LEGACY_READ_ONLY`; the nanobot adapter exposes that stable code.
- Tests now cover legacy read-only behavior and stop idempotent replay.

## Final verdict

**PASS**

The final incremental review of `ebc4319...c39ff5d` confirmed that the stable
legacy error is exposed and found no remaining standards issue.
