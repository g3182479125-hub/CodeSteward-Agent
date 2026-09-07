# P7-03 Standards Review

Date: 2026-09-07
Candidate: `b0a71faeb1027ad8dc8506a722ebc76c5112c590`

## Decision

**PASS**

## Evidence checked

- The integrated installer consumes both `nanobot/uv.lock` and
  `devpilot-agent/uv.lock`, and installs runtime requirements without the
  DevPilot development group.
- DevPilot uses ordinary `uvicorn`; the integrated environment keeps
  nanobot's `websockets=16.1.1` constraint instead of installing 17.1.
- The handoff binds migration to `DEVPILOT_DATABASE_URL`; the acceptance record
  contains isolated migration, backup/restore, serve, health, stop, and replay
  commands with exit/status evidence.
- The candidate has 14 DevPilot entry points, package-import evidence, fixed
  candidate SHA replay, secret-scan coverage, lock checks, and no credentials,
  databases, logs, virtual environments, or user fixtures in the delivery.
- P6 `provider-default` remains explicitly limited; no strict network-isolation
  claim is made.

## Non-blocking notes

Nanobot optional channel tests and Windows-inapplicable tests remain skipped as
recorded by the source regression command. The two user-modified Node fixture
files remain outside the candidate commit.
