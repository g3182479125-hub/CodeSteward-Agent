# P4-02 Independent Product Review — Round 2

Decision: `request-changes`

Reviewer: independent read-only Review subagent `Hegel`.

## Findings

- `[blocker]` A previous patch accidentally changed the P4-01 feature-delivery
  failure event to `bug_fix.failed`.
- `[blocker]` Case-sensitive evidence keys could allow conflicting before hashes
  for the same Windows file.
- `[must-fix-before-next-node]` Reports lacked file-level and manifest-level
  before/after hashes.
- `[must-fix-before-next-node]` Failure Artifact events did not consistently bind
  the Artifact identity.
- `[must-fix-before-next-node]` API, Tool, Skill and static-command contract
  coverage was incomplete.
- `[must-fix-before-next-node]` The current implementation needed a commit-bound
  P4-02 evidence manifest rather than reusing P4-01 test claims.

## Re-review disposition

The implementation agent fixed the event type, introduced OS-aware canonical
path keys and conflicting-hash rejection, added file/manifest hashes and
Artifact IDs to reports/events, expanded contract tests, and prepared a
commit-bound evidence package. A new independent review is required against
that implementation commit; this round itself does not unlock P4-03.
