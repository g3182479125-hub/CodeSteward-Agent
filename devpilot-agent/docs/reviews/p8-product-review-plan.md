# P8 Product Review Plan

Date: 2026-09-07

## Outcome and boundaries

P8 delivers a bounded, user-controlled loop for improving the existing `bug-fix` Skill from failure feedback: collect evidence, create a private candidate, compare it against a fixed acceptance set, activate it only after an in-chat confirmation, pin the selected Skill for active tasks, and restore it when explicitly requested. The existing nanobot chat remains the only user-facing surface.

P8 does not add a dashboard, a second chat UI, autonomous Skill activation, an unrestricted code executor, a general prompt optimizer, a live-model quality claim, a scheduler, or P8-03 multi-Skill orchestration.

## Node acceptance questions

- P8-01: Are feedback and candidates bounded, attributable to the current workspace/project and Skill version, protected from sensitive/protected metadata, private to the data directory, and unreachable as an active Skill until explicit evaluation and confirmation?
- P8-02: Are there exactly 18 fixed baseline/candidate comparisons across the three required categories and three repeats; are traces and hashes persisted; are confirmation, activation, task snapshot, restore, conflict, integrity, and restart-recovery behavior enforced?
- P8-03: This optional node is not selected for this delivery. No implementation or acceptance claim is permitted.

## Evidence and checks

Reviewers inspect the frozen plan, changed source, tests, acceptance evidence, and the actual test output. Required evidence includes the DevPilot P8 tests, nanobot integration/guard tests, lint output, `git diff --check`, and native-chat command wiring. Synthetic evaluation must be described as deterministic acceptance evidence, not as a general production quality measurement.

## Safety and compatibility checks

Check workspace/project/session/user binding, protected `bug-fix` Skill read/write paths, sensitive-data handling, bounded payloads, atomic file replacement, one-time expiry-bound confirmation, immutable task snapshots, external-edit conflict behavior, restart reconciliation, and preservation of the original nanobot UI. Existing users must retain the normal Skill/tool behavior when controlled mode is disabled.

## Severity and decisions

- Blocker: violates a frozen acceptance boundary, permits an unapproved or cross-workspace activation, loses the stable version, or lacks evidence for a required safety invariant.
- Must-fix-before-next-node: required behavior is incomplete or the evidence is not reproducible.
- Follow-up: useful hardening explicitly outside P8 acceptance.
- Observation: non-blocking note.

The decision is `approve`, `approve-with-follow-ups`, `request-changes`, or `blocked`. A review report records findings, evidence inspected, unanswered questions, and concrete recommendations.
