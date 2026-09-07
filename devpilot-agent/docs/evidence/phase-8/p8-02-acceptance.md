# P8-02 Acceptance Evidence

Date: 2026-09-07
Scope: fixed comparison, native-chat confirmation, activation, task snapshots, and restore

## Delivered behavior

- The trusted evaluator runs a fixed synthetic Agent/Tool protocol trace for three categories (`original_failure`, `nearby_holdout`, `regression`), baseline/candidate versions, and three repeats: exactly 18 cases. Candidate text can affect the simulated action trace, but cannot alter the case definitions or assertions. The trace is protocol-level acceptance evidence, not a claim of live nanobot tool execution.
- Eligibility requires a real fixed assertion improvement, no category regression, and complete results. The synthetic runner records model/parameter fields, start-file hash, Skill hashes, profile, template, budget, usage availability, duration, and every case result. It performs no model call and does not claim general statistical improvement.
- `/skill-improvement` is the only user-facing control. It lists/views candidates, runs evaluation, and supports `enable`, `ignore`, and `restore` in the existing nanobot chat; no new page or agent loop is added.
- Enablement creates a five-minute, one-time confirmation bound to user, session, workspace, project, Skill, base/candidate hashes, test set, and evaluation result. Activation rechecks the live base hash, atomically replaces only `skills/bug-fix/SKILL.md`, and records the previous content.
- Task snapshots are immutable per task ID: an existing task continues to read its captured version, while a new task captures the current effective version. Restore is explicit, serialized under the store lock, and stops on external edits instead of overwriting them. Activation journal recovery fails closed on integrity errors and reconciles a committed file swap after restart.
- `/skill-improvement delete-evidence <evidence-id>` tombstones authorized feedback, removes its text/reference payload, and invalidates linked candidates; deletion requires a direct user turn and matching workspace/user/project scope.
- The effective Skill remains protected from Dream and normal filesystem writes; host shell is disabled when `DEVPILOT_ENABLE_SKILL_IMPROVEMENT=1`. DevPilot execution remains the separate approved backend.

## Verification commands

Run from `devpilot-agent/`:

```powershell
uv run ruff check src/devpilot/skill_improvement.py src/devpilot/nanobot_tools.py
uv run pytest tests/test_p8_01_skill_improvement.py tests/test_p8_02_skill_improvement.py -q
```

Run from `nanobot/`:

```powershell
uv run ruff check nanobot/agent/task_profiles.py nanobot/command/builtin.py nanobot/agent/tools/filesystem.py nanobot/agent/tools/shell.py tests/agent/test_p8_skill_guard.py tests/agent/test_dream.py
uv run pytest tests/agent/test_dream.py tests/agent/test_p8_skill_guard.py tests/agent/test_task_profiles.py tests/command/test_trigger_command.py tests/command/test_skill_command.py -q
```

The P8-02 service suite verifies 18-case improvement, no-improvement refusal, one-time/scope-bound confirmation, old/new task snapshots, atomic activation, and restore conflict detection. Native-chat tests verify the existing nanobot command route.

## Non-goals and evidence boundary

This is a deterministic synthetic acceptance harness, not a claim that every real task improves. It does not evaluate user repositories, change test standards, call a model, silently roll back, or implement P8-03 scheduled batching. P8-03 is recorded as “optional, not selected”; no automatic activation or production Skill update is enabled by default. Service-owned P8 records are persisted privately and are not part of the workspace or Git handoff.
