# Local-Only Harness Demonstration and Temporary Tool Validation

## Status

Design approved by the user on 2026-09-27 for implementation planning. FC remains disabled. Skill self-evolution is expressly outside this scope.

## Goal

Make the non-Skill-evolution Harness capabilities demonstrable with the integrated local Agent while preserving one running nanobot AgentLoop and keeping FC completely unused. The demonstration must show live Skill discovery/content refresh, session-scoped Tool attach/call/detach, built-in Loop Driver handoff, and the existing ShopSphere ChangeSet delivery chain.

The missing link is model-authored temporary Tool validation: its current activation contract requires an FC validation receipt, so it cannot run with FC disabled. Add an explicit local validation path, then demonstrate the candidate lifecycle through the actual WebUI Agent rather than substituting direct edits or fabricated receipts.

## Existing facts and reuse

- `ContextBuilder.build_system_prompt()` obtains current Skills from `SkillsLoader` each time it assembles model context; Skill files are read from disk rather than held as immutable process-start snapshots.
- `PluginManager` and `DynamicPluginRegistry` already provide scoped live Tool routes, next-step schema refresh, leases, and cleanup. Reuse them; do not create a second registry or AgentLoop.
- H7/H8 already provide the stable Host and built-in `default-react` / `plan-then-act` Loop Drivers. Demonstrate Driver handoff at a committed step boundary; do not claim that Python code for the core Loop itself is hot-replaced.
- `TemporaryToolValidator`, `TemporaryToolValidateTool`, and `TemporaryToolActivateTool` currently build an FC backend directly. The registry accepts a matching FC validation receipt, and existing tests assert that policy.
- `LocalExecutionBackend` supports only server-fixed profiles and explicitly reports that local project code runs with the current OS user's permissions and host-default network. It is not an OS sandbox. Do not bypass its fixed-profile checks by adding arbitrary model-provided commands.
- Existing H1–H11 and P0–P8 nodes are complete and remain historical. This work is a new, bounded local-only acceptance extension; it does not rewrite their evidence or claim their old FC acceptance was local.

## Chosen approach

Use the existing local execution seam with a new server-owned `temporary-tool-contract` profile. The server constructs the complete validation request from the immutable candidate bundle and a fixed contract checker; the model cannot provide a command, executable, environment, backend, path, or dependency. The local receipt is explicitly marked `backend=local`, `execution_location=local`, and has no sandbox ID. The receipt is bound to the session, Run, project, manifest hash, source hash, checker version, and execution result. `PluginManager` accepts this local receipt only under the existing Full Access/WebSocket policy and only for that exact artifact and live session/Run.

The local checker and the subsequently activated candidate execute as ordinary local Python code under the current Windows user. A reduced child environment, temporary working directory, fixed input bundle, AST/contract checks, output caps, and timeout improve operational hygiene but do not create an OS security boundary and do not prevent arbitrary Python from reading user-accessible files or using host-default networking. Therefore this mode is a Full Access trust decision, not a sandbox or privacy enforcement mechanism. Never describe it as isolated, safe against malicious generated code, or guaranteed to intercept undeclared privacy access. Demo runs must use a disposable workspace and synthetic inputs, with no credentials or personal data present in scope.

Keep the existing FC adapter and its explicit opt-in untouched for future use, but this acceptance flow must verify that FC construction, prepare, run, and cleanup are never called while FC is off. There is no local-to-FC fallback.

## User-visible flow

1. In a disposable Full Access WebUI session, ask the Agent to inspect a small synthetic project.
2. During the same still-running AgentLoop, add or update a harmless test Skill in that session's workspace. On the next model step, the Skill summary/content must reflect the new file without restarting the Gateway, Loop, or conversation.
3. Ask for a capability unavailable from the base Tool set. The Agent records the gap, writes an immutable temporary Tool candidate, and submits it to the fixed local validator.
4. On a passing receipt, the existing PluginManager activates the exact version for the current session/Run; the next model step sees and calls the new Tool. A different session must not see it.
5. Update the candidate as a new version, verify the old call lease drains, and show the new version on the next step. Stop/remove the candidate and confirm it is no longer routable; end the Run and confirm temporary sources are cleaned up unless explicitly saved through the existing persistence confirmation.
6. While keeping the same Host process and user task identity, switch between the existing built-in Loop Drivers at a committed step boundary and continue from the checkpoint. Do not claim that a core `AgentLoop` Python implementation was replaced.
7. On the separate ShopSphere challenge copy, show defect reproduction, ChangeSet/Diff, the existing execution confirmation, local Apply+Verify, full local tests, and—only on a disposable copy—Rollback and hash verification.

