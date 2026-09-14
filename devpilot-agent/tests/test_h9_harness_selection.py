from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

from devpilot.harness_selection import HarnessSelectionError, HarnessSelectionStore


def _cases(*, baseline_target: bool, candidate_target: bool) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for category in ("target", "holdout", "regression"):
        for scenario in (1, 2):
            for version in ("baseline", "candidate"):
                for repeat in (1, 2):
                    success = category != "target" or (
                        baseline_target if version == "baseline" else candidate_target
                    )
                    cases.append(
                        {
                            "case_id": f"{category}:{scenario}:{version}:{repeat}",
                            "category": category,
                            "scenario": scenario,
                            "version": version,
                            "repeat": repeat,
                            "fixture_hash": hashlib.sha256(
                                json.dumps(
                                    {"case": f"{category}-{scenario}"},
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode()
                            ).hexdigest(),
                            "success": success,
                            "contract_passed": True,
                            "side_effect_count": 0,
                            "permission_violation": False,
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "calls": 1,
                            },
                        }
                    )
    return cases


def _store(tmp_path) -> HarnessSelectionStore:
    path = tmp_path / "devpilot.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "create table harness_selection_records ("
            "record_id text primary key, record_type text not null, payload_json text not null, "
            "record_mac text not null, created_at text not null)"
        )
    return HarnessSelectionStore(path, integrity_key=b"h9-test-key")


def _evidence(store: HarnessSelectionStore) -> tuple[str, ...]:
    return (
        store.record_evidence(
            task_id="task-1",
            project_id="project-1",
            workspace="E:/fixture",
            version_hash="a" * 64,
            source_type="tool_result",
            summary="fixed fixture result",
        ),
    )


