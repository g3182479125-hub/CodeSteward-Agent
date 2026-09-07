# P7-03 Acceptance Evidence

Date: 2026-09-07

## Environment and installation

The integrated installation path is `scripts/install-integrated.ps1`. It first
creates the nanobot environment from `nanobot/uv.lock`, exports runtime
requirements from `devpilot-agent/uv.lock` with `uv export --locked`, installs
those exact requirements into the nanobot environment, and finally installs the
DevPilot editable package without re-resolving dependencies.

The checked integration environment reported:

```text
nanobot=0.3.0
devpilot=0.1.0
devpilot_entry_points=14
imports=ok
```

The 14 discovered entries are the DevPilot inspect, search, read, proposal,
approval, delivery-report, run-state, watch, and confirmed-action tools declared
in `devpilot-agent/pyproject.toml`.

## Migration and backup/restore

In an isolated `local-data/p7-03-backup-check` directory, the control-plane
database migrated successfully through:

```text
0010_p5_persist_idempotency_key
```

The database file was copied to a separate
`local-data/p7-03-restore-check` directory. Querying `alembic_version` after the
copy returned the same value, `0010_p5_persist_idempotency_key`. The original
runtime data directory was not overwritten. These directories are local ignored
test data and are not part of the repository.

## Regression and safety checks

- DevPilot full suite in the integrated nanobot environment: `236 passed, 3 skipped`.
- nanobot source regression suite: `5866 passed, 42 skipped` (the nanobot test environment;
  the shared runtime environment is verified separately below).
- P7-02 fixed replay: 18 records; candidate 9/9; baseline 0/9.
- Integrated package check: 14 DevPilot entry points and both package imports passed.
- Ruff, secret scan, Markdown link check, and `git diff --check`: passed.
- The P7-02 benchmark and P7-03 delivery-contract tests passed.

The nanobot source regression suite was run in nanobot's test environment with
its development-only test dependencies. The runtime integration check is
separate and verifies the actual shared environment used to start nanobot and
load DevPilot tools; test-only extras are not required for normal user startup.

## Boundaries

The only user-facing interface remains nanobot's native WebUI. No independent
DevPilot page was added. API keys, `.env`, SQLite files, logs, virtual
environments, dependency directories, and user fixtures are excluded from the
delivery. P6's current FC `provider-default` limitation remains explicit: this
P7 node does not claim strict network isolation or a new sandbox capability.
