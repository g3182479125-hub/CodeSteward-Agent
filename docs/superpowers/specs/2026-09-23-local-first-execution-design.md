# Local-first Execution with FC Opt-in

## Status

- Date: 2026-09-23
- Status: implementation complete; DevPilot regression suite and reviews passed; ShopSphere end-to-end receipt issue remains
- Scope: DevPilot project inspection/test execution backend selection
- Related implementation: existing nanobot `exec` tool and DevPilot `ProjectTestRunner`

## Problem

DevPilot project tests currently go through the FC execution backend. This blocks
ordinary work when the provider cannot install dependencies or does not support a
project's test framework. In the ShopSphere exercise, FC dependency preparation
was denied network access and the test entry used Python `unittest`; the local
test suite was able to run successfully. Although nanobot has a local `exec`
tool, that path is not connected to the DevPilot project-test receipt workflow in
the affected runtime.

## Goal

Run ordinary project checks, dependency preparation and tests on the user's
machine by default. FC must be fully inactive by default, while remaining
available behind one explicit opt-in setting so the user can turn it on later.
The selected execution location must be observable in every receipt. The current
ChangeSet, Apply, Verify, Rollback and privacy-confirmation semantics are not
changed by this work.

## Non-goals

- Delete the FC adapter, provider credentials/configuration, or existing FC tests.
- Silently switch back to FC while the opt-in setting is off.
- Accept model-authored arbitrary test/install shell commands in DevPilot's
  fixed-profile project-test workflow.
- Remove project-root/path validation, execution receipts, dependency failure
  classification, or existing privacy prompts.
- Change ordinary nanobot `exec` configuration or redesign all unrelated tool
  execution.

## Design

### Backend selection

Reuse the existing `DEVPILOT_ENABLE_FC_SANDBOX` setting, defaulting to
`0`/false. With the default value, project test execution selects a local
execution adapter and must not instantiate, contact, prepare, or clean up an FC
sandbox. When explicitly set to `1`, the existing FC adapter remains selectable.
Invalid values fail configuration validation rather than silently enabling FC.
The setting is read by the server-side backend factory so WebUI and tool callers
cannot override it per request.

There is no automatic FC retry when local execution fails while FC is disabled.
The failure is returned as a local execution/preparation failure with an
actionable message. This makes “FC is off” an enforceable runtime property, not
just a UI label.

### Local execution adapter

Implement the local adapter behind the existing `ExecutionBackend` seam, so
`ProjectTestRunner`, Run persistence, failure evidence, retries and result
serialization retain one orchestration path. It executes only server-selected
profiles from the server-created project snapshot and in the resolved project
working directory. It returns the same structured execution facts (backend,
exit code, stdout/stderr subject to existing redaction/size limits, duration and
dependency preparation status); it does not claim to provide OS-level
isolation.

The first supported profiles include the existing Python/pytest and Node test
profiles plus Python standard-library `unittest`, needed for projects that do
not declare pytest. Profile selection and commands remain server-owned. Local
dependency preparation may access the network when required by a supported
project declaration, but must use a project-scoped environment and must keep
preparation failures distinct from test failures. It must not pass host secrets
or the full process environment into child commands.

### Runtime, records, and errors

Receipts identify `execution_location` (`local` or `fc`) and backend/profile.
Local receipts have no `sandbox_id`; FC receipts retain the existing sandbox ID.
Preparation and test execution remain separate phases. No non-zero, missing, or
unparseable exit status can be reported as success. A local failure is not
reclassified as a code defect unless a real test process ran and returned a
failure receipt.

Existing trust binding, allowed project roots, ChangeSet confirmation, Apply,
Verify, Rollback and privacy protections remain unchanged. “Local-first” means
where the process runs, not permission to exfiltrate private data or bypass the
existing project scope.

### Compatibility and migration

No database destructive migration is required. Additive receipt fields remain
backward-compatible for stored FC runs. Startup and health diagnostics disclose
the selected default backend and whether FC opt-in is enabled, without exposing
credentials. Existing FC-specific recovery behavior is used only for FC Runs;
local execution errors are not retried through FC while the setting is off.

## Verification and acceptance

1. With the setting unset and explicitly false, a test Run executes locally,
   records `execution_location=local`, and makes zero calls to FC construction,
   prepare, run, or cleanup.
2. The local adapter can execute passing/failing pytest, Node and unittest
   fixtures and records real exit status and output within existing limits.
3. A dependency preparation failure is reported as preparation failure and does
   not create a code-failure Run or claim tests ran.
4. With explicit opt-in true, the existing FC selection path and FC contract
   tests continue to work; without credentials, the off-by-default path still
   works and does not require FC configuration.
5. The runner rejects model-provided arbitrary commands and paths outside the
   validated snapshot, and starts children with a reduced environment. This is
   not an OS boundary: project/test code still runs with the current user's
   filesystem and network permissions; the receipt and UI must say so.
6. Existing DevPilot test, persistence, recovery, ChangeSet and nanobot tool
   integration regressions pass, and the final diff receives Standards and Spec
   subagent reviews as required by repository policy.

## Implementation and verification record

- FC Sandbox is disabled by default; only `DEVPILOT_ENABLE_FC_SANDBOX=1` (or
  the startup script's `-EnableFCSandbox` switch) enables the existing FC path.
- The local adapter runs fixed server-selected profiles against per-run project
  snapshots, uses bounded child processes, and reports that host permissions and
  network remain available. It does not claim OS-level isolation.
- Local Run and Verify receipts validate `execution_location`, backend,
  `sandbox_id`, and isolation attestations consistently. No local-to-FC fallback
  is performed.
- Full DevPilot test suite: passed; 5 skipped for Windows symlink privilege (4)
  and explicit live-FC opt-in (1). Focused local execution/control-plane tests,
  Ruff, targeted Pyright, startup-script Pester tests and `git diff --check` pass.
- Standards and Spec subagent reviews: PASS on the final implementation diff.
- A live FC provider test was intentionally not run; this change defaults FC
  off, and does not claim live cloud-provider acceptance.
- ShopSphere challenge through `ProjectTestRunner` reached local dependency
  preparation successfully (`execution_location=local`, no `sandbox_id`), but
  Run `e896cf25-6fb1-46a9-b018-4419c1011726` ended as
  `TEST_RECEIPT_INCOMPLETE`; no test exit code was accepted. The fixed
  `python -m unittest discover -v` output likely exceeded the 16 KiB receipt
  limit, which causes truncated output to be rejected. Confirm/fix this before
  calling the project-level end-to-end flow accepted.
- The already-running Control Plane still advertised only the FC backend, so
  this challenge was run through the current local backend code in an isolated
  temporary harness rather than through the stale live service. Reload/restart
  the integrated stack before a WebUI/Personal Agent acceptance run.

## Rollback

Set `DEVPILOT_ENABLE_FC_SANDBOX=1` to explicitly restore the existing FC
backend while diagnosing a local-backend issue. Reverting the backend-selection
change also restores the prior FC default, but the setting remains documented so
the user can choose deliberately.

## Operational warning

Local execution runs project and dependency code with the current user's OS
permissions and host-default network access. Do not run untrusted projects
locally. FC remains available only after explicit opt-in and valid local
configuration; it is not used as an automatic fallback.