## Interface and receipt contract

- The model-facing create/validate/activate/persist Tools retain their current schemas unless code inspection proves a field is required; all backend/profile/command decisions remain server-owned.
- The validation adapter accepts one immutable artifact and returns one typed result. It does not import candidate code in the host during validation.
- A local validation result is distinguishable from FC at every persistence and UI layer. It must never satisfy an API that specifically requires an FC attestation for another product path.
- Activation rechecks the artifact hash, manifest, session/Run/project binding, expiry, Full Access context, local validation version, and successful exit status immediately before registration.
- Candidate activation continues through `PluginManager` leases and `DynamicPluginRegistry`; cleanup, persistence confirmation, and failure recovery keep their existing lifecycle semantics.
- The presentation must say “本机执行（非 OS 沙箱；候选代码继承当前用户权限）”; it must not say “安全沙箱”“隐私已隔离” or imply that static inspection can guarantee the candidate's runtime behavior.

## Acceptance criteria

1. With FC disabled, a positive local temporary-Tool contract check produces a complete, durable, hash-bound local receipt and activates only the matching candidate.
2. Syntax, import, Tool schema, invalid-parameter, controlled-error, output-limit, timeout, stale-hash, expired-artifact, duplicate-name, and missing-receipt cases fail closed and leave no active route.
3. FC backend spies prove zero construction/contact/prepare/run/cleanup calls throughout local validate, activate, invoke, update, and cleanup.
4. The same active AgentLoop sees the updated Skill and newly activated Tool on the next model decision; no process/session restart occurs. Another session cannot see or call the candidate.
5. Version update, in-flight lease draining, stop/remove, Run-end cleanup, and explicit persistence confirmation remain observable and correct.
6. A live WebUI evidence run records process ID, session/Run IDs, candidate/version/source hashes, local receipt, Tool invocation, cleanup, and Loop Driver checkpoint transition. No synthetic receipt may be presented as live execution.
7. ShopSphere delivery evidence remains local, with the exact real ChangeSet and test receipts; rollback is exercised only on a disposable challenge copy.
8. The whole recording checklist explicitly excludes Skill self-evolution and all FC/cloud behavior; it distinguishes source-level support, automated tests, and real WebUI evidence.

## Non-goals and known limits

- Skill self-evolution, profile candidate generation/activation, Cron/Dream iteration, and all FC/cloud execution are excluded from the recording and this implementation batch.
- This does not replace the AgentLoop core code dynamically; it proves that Skills, Tools, and the selected built-in Driver can change while the stable Host/Loop remains alive.
- Local temporary Python code is not isolated from the current OS account. This mode is inappropriate for untrusted candidates or sensitive data. It does not preserve the old FC-proxy privacy guarantee; the user has chosen the Full Access local trust model for this demo.
- Do not modify general local project-test command allowlists to run arbitrary candidate commands. Do not silently enable FC or add an FC fallback.
- Do not modify ShopSphere's source manually; use the personal Agent's existing ChangeSet delivery chain for any repair.

## Test seams

- Public validator seam: immutable temporary artifact in → typed, persisted local/FC validation receipt out.
- Public plugin-manager seam: scoped session/project Tool view before/after activate, update, stop, and cleanup; observe schema and execution through the view.
- Context assembly seam: call the same `ContextBuilder` before/after a Skill file changes and compare the resulting system prompt.
- Live runtime seam: one WebUI session and process across the Skill/Tool/Driver changes; observe the next model step and another session's isolation.
- Project delivery seam: the Agent-visible ChangeSet/apply_verify/test_run results and final file hashes on the disposable ShopSphere challenge.

## Delivery and review

Implementation proceeds as one new acceptance batch after the user reviews this written specification. The batch must update the current plan with a new node instead of reopening completed H1–H11/P0–P8 history; add red/green tests at the seams above; run focused tests, affected nanobot/DevPilot regression suites, Ruff/type checks, and `git diff --check`; then dispatch parallel Standards and Spec subagent reviews against one fixed candidate per the repository's ADR-0004. Completion requires review PASS, zero FC calls, and the real WebUI evidence checklist—not only unit tests.
