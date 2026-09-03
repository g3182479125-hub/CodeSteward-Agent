# Agent Instructions

This repository contains the nanobot agent and the DevPilot control plane. The
project also vendors Matt Pocock's engineering skills under
`agent-skills/mattpocock-skills/skills/` so that every coding agent can use the
same workflow after cloning the repository.

## Required skill workflow

At the beginning of every task, read:

1. `agent-skills/mattpocock-skills/README.md`
2. The relevant `SKILL.md` under `agent-skills/mattpocock-skills/skills/`

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
