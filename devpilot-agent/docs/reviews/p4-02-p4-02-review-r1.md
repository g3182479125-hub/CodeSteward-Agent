# P4-02 Independent Product Review — Round 1

Decision: `request-changes`

Reviewer: independent read-only Review subagent `Kierkegaard`.

## Findings

- `[blocker]` Bug-fix verification/backend failure did not consistently create a
  failure Artifact, and Artifact persistence fallback used the feature-delivery
  event type. Recommendation: add bug-fix failure Artifact/event and fallback.
- `[must-fix-before-next-node]` Delivery reports lacked before/after hashes,
  profile, exit code, Artifact ID and an explicit Fake-only/no-real-React-test
  statement.
- `[must-fix-before-next-node]` `bug_check.failed` had no server-owned producer
  provenance; tests manually appended the event.
- `[must-fix-before-next-node]` G-02 tests recreated the React fixture instead of
  binding the repository fixture.
- `[must-fix-before-next-node]` API, nanobot adapter, error mapping, static
  command and Skill contract coverage was incomplete.
- `[must-fix-before-next-node]` Conflicting before hashes from multiple failure
  Runs were silently overwritten.
- `[must-fix-before-next-node]` Bug-fix contract details were not recoverable
  through a restarted control plane.

## Positive checks

Apply still required an external capability and explicit confirmation; nanobot
did not receive capability tokens; patch scope and hashes remained server-bound;
non-Fake execution stayed fail-closed; no P4-01 API removal was found.

## Re-review requirement

The implementation agent must provide new test/evidence results and launch a new
independent read-only review after the fixes. This round does not unlock later
nodes.
