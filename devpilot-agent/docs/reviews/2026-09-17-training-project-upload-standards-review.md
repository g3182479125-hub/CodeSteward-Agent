# Training project upload — Standards review

- Fixed point: `c6b4725`
- Candidate: `15b0432`
- Diff: `git diff c6b4725...15b0432`
- Reviewer: Standards subagent `01a0ad94-af8d-7c33-a195-b52dc2975cf7`
- Governing references: `AGENTS.md`, `CURRENT_PLAN.md` sections 0/6/7, and ADR-0004.

## Verdict

**PASS**

The review confirmed that the training project is committed as ordinary files rather than a
nested Git repository, and that no secrets, databases, logs, virtual environments, temporary
pytest directories, or unrelated runtime files are included. The second-window PowerShell
instructions rebuild the repository and project paths and reload the user-scoped control-plane
token before registration and trust. The route, header, and token handling match the implementation.

`git diff --check` and the repository Markdown checks passed. No scope creep or remaining
Standards blocker was found.
