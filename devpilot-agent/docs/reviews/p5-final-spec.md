# P5 Final Spec Review

## Status

Blocked by the required review subagent service. The Spec reviewer was
dispatched in parallel with the Standards reviewer twice after the final
fixes. Both attempts failed before returning a report with:

`unexpected status 404 Not Found: Unknown error, url: https://chatgpt.com/backend-api/codex/responses`

Per `AGENTS.md`, this cannot be counted as a Spec approval. Local contract,
build, smoke, and evidence checks are recorded in
`docs/evidence/phase-5/p5-acceptance.md`.

## Scope notes for the next review

The optional console intentionally keeps token-gated Apply, Verify, and
Rollback in nanobot's existing confirmation chain, while ChangeSet detail
contains the final accept/stop decision controls and never stores tokens.
Canonical `/api/v1` responses remove local filesystem coordinates; legacy
`/v1` responses preserve their previous payload shape for compatibility.
