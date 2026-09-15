# H11 Spec Review — candidate 62540b5

- Reviewer: `01a0a424-5d7c-7e13-8bd2-c5a143ea2d56`
- Axis: Spec
- Baseline: `3b37fd0`
- Candidate: `62540b5`
- Scope: `git diff 3b37fd0...62540b5`; H11.1–H11.5 and CURRENT_PLAN §0/§6/§7 checked.

## Verdict

**FAIL**.

1. H11.2/H11.4: `model_plugins.py:698-720` and
   `candidate_runner.py:298-312` persist autonomy data only on some FC
   receipts. Define/activate/stop/remove, confirm/deny, cleanup, and the
   non-blocking `notice` activity are not uniformly represented in the
   existing operation/event record. Minimum fix: use the existing operation
   ledger for every decision and include candidate/version, input class,
   budget fact, and FC summary.
2. H11.1: `_authorize` at `model_plugins.py:731-815` also governs H9
   `evaluate`, `evaluate_loop`, and `trial_loop`; `full` can therefore relax
   non-H5/H6 lifecycle actions. Minimum fix: apply autonomy only at the H5/H6
   candidate entry points.
3. H11.4/H11-D: `manage`'s `revoke` only clears in-memory authorization; it
   does not stop/drain/clean active routes, and an old proxy can be
   re-authorized by `full` in `execute_active`. Minimum fix: stop candidate
   routes on revoke and reject stale/non-active proxy registrations.

## Verification and limits

The reviewer checked the same fixed diff and reported the local H11/H5/H6
target tests passing. Real WebUI/FC and the full suite were not rerun by this
review. The candidate evidence also had an unlabelled old error-code reference.
