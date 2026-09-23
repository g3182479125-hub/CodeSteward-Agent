# Project-test persistent prepare evidence

Candidate under test: working tree after the bounded-recovery batch and before
the documentation commit that records this evidence.

## Contract

The FC adapter now separates environment preparation from a test Run:

- environment identity is derived from project ID, command/Profile, working
  directory, dependency Profile, and dependency fingerprint;
- only the provider sandbox ID and non-secret metadata are persisted under
  `DEVPILOT_FC_STATE_DIR\prepared`;
- a later Run may reuse the environment, including after constructing a new
  backend instance;
- a changed dependency fingerprint produces a different environment ID;
- expiry, provider query failure, profile mismatch, and cleanup failure are
  explicit errors; the adapter does not silently create a replacement or
  replay a test.

## Automated evidence

```text
pytest -q devpilot-agent/tests/test_fc_sandbox_contract.py devpilot-agent/tests/test_agent_test_runs.py
57 passed
```

The contract tests cover reuse across two Runs, reconnect after a new backend
instance, and dependency-fingerprint invalidation. The full DevPilot suite
also passed with exit code `0` and three skips.

## Real FC evidence

Configuration used the pre-existing local FC environment variables with the
secret value omitted from all output:

- backend: `fc-agent-sandbox`
- template: `code-interpreter-v1`
- network policy: `provider-default` (network isolation is not claimed)
- `sandbox-check`: passed, exit code `0`
- persistent check: one prepare, two test Runs, then explicit cleanup
- prepared sandbox: `sbx-8f784772-1e47-4621-ab1e-357b2d66cfe0`
- first Run exit code: `0`
- second Run exit code: `0`
- cleanup status: `cleaned`

The real check uploaded only an in-memory one-line Python fixture and did not
read or modify a local project. Provider disconnect/timeout injection was not
performed; that remains the next real-provider acceptance item.
