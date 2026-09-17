# Uncertain-turn recovery Spec review

- Fixed point: `64685b1`
- Candidate: `331beaa`
- Reviewer: Spec subagent `01a0ae3a-18af-7472-a602-90ee1e15964f`
- Scope: `git diff 64685b1...331beaa`
- Verdict: **PASS**

## Checked against

- `devpilot-agent/docs/plans/CURRENT_PLAN.md` sections 0, 6 and 7
- `devpilot-agent/docs/plans/LIVE_HARNESS_EXTENSION.md` H7/H8 contracts
- H7 Host-owned checkpointing and no-replay boundary
- H8 managed owner/CAS, recovery, budget and fresh-turn behavior

## Final conclusion

The final candidate keeps the old unknown operation queryable without making it a global conversation lock. New user turns are processed independently, but the Host still refuses the exact old side-effect call. Checkpoints without durable replay fingerprints fail closed. Recovery turns can run with a fresh local budget even when the old durable budget is exhausted; the durable checkpoint remains monotonic. Matching terminal status can retire a single corresponding stale intent, while unrelated or multiple stale intents remain protected. `/new` is an explicit clean-session reset.

The final tests cover ordinary continuation, exact replay blocking, legacy fail-closed behavior, zero-budget recovery, terminal-query marker cleanup, mixed unrelated intents, H8 persistence, and `/new` reset. No remaining H7/H8 specification blocker was reported.
