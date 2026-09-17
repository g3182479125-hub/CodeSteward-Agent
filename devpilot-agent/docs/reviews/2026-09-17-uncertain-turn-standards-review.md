# Uncertain-turn recovery Standards review

- Fixed point: `64685b1`
- Candidate: `331beaa`
- Reviewer: Standards subagent `01a0ae3a-17a9-7be2-8039-92c31ab76536`
- Scope: `git diff 64685b1...331beaa`
- Verdict: **PASS**

## Checked

- A stale H7 marker no longer stops unrelated ordinary messages, read-only questions, or status/recovery turns before the Host builds a new turn.
- Exact old tool calls are blocked at the Host tool boundary. Legacy checkpoints without persisted fingerprints fail closed even when a temporary hash can be derived from old pending-call data.
- Recovery turns retain H8 persistence and CAS/owner-generation updates. A zero durable iteration budget cannot prevent the fresh inbound turn from starting, while the persisted budget never increases.
- A terminal result only retires the old tool-call intent when it matches the old business reference, there are no other unsettled business references, and there is exactly one old intent. Multiple or mixed unknown intents remain protected.
- `/new` retires the session H8 owner and clears the recovery marker.

## Evidence

```text
python -m pytest -q tests/agent/test_loop_save_turn.py tests/agent/test_loop_driver.py tests/agent/test_loop_handoff.py -k "not h9_persisted_loop_selection_starts_only_a_new_task_owner"
126 passed, 1 deselected

ruff check nanobot/agent/loop.py nanobot/agent/loop_driver.py nanobot/agent/loop_handoff.py nanobot/agent/runner.py tests/agent/test_loop_driver.py tests/agent/test_loop_handoff.py tests/agent/test_loop_save_turn.py
All checks passed

git diff --check
passed
```

The review found no remaining standards or replay-safety blocker in the final candidate.
