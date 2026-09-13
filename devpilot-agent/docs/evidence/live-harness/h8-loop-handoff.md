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

## Deliberate boundaries

- Only two trusted local Driver artifacts are registered in H8.  A remote or
  model-generated Driver is rejected instead of receiving conversation data;
  H9 will add the candidate/evaluation path.
- The UI/command reports `switch_pending` until a committed safe boundary.  A
  pending tool call or an uncertain business run prevents ownership transfer.
- User cancellation remains implemented by the Host runner; Driver objects do
  not receive cancellation authority.
