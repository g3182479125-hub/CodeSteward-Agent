# H7 Loop Driver Boundaries — automated evidence

Candidate: `33bf51e` (`feat: add h7 loop checkpoint boundaries`).

Scope: H7 only.  This record does not claim that loop replacement (H8), version
selection (H9), or the H10 browser demonstration has been completed.

## What was exercised

On 2026-09-13 the following command ran from `nanobot/`, with pytest temporary
files isolated on E: because the Windows default pytest temp folder was locked:

```powershell
uv run --with pytest-asyncio pytest --basetemp E:\codex-local\pytest-h7\run-05 `
  tests/agent/test_loop_driver.py `
  tests/agent/test_runner_core.py::test_runner_replays_provider_state_without_chat_projection_duplicates `
  tests/agent/test_loop_save_turn.py::test_runtime_checkpoint_keeps_provider_state_out_of_public_metadata `
  tests/agent/test_loop_save_turn.py::test_restore_runtime_checkpoint_rehydrates_completed_and_pending_tools `
  tests/agent/test_loop_save_turn.py::test_restore_runtime_checkpoint_accepts_h7_driver_provenance `
  tests/agent/test_task_profiles.py -q
```

Result: **18 passed in 1.62s**.

`uv run ruff check` over the changed runner, loop, driver and test files, plus
`git diff --check`, both passed in the same run.

## Facts established by the tests

| H7 requirement | Evidence |
| --- | --- |
| Host owns effects; Driver only selects an action | `test_runner_calls_driver_at_model_and_tool_boundaries` observes decisions at both real boundaries while the existing Host performs the tool call. |
| Default behavior remains available | `DefaultReactDriver` returns model, tool-dispatch and pause decisions; the three existing profile contract tests all pass. |
| Checkpoint is public and serializable | `LoopCheckpoint` round-trip/reject-schema tests plus the persisted-loop test validate a public `h7_loop_checkpoint`; provider state stays out of public metadata. |
| Pending/unknown effects are not switch-safe | `test_checkpoint_blocks_driver_switch_for_pending_or_uncertain_side_effects` rejects both conditions. |
| Legacy records remain readable | Existing runtime-checkpoint recovery tests continue to restore records that omit H7 fields. |

## Limits retained deliberately

H7 does not yet expose a switch command, transfer Driver ownership, or execute a
second Driver.  Those changes are H8.  A pending tool is recorded as an
uncertain side-effect reference and is therefore not eligible for a Driver
switch; it is not replayed by this H7 code.
