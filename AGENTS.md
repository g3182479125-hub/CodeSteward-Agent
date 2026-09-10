# Agent Instructions

## Live Harness extension planning (2026-09-10)

For runtime plugin / self-evolving Harness work, also read
`devpilot-agent/docs/plans/LIVE_HARNESS_EXTENSION.md` in full. It defines the
proposed H1–H10 extension and explicit exceptions to the old startup-only plugin
scope. P0–P8 remain historical completed work; H1 and H2 are implemented and
passed their batched review, and H3–H10 are not implemented.
When the user asks to implement this extension, follow its contracts and node
order instead of interpreting the old no-hot-unload/no-Loop-change rules as a
ban on the newly requested capability. Existing execution permissions remain.
The user requested one subagent review and one revision for this plan only;
future code reviews still follow the batched Standards/Spec procedure below.

This repository contains the nanobot agent and the DevPilot control plane. The
project also vendors Matt Pocock's engineering skills under
`agent-skills/mattpocock-skills/skills/` so that every coding agent can use the
same workflow after cloning the repository.

## Required skill workflow

At the beginning of every task, read:

1. `agent-skills/mattpocock-skills/README.md`
2. The relevant `SKILL.md` under `agent-skills/mattpocock-skills/skills/`
3. `devpilot-agent/docs/adr/ADR-0004-batched-review-and-incremental-recheck.md`
   (mandatory review/fix workflow amendment to the frozen plan).
4. `devpilot-agent/docs/plans/CURRENT_PLAN.md` (current product scope, design,
   node acceptance, and module map; precedence is recorded in ADR-0006).

The product is a personal agent based on nanobot, with selective Harness design
adoption tracked in `devpilot-agent/docs/plans/HARNESS_ADOPTION.md`. For remaining
work follow CURRENT_PLAN 3.3, including the full node specifications in section 6
and mandatory review procedure in section 7. Every developer and reviewer must
read these before acting; a prior-model summary is not sufficient.
For P8, also read `devpilot-agent/docs/plans/SKILL_IMPROVEMENT.md` in full.
ADR-0007 authorizes this bounded addition; it does not authorize autonomous
production Skill updates or weaken any review or execution gate.
Do not reopen completed P0-P5 work or recreate
nanobot capabilities merely to demonstrate borrowing from a reference project.

Do not begin implementation, editing, or test changes until the relevant Skill
has been read. If the task is ambiguous, use the engineering skill that helps
resolve the ambiguity before changing code.

For code development, select the smallest applicable workflow:

- New or changed behaviour: read `engineering/implement/SKILL.md` and
  `engineering/tdd/SKILL.md`.
- Design or architecture work: also read `engineering/codebase-design/SKILL.md`.
- Bug diagnosis or regression: read `engineering/diagnosing-bugs/SKILL.md`.
- Review: read `engineering/code-review/SKILL.md`.
- Merge or rebase conflicts: read `engineering/resolving-merge-conflicts/SKILL.md`.
- Requirements are unclear: read `engineering/grill-with-docs/SKILL.md` before
  implementation.

Every implementation must finish with relevant automated tests and a review
of the resulting diff. Keep the workflow evidence in the repository's
existing documentation locations.

## Mandatory subagent review

Any code review is incomplete unless it uses parallel subagents for both axes:

1. A Standards subagent checks the diff against repository standards and the
   documented Fowler-smell baseline.
2. A Spec subagent checks the diff against the originating design/spec and
   reports missing requirements, scope creep, and incorrect implementations.

The main agent must pin a fixed point, capture `git diff <fixed-point>...HEAD`,
send the two independent prompts in parallel, wait for both reports, and
record them separately. A local self-review may supplement this process but
must never be presented as a substitute. If subagent tooling is unavailable,
the review is blocked and the final report must say so explicitly.

Apply ADR-0004 to scheduling and follow-up: batch fixes, validate once per changed
scope, then dispatch both axes against one immutable candidate. Reuse the same
reviewers for documented incremental rechecks. Only the main agent dispatches the
pair; reviewers do not recursively spawn reviewers. A reviewer's lack of spawning
tools is not a blocker when the main agent has successfully dispatched both axes.
Never infer PASS from silence, a timeout, or a request to finish quickly.

Review a completed batch once along both axes, then reuse reviewers for delta and
impact checks. Do not restart full reviews for each finding or for recording a
verdict. If the same issue survives a second recheck, diagnose the cause before
another review; never waive an unresolved defect because a round limit was reached.
Include CURRENT_PLAN sections 0/6/7 and the relevant node in every review prompt.

## Project skill source

The vendored Skill source and its supporting files are under
`agent-skills/mattpocock-skills/`. This is a pinned local copy from
`https://github.com/mattpocock/skills`. Update it deliberately, review the
diff, and keep the project instruction above synchronized when the workflow
changes.

## Project configuration

- Issue tracker and domain-document rules: `devpilot-agent/docs/agents/`
- DevPilot architecture and phase evidence: `devpilot-agent/docs/`
- nanobot-specific instructions: `nanobot/AGENTS.md`
- Secrets, virtual environments, databases, logs, and generated dependencies
  must remain untracked.