def test_h9_promotes_only_a_strictly_better_loop_candidate(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="loop",
        target_id="default-react",
        baseline_version="1",
        candidate_version="2",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-1",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )

    report = store._record_evaluation(
        candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )

    assert report.eligible is True
    assert report.total_runs == 24
    assert store.get_candidate(candidate.candidate_id).status == "pending_confirmation"


def test_h9_never_promotes_a_candidate_that_regresses_or_reuses_an_invalid_suite(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="plugin",
        target_id="formatter",
        baseline_version="1.0.0",
        candidate_version="1.1.0",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-1",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    cases = _cases(baseline_target=False, candidate_target=True)
    cases[-1]["success"] = False

    report = store._record_evaluation(candidate.candidate_id, cases)

    assert report.eligible is False
    assert store.get_candidate(candidate.candidate_id).status == "evaluation_failed"


def test_h9_consumes_one_confirmation_for_the_exact_evaluation_and_scope(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="loop",
        target_id="default-react",
        baseline_version="1",
        candidate_version="2",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-1",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    report = store._record_evaluation(
        candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id, user_id="user-1", session_id="session-1"
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _item: store.consume_confirmation(
                    confirmation.confirmation_id,
                    user_id="user-1",
                    session_id="session-1",
                    evaluation_id=report.evaluation_id,
                ),
                range(2),
            )
        )

    assert [result.status for result in results].count("active") == 1
    assert [result.status for result in results].count("already_consumed") == 1
    with pytest.raises(HarnessSelectionError, match="scope"):
        store.consume_confirmation(
            confirmation.confirmation_id,
            user_id="user-1",
            session_id="other-session",
            evaluation_id=report.evaluation_id,
        )


def test_h9_confirmation_expires_and_retired_candidates_invalidate_pending_confirmations(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="loop",
        target_id="default-react",
        baseline_version="1",
        candidate_version="2",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-confirmation-lifecycle",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    report = store._record_evaluation(
        candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id, user_id="user-1", session_id="session-1"
    )
    expired = replace(confirmation, expires_at="2000-01-01T00:00:00+00:00")
    store._write(confirmation.confirmation_id, "confirmation", asdict(expired))
    with pytest.raises(HarnessSelectionError, match="expired"):
        store.consume_confirmation(
            confirmation.confirmation_id,
            user_id="user-1",
            session_id="session-1",
            evaluation_id=report.evaluation_id,
        )

    fresh_candidate = store.create_candidate(
        kind="loop",
        target_id="default-react",
        baseline_version="1",
        candidate_version="3",
        baseline_hash="a" * 64,
        candidate_hash="c" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-confirmation-invalidation",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    fresh_report = store._record_evaluation(
        fresh_candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )
    fresh_confirmation = store.issue_confirmation(
        fresh_candidate.candidate_id, user_id="user-1", session_id="session-1"
    )
    assert store.retire_candidate(fresh_candidate.candidate_id).status == "retired"
    with pytest.raises(HarnessSelectionError, match="no longer valid"):
        store.consume_confirmation(
            fresh_confirmation.confirmation_id,
            user_id="user-1",
            session_id="session-1",
            evaluation_id=fresh_report.evaluation_id,
        )


def test_h9_rejects_a_replayed_confirmation_after_its_candidate_binding_changes(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="loop",
        target_id="default-react",
        baseline_version="1",
        candidate_version="2",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-replayed-confirmation",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    report = store._record_evaluation(
        candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id, user_id="user-1", session_id="session-1"
    )
    store.consume_confirmation(
        confirmation.confirmation_id,
        user_id="user-1",
        session_id="session-1",
        evaluation_id=report.evaluation_id,
    )
    changed = replace(store.get_candidate(candidate.candidate_id), candidate_hash="c" * 64)
    store._write(candidate.candidate_id, "candidate", asdict(changed))

    with pytest.raises(HarnessSelectionError, match="changed"):
        store.consume_confirmation(
            confirmation.confirmation_id,
            user_id="user-1",
            session_id="session-1",
            evaluation_id=report.evaluation_id,
        )


def test_h9_rejects_confirmation_when_the_bound_candidate_changes(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="loop",
        target_id="default-react",
        baseline_version="1",
        candidate_version="2",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-bound",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    report = store._record_evaluation(
        candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id, user_id="user-1", session_id="session-1"
    )
    changed = replace(store.get_candidate(candidate.candidate_id), candidate_hash="c" * 64)
    store._write(candidate.candidate_id, "candidate", asdict(changed))

    with pytest.raises(HarnessSelectionError, match="changed"):
        store.consume_confirmation(
            confirmation.confirmation_id,
            user_id="user-1",
            session_id="session-1",
            evaluation_id=report.evaluation_id,
        )


def test_h9_rejects_a_fixed_suite_when_the_user_budget_cannot_fund_it(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="plugin",
        target_id="formatter",
        baseline_version="1",
        candidate_version="2",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-budget",
        budget={"max_candidates": 2, "max_evaluations": 23, "max_tokens": 1000},
    )

    with pytest.raises(HarnessSelectionError, match="cannot fund"):
        store._record_evaluation(
            candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
        )


def test_h9_rejects_a_second_active_candidate_for_the_same_scope_and_kind(tmp_path) -> None:
    store = _store(tmp_path)
    common = {
        "kind": "loop",
        "target_id": "default-react",
        "baseline_version": "1",
        "baseline_hash": "a" * 64,
        "scope": {"project_id": "project-1", "workspace": "E:/fixture"},
        "evidence_refs": _evidence(store),
        "round_id": "round-conflict",
        "budget": {"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    }
    first = store.create_candidate(
        **common, candidate_version="2", candidate_hash="b" * 64
    )
    second = store.create_candidate(
        **common, candidate_version="3", candidate_hash="c" * 64
    )
    first_report = store._record_evaluation(
        first.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )
    first_confirmation = store.issue_confirmation(
        first.candidate_id, user_id="user-1", session_id="session-1"
    )
    store.consume_confirmation(
        first_confirmation.confirmation_id,
        user_id="user-1",
        session_id="session-1",
        evaluation_id=first_report.evaluation_id,
    )
    second_report = store._record_evaluation(
        second.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )
    second_confirmation = store.issue_confirmation(
        second.candidate_id, user_id="user-1", session_id="session-1"
    )

    with pytest.raises(HarnessSelectionError, match="already owns"):
        store.consume_confirmation(
            second_confirmation.confirmation_id,
            user_id="user-1",
            session_id="session-1",
            evaluation_id=second_report.evaluation_id,
        )


def test_h9_cannot_retire_a_version_with_a_recoverable_task_reference(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="loop",
        target_id="default-react",
        baseline_version="1",
        candidate_version="2",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-1",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    report = store._record_evaluation(
        candidate.candidate_id,
        _cases(baseline_target=False, candidate_target=True),
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id, user_id="user-1", session_id="s-1"
    )
    active = store.consume_confirmation(
        confirmation.confirmation_id,
        user_id="user-1",
        session_id="s-1",
        evaluation_id=report.evaluation_id,
    )
    store.record_task_reference(active.candidate_id, task_id="task-1", recoverable=True)

    with pytest.raises(HarnessSelectionError, match="recoverable"):
        store.retire_candidate(active.candidate_id)

    store.record_task_reference(active.candidate_id, task_id="task-1", recoverable=False)
    assert store.retire_candidate(active.candidate_id).status == "retired"


def test_h9_retirement_serializes_with_a_concurrent_task_reference(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="loop",
        target_id="default-react",
        baseline_version="1",
        candidate_version="2",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-retirement-race",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    report = store._record_evaluation(
        candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id, user_id="user-1", session_id="s-1"
    )
    active = store.consume_confirmation(
        confirmation.confirmation_id,
        user_id="user-1",
        session_id="s-1",
        evaluation_id=report.evaluation_id,
    )

    entered = threading.Event()
    release = threading.Event()
    original_write = store._write_in_transaction

    def pause_retirement(
        connection: sqlite3.Connection,
        record_id: str,
        record_type: str,
        payload: dict[str, object],
    ) -> None:
        if record_type == "candidate" and payload.get("status") == "retired":
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("retirement test did not receive its release")
        original_write(connection, record_id, record_type, payload)

    store._write_in_transaction = pause_retirement  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as pool:
        retirement = pool.submit(store.retire_candidate, active.candidate_id)
        assert entered.wait(timeout=2)
        reference = pool.submit(
            store.record_task_reference,
            active.candidate_id,
            task_id="task-race",
            recoverable=True,
        )
        time.sleep(0.05)
        assert reference.done() is False
        release.set()
        assert retirement.result().status == "retired"
        with pytest.raises(HarnessSelectionError, match="only an active"):
            reference.result()


def test_h9_exposes_an_active_loop_only_for_new_tasks_in_its_exact_scope(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="loop",
        target_id="plan-then-act",
        baseline_version="default-react@1",
        candidate_version="1",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-1",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    report = store._record_evaluation(
        candidate.candidate_id,
        _cases(baseline_target=False, candidate_target=True),
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id, user_id="user-1", session_id="s-1"
    )
    store.consume_confirmation(
        confirmation.confirmation_id,
        user_id="user-1",
        session_id="s-1",
        evaluation_id=report.evaluation_id,
    )

    assert store.active_loop_driver({"project_id": "project-1", "workspace": "E:/fixture"}) == (
        "plan-then-act",
        "1",
    )
    assert store.active_loop_driver({"project_id": "project-2", "workspace": "E:/fixture"}) is None


def test_h9_rejects_a_third_candidate_in_the_same_selection_round(tmp_path) -> None:
    store = _store(tmp_path)
    arguments = {
        "kind": "plugin",
        "target_id": "formatter",
        "baseline_version": "1.0.0",
        "baseline_hash": "a" * 64,
        "scope": {"project_id": "project-1", "workspace": "E:/fixture"},
        "evidence_refs": _evidence(store),
        "round_id": "round-limited",
        "budget": {"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    }
    for version, digest in (("1.1.0", "b"), ("1.2.0", "c")):
        store.create_candidate(
            **arguments,
            candidate_version=version,
            candidate_hash=digest * 64,
        )

    with pytest.raises(HarnessSelectionError, match="candidate budget"):
        store.create_candidate(
            **arguments,
            candidate_version="1.3.0",
            candidate_hash="d" * 64,
        )


def test_h9_round_cannot_switch_to_a_different_plugin_or_driver_target(tmp_path) -> None:
    store = _store(tmp_path)
    arguments = {
        "kind": "plugin",
        "baseline_version": "1.0.0",
        "baseline_hash": "a" * 64,
        "scope": {"project_id": "project-1", "workspace": "E:/fixture"},
        "evidence_refs": _evidence(store),
        "round_id": "round-one-target",
        "budget": {"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    }
    store.create_candidate(
        **arguments,
        target_id="formatter",
        candidate_version="1.1.0",
        candidate_hash="b" * 64,
    )

    with pytest.raises(HarnessSelectionError, match="one plugin or loop Driver target"):
        store.create_candidate(
            **arguments,
            target_id="summarizer",
            candidate_version="1.1.0",
            candidate_hash="c" * 64,
        )


def test_h9_round_budget_cannot_be_reused_by_two_candidates(tmp_path) -> None:
    store = _store(tmp_path)
    arguments = {
        "kind": "plugin",
        "target_id": "formatter",
        "baseline_version": "1.0.0",
        "baseline_hash": "a" * 64,
        "scope": {"project_id": "project-1", "workspace": "E:/fixture"},
        "evidence_refs": _evidence(store),
        "round_id": "round-shared-budget",
        "budget": {"max_candidates": 2, "max_evaluations": 24, "max_tokens": 1000},
    }
    first = store.create_candidate(
        **arguments, candidate_version="1.1.0", candidate_hash="b" * 64
    )
    second = store.create_candidate(
        **arguments, candidate_version="1.2.0", candidate_hash="c" * 64
    )
    store._record_evaluation(
        first.candidate_id, _cases(baseline_target=False, candidate_target=True)
    )

    with pytest.raises(HarnessSelectionError, match="round evaluation budget"):
        store._record_evaluation(
            second.candidate_id, _cases(baseline_target=False, candidate_target=True)
        )


def test_h9_keeps_infrastructure_failure_out_of_candidate_evidence(tmp_path) -> None:
    store = _store(tmp_path)
    failed_evidence = store.record_evidence(
        task_id="task-1",
        project_id="project-1",
        workspace="E:/fixture",
        version_hash="a" * 64,
        source_type="tool_result",
        summary="FC unavailable",
        infrastructure_failure=True,
    )

    with pytest.raises(HarnessSelectionError, match="not verified"):
        store.create_candidate(
            kind="loop",
            target_id="plan-then-act",
            baseline_version="default-react@1",
            candidate_version="1",
            baseline_hash="a" * 64,
            candidate_hash="b" * 64,
            scope={"project_id": "project-1", "workspace": "E:/fixture"},
            evidence_refs=(failed_evidence,),
            round_id="round-1",
            budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
        )


def test_h9_fixed_runner_reproduces_a_preseeded_pure_calculation_improvement(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="plugin",
        target_id="discount",
        baseline_version="1.0.0",
        candidate_version="1.1.0",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-calculation",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )

    baseline_source = "def discount(price, percent):\n    return price - price * percent / 10\n"
    candidate_source = "def discount(price, percent):\n    return price - price * percent / 100\n"

    def fixed_trial(
        category: str, version: str, _scenario: int, _repeat: int, fixture: object
    ) -> dict[str, object]:
        assert isinstance(fixture, dict) and fixture["case"].startswith(category)
        namespace: dict[str, object] = {}
        exec(baseline_source if version == "baseline" else candidate_source, namespace)
        result = namespace["discount"](100, 10)
        return {
            "success": result == 90.0,
            "contract_passed": True,
            "side_effect_count": 0,
            "permission_violation": False,
            "output": "candidate output is not a durable H9 fact",
            "usage": {"input_tokens": 1, "output_tokens": 1, "calls": 1},
        }

    report = store.run_fixed_evaluation(candidate.candidate_id, fixed_trial)

    assert report.eligible is True
    assert report.total_runs == 24
    assert all("output" not in case for case in report.cases)


def test_h9_fixed_runner_records_infrastructure_failure_and_closes_candidate(tmp_path) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="plugin",
        target_id="discount",
        baseline_version="1.0.0",
        candidate_version="1.1.0",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-infrastructure-failure",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    attempts: list[tuple[str, str, int, int]] = []

    def broken_trial(
        category: str, version: str, scenario: int, repeat: int, _fixture: object
    ) -> dict[str, object]:
        attempts.append((category, version, scenario, repeat))
        raise RuntimeError("provider response must not be persisted")

    with pytest.raises(HarnessSelectionError, match="infrastructure failure"):
        store.run_fixed_evaluation(candidate.candidate_id, broken_trial)

    failed = store.get_candidate(candidate.candidate_id)
    assert failed.status == "evaluation_failed"
    assert failed.evaluation_id is not None
    evaluation = store.get_evaluation(failed.evaluation_id)
    assert evaluation is not None
    assert evaluation.status == "infrastructure_failure"
    assert evaluation.infrastructure_failure is True
    assert evaluation.failure_type == "RuntimeError"
    assert evaluation.total_runs == 1
    assert attempts == [("target", "baseline", 1, 1)]

    with pytest.raises(HarnessSelectionError, match="not pending evaluation"):
        store._record_evaluation(
            candidate.candidate_id, _cases(baseline_target=False, candidate_target=True)
        )


def test_h9_fixed_runner_rejects_a_malformed_host_result_as_infrastructure_failure(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    candidate = store.create_candidate(
        kind="plugin",
        target_id="discount",
        baseline_version="1.0.0",
        candidate_version="1.1.0",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": "project-1", "workspace": "E:/fixture"},
        evidence_refs=_evidence(store),
        round_id="round-malformed-result",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )

    def malformed_trial(
        _category: str, _version: str, _scenario: int, _repeat: int, _fixture: object
    ) -> dict[str, object]:
        return {"success": True, "contract_passed": True}

    with pytest.raises(HarnessSelectionError, match="infrastructure failure"):
        store.run_fixed_evaluation(candidate.candidate_id, malformed_trial)

    failed = store.get_candidate(candidate.candidate_id)
    assert failed.status == "evaluation_failed"
    assert failed.evaluation_id is not None
    evaluation = store.get_evaluation(failed.evaluation_id)
    assert evaluation is not None
    assert evaluation.infrastructure_failure is True
    assert evaluation.failure_type == "HarnessSelectionError"
    assert evaluation.total_runs == 1
