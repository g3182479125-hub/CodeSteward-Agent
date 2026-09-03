# Matt Pocock Skills, Vendored Copy

This directory contains the local copy of the engineering and productivity
Skills used by this repository's coding agents.

Source: https://github.com/mattpocock/skills

The copy was imported from `skills-main.zip` on 2026-09-03. The complete set is
under `skills/`. Each Skill has its own `SKILL.md`, and some Skills include
supporting templates, references, scripts, and Codex metadata.

The repository root `AGENTS.md` is the required entry point. Every task must
read this file and the relevant Skill before taking action. The vendored copy
is intentionally tracked with this project so another computer can use the
same workflow after cloning it.

For every code review, the required `engineering/code-review/SKILL.md` flow is
mandatory: run independent Standards and Spec reviews through parallel
subagents, aggregate them under separate headings, and never replace them with
only a main-agent self-review. If subagent tooling is unavailable, report the
review as blocked instead of claiming completion.
