# P5 Final Spec Review

## Status

The Spec reviewer completed its inspection of `git diff 7ec7bd6...HEAD`, but
the review service also reported an intermittent 404 during the run. The
following findings remain open for a later P5 cleanup pass:

`unexpected status 404 Not Found: Unknown error, url: https://chatgpt.com/backend-api/codex/responses`

Per `AGENTS.md`, the service error remains a process blocker for certification.
Local contract, build, smoke, and evidence checks are recorded in
`docs/evidence/phase-5/p5-acceptance.md`.

## Spec report

- Major: the committed smoke script is a shell/API check, not a browser smoke
  test against a fake API fixture.
- Minor: project registration adds a mutation beyond the P5-01 non-goal that
  excludes new project resources.
- Major: configured-but-unverified backends are labelled `configured`; the
  console should label them unavailable/denied until verification succeeds.

## Scope notes for the next review

The optional console intentionally keeps token-gated Apply, Verify, and
Rollback in nanobot's existing confirmation chain, while ChangeSet detail
contains the final accept/stop decision controls and never stores tokens.
Canonical `/api/v1` responses remove local filesystem coordinates; legacy
`/v1` responses preserve their previous payload shape for compatibility.
