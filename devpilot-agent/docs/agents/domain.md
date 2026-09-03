# Domain documentation

This repository uses a single-context documentation layout.

- The project vocabulary and durable domain model belong in `CONTEXT.md` at
  the repository root when one is needed.
- Architectural decisions belong in `docs/adr/`.
- DevPilot phase evidence, reviews, handoffs, and runbooks remain under
  `devpilot-agent/docs/`.

Agents should read the relevant context and decision records before changing a
module, and should update them when a durable domain or architecture decision
changes.
