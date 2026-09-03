# P4-04 Parallel Subagent Review

Date: 2026-09-03 (Asia/Shanghai)
Review fixed point: `2f5b6e1`
Runtime implementation target: `01c8e4804ceeccc2461f3e14c0aad9fa123448b7`
Review method: two independent read-only subagents were started in parallel;
one reviewed Standards and one reviewed Spec. The implementation owner did not
edit files during either review.

## Standards

The Standards subagent reported:

> Hard violations: 0.
>
> Fowler judgement calls: Duplicated Code in `delivery_decisions.py` because two
> state sets overlap; Middle Man because `accept/stop` delegate to the manager;
> Shotgun Surgery because Tool registration is spread across `pyproject.toml`
> and three Skills. No other baseline smell was confirmed.

The earlier evidence finding was valid at the review point and is closed by
the committed `docs/evidence/phase-4/p4-04-acceptance.md`,
`p4-04-manifest.json`, and `p4-04-verification.log`. The registration changes
are intentional contract synchronization, and the delegation is the designed
workflow boundary, so both remain judgement calls rather than hard violations.

## Spec

The Spec subagent reported these findings against the code review point:

> The required acceptance, manifest, verification log and review record were
> absent at the initial review point. The initial focused test covered the
> feature workflow only. Unknown decision/empty confirmation did not yet have
> the desired stable error handling, and accepting failed verification could
> clear `failure_reason`.

Resolution:

- the four evidence records are now present and referenced by the manifest;
- the full regression run includes the existing P4-01, P4-02 and P4-03 suites,
  while the focused P4-04 contract suite has five passing tests;
- the decision request now routes unknown actions to `DECISION_INVALID` and
  mismatched confirmations to `USER_CONFIRMATION_REQUIRED`;
- acceptance preserves the persisted failure reason for a failed verification;
- the mandatory subagent-review rule was added to `AGENTS.md` and the vendored
  skills README because the user explicitly requested it. It is not an
  unrequested runtime scope expansion.

## Review outcome

No unresolved hard Standards violation remains. Runtime findings from the
initial review were fixed and re-tested. P4-04 evidence is now linked to the
implementation commit and the quality transcript; the only remaining baseline
items are documented judgement calls with no Gate impact.
