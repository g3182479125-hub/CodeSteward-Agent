# H8 Loop handoff evidence

Commit under test: `6caf8d3` plus its H8 completion follow-up.

## What is live

- The existing DevPilot SQLite database now owns `loop_handoff_state`; it is
  separate from `plugin_runtime_state`, so a Loop handoff cannot overwrite a
  tool/plugin route.
- `/plugins switch-loop <default-react|plan-then-act>` records an operation and
  waits for a Host checkpoint.  It does not restart nanobot or replace a
  session.
- `ManagedLoopDriver` resolves the active durable owner on every Host action.
  A committed boundary is visible to the next action in the same `AgentRunner`.
- An unavailable/incompatible saved Driver returns to `default-react` while
  preserving the checkpoint.  Uncertain side effects remain paused and are not
  replayed.

## Automated evidence

From `nanobot/`:

```text
32 passed in 5.00s
```

The focused set covers H8 driver ownership, default→plan→default continuity,
switch-during-tool waiting, one tool dispatch, generation CAS, incompatible
Driver recovery, native command/status, H7 checkpoint privacy/uncertainty, and
the three existing Profile routes.

From `devpilot-agent/`:

```text
4 passed
```

The migration test upgrades a blank database to `0013_h8_loop_handoff_state`
and verifies that the handoff table is present.

## 2026-09-14 continuation evidence

- The migration head is `0015_h8_switch_generation`; the local DevPilot
  database contains `loop_handoff_state` as its independent ownership store.
- A real FC run using `code-interpreter-v1` and `provider-default` completed
  a candidate trial with `passed` status and clean sandbox cleanup.  The same
  immutable strategy completed a live decision; after rebuilding the catalog,
  recovery queried the original trial id without replaying the decision.
- A real local WebUI session completed a default Driver checkpoint with stable
  session/task/project ownership and a persisted session-prefix hash.  The
  current `AgentLoop` integration also commits a pending default→plan switch
  using that ownership record; a Profile-derived task id no longer replaces a
  command-established owner.
- Automated H8 evidence: `88 passed` from the focused handoff, command,
  remote-strategy, candidate-runner, and migration suite; Ruff and
  `git diff --check` passed.  The suite covers A/B/C/D/E/F/G/H/I contracts,
  including one-dispatch ownership, recovery-only uncertain work, three
  persistence windows, structured Driver state, redacted remote projection,
  durable budgets, FC cancellation, and restart recovery.
- The first independent Standards review caught a reproducible `WinError 206`
  in the deep-workspace remote-artifact regression.  Artifact, journal, and
  legacy-artifact file operations now use the Windows extended-length path
  form only on Windows; stored locations and non-Windows behavior are
  unchanged.  The exact remote-strategy suite passes `8 passed` both with the
  normal pytest temporary directory and `--basetemp D:\\tmp\\h8-pathfix`; the
  post-fix complete focused suite passes `88 passed in 4.13s`.

## Remaining completion gate

This evidence does not replace ADR-0004's mandatory independent Standards and
Spec reviews.  H8 remains in verification until both reviews explicitly pass
the final candidate and their reports are saved under `docs/reviews/`.

## Deliberate boundaries

- Only two trusted local Driver artifacts are registered in H8.  A remote or
  model-generated Driver is rejected instead of receiving conversation data;
  H9 will add the candidate/evaluation path.
- The UI/command reports `switch_pending` until a committed safe boundary.  A
  pending tool call or an uncertain business run prevents ownership transfer.
- User cancellation remains implemented by the Host runner; Driver objects do
  not receive cancellation authority.
