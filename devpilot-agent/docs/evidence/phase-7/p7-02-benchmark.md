# P7-02 Benchmark Evidence

Date: 2026-09-07
Candidate: `7465dacfa8c0a35a341b248e7564eb3e2b9ef1ce`
Baseline: `e8369151cba046007cc05621756ab702a1ec99f8`

## Scope

This is a deterministic workflow benchmark for the P7-01 task-profile runtime.
It does not claim that the candidate produces better natural-language answers.
It verifies that the same three bounded task intents consume the fixture,
produce the expected bounded proposal/evidence shape, route through the expected
profile, and enforce the safety boundary.

The benchmark uses the same fixed input contract for both versions:

| Task | Profile | Fixture | Expected boundary |
| --- | --- | --- | --- |
| Project explanation | `repo-analysis` | `devpilot-agent/tests/fixtures/python-project` | `devpilot_inspect_project` only |
| Small feature | `feature-delivery` | `devpilot-agent/tests/fixtures/python-project` | confirmed action required for code changes |
| Bug fix | `bug-fix` | `devpilot-agent/tests/fixtures/react-form-project` | confirmed action required for code changes |

Each task/version pair ran three repetitions: 18 runs total. The script is
`devpilot-agent/scripts/p7_02_benchmark.py`; the raw records are in
`p7-02-benchmark-results.json` in this directory.

## Results

| Version | Contract passes | Total runs | Manual interventions | Model/FC calls |
| --- | ---: | ---: | ---: | --- |
| Baseline | 0 | 9 | 0 | 0 / 0 |
| Candidate | 9 | 9 | 0 | 0 / 0 |

The baseline runs the same fixture probes but intentionally fails the task gate
for all three contracts because the selected pre-P7 commit has no trusted
task-profile runtime, isolated tool view, or session policy binding. Those
failures are retained in the raw JSON rather than being hidden.

The candidate completed all nine deterministic contract probes. Probe duration
was 65–70 ms per candidate run and 183–202 ms per baseline run on this machine;
these are local contract-check timings, not end-to-end LLM or FC latency.

## Reproduction

From `devpilot-agent`:

```powershell
uv run --no-sync python scripts/p7_02_benchmark.py --baseline e8369151cba046007cc05621756ab702a1ec99f8 --candidate 7465dacfa8c0a35a341b248e7564eb3e2b9ef1ce --repetitions 3 --output docs/evidence/phase-7/p7-02-benchmark-results.json
```

The benchmark reads the commit-pinned task-profile module and the fixed fixture,
creates proposals only in memory, and exercises the real Action Broker with a
missing confirmation reservation. It does not modify either fixture, invoke a
model, create a sandbox, or use a user repository. It therefore produces no
provider or FC cost.

## Acceptance conclusion

P7-02 passes its bounded acceptance scope: P7-01's routing and safety contracts
are reproducible at 9/9 candidate probes versus 0/9 on the selected pre-P7
baseline. No general claim is made about model quality, production latency, or
real sandbox execution from this offline benchmark.
