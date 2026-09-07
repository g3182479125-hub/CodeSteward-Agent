# P8-01 Acceptance Evidence

Date: 2026-09-07
Scope: bounded Skill-improvement evidence collection and inactive candidate storage

## Delivered behavior

- The first trial Skill is fixed to `skills/bug-fix/SKILL.md`.
- User feedback and tool-result evidence is recorded as `verified`; model inference and external content remain `pending_verification` until two independent references are present. Every record carries user, session, workspace, project, task, Skill, and hash attribution.
- Candidate content is stored under the private `skill-improvement/candidates/` data directory outside the nanobot workspace by default, not under the workspace `skills/` directory, so `SkillsLoader` and normal file tools cannot discover or modify it. Explicit data roots inside the workspace are rejected.
- Candidate creation rejects path traversal, unsupported Skill IDs, unverified evidence, cross-project evidence, changed frontmatter/trust metadata, sensitive material, oversized content, and more than three candidates in one batch.
- The effective `bug-fix` Skill is protected on all Dream write paths: `edit_file`, `apply_patch`, and `write_file`. Ordinary workspace Skills remain writable for Dream memory maintenance.
- Records use integrity-protected atomic JSON/JSONL persistence; tampered records fail closed. No model call, Skill activation, publishing, or autonomous write to the effective Skill is performed by P8-01.

## Verification commands

Run from `devpilot-agent/`:

```powershell
uv run ruff check src/devpilot/skill_improvement.py src/devpilot/nanobot_tools.py
uv run pytest tests/test_p8_01_skill_improvement.py -q
uv run pytest tests/test_p4_bug_fix.py tests/test_p5_api_contract.py -q
```

Run from `nanobot/`:

```powershell
uv run pytest tests/agent/test_dream.py tests/agent/test_p8_skill_guard.py -q
```

Expected evidence includes successful feedback/candidate persistence, redaction and metadata checks, the three-candidate batch limit, protection of the effective Skill, and preservation of the original four-tool Dream registry contract.

## Explicit non-goals

P8-01 does not evaluate candidates, change the active Skill, expose a management page, create a second database, or add a new agent loop. P8-02 owns fixed comparison, direct chat confirmation, snapshot binding, atomic activation, and guarded restore.
