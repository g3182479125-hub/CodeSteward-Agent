from __future__ import annotations

import hashlib
import json
import multiprocessing
import threading
import time as pytime
from dataclasses import replace
from typing import Any

import pytest

from devpilot.candidate_runner import (
    CANDIDATE_MAX_OUTPUT_BYTES,
    CANDIDATE_MAX_TRIAL_SECONDS,
    CANDIDATE_MAX_TRIALS,
    CandidateExecutionResult,
    CandidateTrialGrant,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
    CandidateTrialRunner,
)
from devpilot.execution import ExecutionError, ExecutionResult
from devpilot.fc_sandbox import FCSandboxBackend
from devpilot.storage import ProjectStore

VALID_SOURCE = "def transform(payload):\n    return {'message': 'Hello, ' + payload['name']}\n"
ISSUER = CandidateTrialGrantIssuer(b"candidate-test-secret-0123456789012345")


def manifest_for(source: str = VALID_SOURCE, **overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "plugin_id": "model-greeting",
        "version": "1.0.0",
        "content_hash": hashlib.sha256(source.encode()).hexdigest(),
        "kind": "tool",
        "api_version": "nanobot.tool/v1",
        "scope": "session",
        "entry": "candidate.py",
        "requires": [],
        "provides": ["tool:model-greeting"],
        "requested_capabilities": [],
        "execution_mode": "fc_proxy",
    }
    manifest.update(overrides)
    return manifest


def request_for(
    *,
    source: str = VALID_SOURCE,
    input_data: object | None = None,
    task_id: str = "task-1",
    project_id: str = "project-1",
    trial_id: str = "trial-1",
    session_id: str = "session-1",
    grant: CandidateTrialGrant | None = None,
    **manifest_overrides: object,
) -> CandidateTrialRequest:
    return CandidateTrialRequest(
        task_id=task_id,
        project_id=project_id,
        trial_id=trial_id,
        session_id=session_id,
        grant=grant
        or ISSUER.issue(
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            allowed_input_rules=(("name", "string", 64, None, None),),
        ),
        manifest=manifest_for(source, **manifest_overrides),
        source=source,
        input_data=input_data if input_data is not None else {"name": "Alice"},
    )


def runner_for(
    backend: Any,
    *,
    store: MemoryIdempotencyStore | None = None,
    **kwargs: object,
) -> CandidateTrialRunner:
    return CandidateTrialRunner(
        backend,
        store=store or MemoryIdempotencyStore(),
        grant_issuer=ISSUER,
        **kwargs,
    )


class RecordingBackend(FCSandboxBackend):
    name = "fc-agent-sandbox"
    verification_available = True
    candidate_execution_available = True

    def __init__(
        self,
        result: ExecutionResult | None = None,
        error: Exception | None = None,
        cleanup_status: str = "cleaned",
    ):
        super().__init__(None)  # type: ignore[arg-type]
        self.result = result or ExecutionResult(
            backend=self.name,
            sandbox_id="candidate-sandbox",
            exit_code=0,
            stdout='{"message":"Hello, Alice"}',
            stderr="",
            duration_ms=12,
            command_profile="model-candidate",
            bundle_bytes=100,
            template="code-interpreter-v1",
            network_policy="provider-default",
        )
        self.error = error
        self.cleanup_status = cleanup_status
        self.requests: list[Any] = []
        self.cancelled: list[str] = []

    def candidate_execution_contract(self) -> tuple[str, str, str, str]:
        return (self.name, "model-candidate", "code-interpreter-v1", "provider-default")

    def run(self, request: Any) -> ExecutionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result

    def get_run_state(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "cleanup_status": self.cleanup_status,
            "status": "completed",
        }

    def cancel(self, run_id: str) -> dict[str, str]:
        self.cancelled.append(run_id)
        return {
            "run_id": run_id,
            "sandbox_id": "candidate-sandbox",
            "status": "cancelled",
            "cleanup_status": "cleaned",
        }


class MemoryIdempotencyStore:
    """Small test double for the ProjectStore idempotency contract."""

    def __init__(self) -> None:
        self.records: dict[str, tuple[str, str | None]] = {}
        self._lock = threading.Lock()

    def reserve_idempotency(self, key: str, request_hash: str) -> tuple[str, str | None]:
        existing = self.records.get(key)
        if existing is None:
            self.records[key] = (request_hash, None)
            return "new", None
        if existing[0] != request_hash:
            raise ValueError("idempotency key was reused with different content")
        if existing[1] is None:
            return "in_progress", None
        if key.startswith("candidate-operation-"):
            payload = json.loads(existing[1])
            if isinstance(payload, dict) and payload.get("status") == "in_progress":
                return "in_progress", existing[1]
        return "replay", existing[1]

    def complete_idempotency(self, key: str, response_json: str) -> None:
        request_hash, _ = self.records[key]
        self.records[key] = (request_hash, response_json)

    def compare_and_set_idempotency(
        self, key: str, expected_response_json: str | None, response_json: str
    ) -> bool:
        with self._lock:
            record = self.records.get(key)
            if record is None or record[1] != expected_response_json:
                return False
            self.records[key] = (record[0], response_json)
            return True

    def get_idempotency(self, key: str) -> tuple[str, str | None] | None:
        return self.records.get(key)

    def abort_idempotency(self, key: str) -> None:
        with self._lock:
            record = self.records.get(key)
            if record is not None and record[1] is None:
                del self.records[key]

    def reserve_candidate_budget(
        self,
        task_id: str,
        max_trials: int,
        max_trial_seconds: int,
        reservation_seconds: int,
        operation_id: str,
    ) -> tuple[bool, int, int]:
        key = "candidate-budget-" + hashlib.sha256(task_id.encode()).hexdigest()
        request_hash = hashlib.sha256(f"candidate-budget:{task_id}".encode()).hexdigest()
        with self._lock:
            record = self.records.get(key)
            if record is None:
                trials, reserved_seconds = 0, 0
                self.records[key] = (
                    request_hash,
                    json.dumps(
                        {
                            "active_operation_id": None,
                            "active_until_epoch": 0,
                            "lease_token": 0,
                            "trials": 0,
                            "reserved_seconds": 0,
                        },
                        sort_keys=True,
                    ),
                )
                active_operation_id = None
                lease_token = 0
            else:
                assert record[0] == request_hash
                payload = json.loads(record[1] or "")
                assert set(payload) == {
                    "active_operation_id",
                    "active_until_epoch",
                    "lease_token",
                    "trials",
                    "reserved_seconds",
                }
                trials = payload["trials"]
                reserved_seconds = payload["reserved_seconds"]
                active_operation_id = payload["active_operation_id"]
                lease_token = payload["lease_token"]
            if active_operation_id not in {None, operation_id}:
                return False, trials, reserved_seconds, lease_token
            if active_operation_id == operation_id:
                return True, trials, reserved_seconds, lease_token
            if trials >= max_trials or reserved_seconds + reservation_seconds > max_trial_seconds:
                return False, trials, reserved_seconds, lease_token
            updated = {
                "active_operation_id": operation_id,
                "active_until_epoch": 1,
                "lease_token": lease_token + 1,
                "trials": trials + 1,
                "reserved_seconds": reserved_seconds + reservation_seconds,
            }
            self.records[key] = (request_hash, json.dumps(updated, sort_keys=True))
            return True, updated["trials"], updated["reserved_seconds"], updated["lease_token"]

    def get_candidate_budget(self, task_id: str) -> tuple[int, int]:
        key = "candidate-budget-" + hashlib.sha256(task_id.encode()).hexdigest()
        record = self.records.get(key)
        if record is None:
            return 0, 0
        payload = json.loads(record[1] or "")
        assert set(payload) == {
            "active_operation_id",
            "active_until_epoch",
            "lease_token",
            "trials",
            "reserved_seconds",
        }
        return payload["trials"], payload["reserved_seconds"]

    def validate_candidate_lease(
        self, task_id: str, operation_id: str, lease_token: int
    ) -> bool:
        key = "candidate-budget-" + hashlib.sha256(task_id.encode()).hexdigest()
        record = self.records.get(key)
        if record is None:
            return False
        payload = json.loads(record[1] or "")
        return (
            payload.get("active_operation_id") == operation_id
            and payload.get("active_until_epoch") == 1
            and payload.get("lease_token") == lease_token
        )

    def settle_candidate_budget(
        self, task_id: str, operation_id: str, lease_token: int | None
    ) -> bool:
        key = "candidate-budget-" + hashlib.sha256(task_id.encode()).hexdigest()
        with self._lock:
            record = self.records.get(key)
            if record is None:
                return False
            payload = json.loads(record[1] or "")
            if payload.get("active_operation_id") != operation_id or (
                lease_token is not None and payload.get("lease_token") != lease_token
            ):
                return False
            payload["active_operation_id"] = None
            payload["active_until_epoch"] = 0
            self.records[key] = (record[0], json.dumps(payload, sort_keys=True))
            return True


class CrashBackend(RecordingBackend):
    def run(self, request: Any) -> ExecutionResult:
        self.requests.append(request)
        raise KeyboardInterrupt


class RecoverBackend(RecordingBackend):
    def get_run_state(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "completed",
            "sandbox_id": "recovered-sandbox",
            "cleanup_status": "cleaned",
            "result": {
                "backend": self.name,
                "sandbox_id": "recovered-sandbox",
                "exit_code": 0,
                "stdout": '{"message":"recovered"}',
                "stderr": "",
                "duration_ms": 7,
                "command_profile": "model-candidate",
                "bundle_bytes": 100,
                "region": "cn-hangzhou",
                "template": "code-interpreter-v1",
                "network_policy": "provider-default",
                "output_truncated": False,
            },
        }


def _reserve_budget_in_process(database_path: str, index: int) -> bool:
    store = ProjectStore(database_path)
    for _attempt in range(50):
        allowed, trials, seconds, _lease_token = store.reserve_candidate_budget(
            "shared-task", CANDIDATE_MAX_TRIALS, CANDIDATE_MAX_TRIAL_SECONDS, 30, f"op-{index}"
        )
        if allowed:
            store.settle_candidate_budget("shared-task", f"op-{index}", _lease_token)
            return True
        if trials >= CANDIDATE_MAX_TRIALS or seconds >= CANDIDATE_MAX_TRIAL_SECONDS:
            return False
        pytime.sleep(0.02)
    return False


def test_trial_runs_only_the_fixed_json_candidate_bundle() -> None:
    backend = RecordingBackend()

    result = runner_for(backend).trial(request_for())

    assert result.status == "passed"
    assert result.output == {"message": "Hello, Alice"}
    request = backend.requests[0]
    assert request.command == "python3 /workspace/devpilot/candidate_runner.py"
    assert request.command_profile == "model-candidate"
    assert request.network_allowed is False
    assert {item.relative_path for item in request.files} == {
        "candidate.py",
        "candidate_runner.py",
        "input.json",
    }
    assert b"E2B_API_KEY" not in b"".join(item.content for item in request.files)


def test_execute_uses_the_same_fc_proxy_path_as_trial() -> None:
    backend = RecordingBackend()

    result = runner_for(backend).execute(request_for())

    assert result.status == "passed"
    assert backend.requests[0].command_profile == "model-candidate"
    assert backend.requests[0].network_allowed is False


@pytest.mark.parametrize(
    ("source", "manifest_overrides", "error_code"),
    [
        (
            "import os\ndef transform(payload):\n    return payload\n",
            {},
            "CANDIDATE_SOURCE_FORBIDDEN",
        ),
        ("def other(payload):\n    return payload\n", {}, "CANDIDATE_ENTRY_INVALID"),
        (
            VALID_SOURCE,
            {"execution_mode": "in_process"},
            "CANDIDATE_EXECUTION_MODE_INVALID",
        ),
        (
            VALID_SOURCE,
            {"requested_capabilities": ["filesystem.write"]},
            "CANDIDATE_CAPABILITY_DENIED",
        ),
        (VALID_SOURCE, {"requires": ["service:search@v1"]}, "CANDIDATE_DEPENDENCY_DENIED"),
        (VALID_SOURCE, {"scope": "global"}, "CANDIDATE_SCOPE_INVALID"),
    ],
)
def test_candidate_contract_rejects_unsafe_or_non_pure_definitions(
    source: str, manifest_overrides: dict[str, object], error_code: str
) -> None:
    backend = RecordingBackend()
    result = runner_for(backend).trial(request_for(source=source, **manifest_overrides))

    assert result.status == "rejected"
    assert result.error_code == error_code
    assert backend.requests == []


def test_candidate_content_hash_must_match_before_backend_call() -> None:
    backend = RecordingBackend()
    result = runner_for(backend).trial(request_for(content_hash="0" * 64))

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_HASH_MISMATCH"
    assert backend.requests == []


def test_backend_is_required_and_never_falls_back_to_host_execution() -> None:
    result = runner_for(None).trial(request_for())

    assert result.status == "failed"
    assert result.error_code == "CANDIDATE_BACKEND_UNAVAILABLE"
    assert result.output is None


def test_candidate_requires_a_signed_host_grant() -> None:
    request = request_for()
    assert request.grant is not None
    unsigned = replace(request, grant=replace(request.grant, signature="tampered"))

    result = runner_for(RecordingBackend()).trial(unsigned)

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_GRANT_INVALID"


@pytest.mark.parametrize("input_data", [{"name": "A" * 65}, {"name": 123}])
def test_host_grant_enforces_value_level_input_rules(input_data: object) -> None:
    result = runner_for(RecordingBackend()).trial(request_for(input_data=input_data))

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_INPUT_SCOPE_INVALID"


def test_fc_backend_contract_is_checked_before_budget_or_execution() -> None:
    class UnboundFCBackend(RecordingBackend):
        candidate_execution_contract = None

    backend = UnboundFCBackend()
    result = runner_for(backend).trial(request_for())

    assert result.error_code == "CANDIDATE_BACKEND_CONTRACT_INVALID"
    assert backend.requests == []


def test_fc_result_receipt_must_match_the_fixed_candidate_contract() -> None:
    backend = RecordingBackend()
    backend.result = replace(backend.result, network_policy="required")

    result = runner_for(backend).trial(request_for())

    assert result.error_code == "CANDIDATE_RESULT_CONTRACT_INVALID"
    assert result.status == "failed"


def test_numeric_input_must_stay_inside_the_host_signed_range() -> None:
    source = "def transform(payload):\n    return {'age': payload['age']}\n"
    grant = ISSUER.issue(
        session_id="session-1",
        task_id="task-1",
        project_id="project-1",
        allowed_input_rules=(("age", "integer", 8, 0, 120),),
    )

    result = runner_for(RecordingBackend()).trial(
        request_for(source=source, input_data={"age": 121}, grant=grant)
    )

    assert result.error_code == "CANDIDATE_INPUT_SCOPE_INVALID"
    assert result.status == "rejected"


def test_candidate_source_cannot_reference_a_local_path() -> None:
    source = "def transform(payload):\n    return {'path': 'C:/Users/private'}\n"

    result = runner_for(RecordingBackend()).trial(request_for(source=source))

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_SOURCE_LOCAL_REFERENCE"


def test_candidate_requires_host_grant_and_rejects_unapproved_input() -> None:
    runner = runner_for(RecordingBackend())

    missing_grant = runner.trial(replace(request_for(), grant=None))
    out_of_scope = runner.trial(request_for(input_data={"email": "a@example.com"}))

    assert missing_grant.error_code == "CANDIDATE_GRANT_REQUIRED"
    assert out_of_scope.error_code == "CANDIDATE_INPUT_SCOPE_INVALID"


def test_candidate_rejects_secrets_in_source_and_input_values() -> None:
    source = "def transform(payload):\n    return {'value': 'sk-12345678901234567890'}\n"
    source_result = runner_for(RecordingBackend()).trial(request_for(source=source))
    input_result = runner_for(RecordingBackend()).trial(
        request_for(input_data={"name": "C:\\private\\fixture"})
    )

    assert source_result.error_code == "CANDIDATE_SOURCE_SENSITIVE"
    assert input_result.error_code == "CANDIDATE_INPUT_SENSITIVE"


def test_non_fc_backend_and_unconfirmed_cleanup_cannot_report_success() -> None:
    from devpilot.execution import FakeExecutionBackend

    non_fc = runner_for(FakeExecutionBackend()).trial(request_for())
    pending_cleanup = runner_for(RecordingBackend(cleanup_status="pending")).trial(
        request_for()
    )

    assert non_fc.error_code == "CANDIDATE_BACKEND_UNAVAILABLE"
    assert pending_cleanup.error_code == "CANDIDATE_CLEANUP_UNCONFIRMED"


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (ExecutionError("FC_TIMEOUT", "FC Sandbox execute timed out"), "FC_TIMEOUT"),
        (
            ExecutionError("FC_SANDBOX_CREATE_FAILED", "FC Sandbox create failed"),
            "FC_SANDBOX_CREATE_FAILED",
        ),
        (RuntimeError("local exception must not leak"), "CANDIDATE_EXECUTION_FAILED"),
    ],
)
def test_backend_failures_are_explicit_and_not_retried(error: Exception, error_code: str) -> None:
    backend = RecordingBackend(error=error)
    result = runner_for(backend).trial(request_for())

    assert result.status == "failed"
    assert result.error_code == error_code
    assert len(backend.requests) == 1
    assert "local exception" not in result.error_message


def test_nonzero_exit_is_not_a_success() -> None:
    backend = RecordingBackend(
        result=ExecutionResult(
            backend="fc-agent-sandbox",
            sandbox_id="candidate-sandbox",
            exit_code=2,
            stdout="",
            stderr="candidate failed",
            duration_ms=4,
            command_profile="model-candidate",
            bundle_bytes=100,
            template="code-interpreter-v1",
            network_policy="provider-default",
        )
    )

    result = runner_for(backend).trial(request_for())

    assert result.status == "failed"
    assert result.error_code == "CANDIDATE_EXECUTION_FAILED"
    assert result.exit_code == 2


def test_invalid_or_truncated_json_output_is_rejected() -> None:
    invalid = RecordingBackend(
        result=ExecutionResult(
            backend="fc-agent-sandbox",
            sandbox_id="candidate-sandbox",
            exit_code=0,
            stdout="not-json",
            stderr="",
            duration_ms=1,
            command_profile="model-candidate",
            bundle_bytes=100,
            template="code-interpreter-v1",
            network_policy="provider-default",
        )
    )
    truncated = RecordingBackend(
        result=ExecutionResult(
            backend="fc-agent-sandbox",
            sandbox_id="candidate-sandbox",
            exit_code=0,
            stdout="{}",
            stderr="",
            duration_ms=1,
            command_profile="model-candidate",
            bundle_bytes=100,
            output_truncated=True,
            template="code-interpreter-v1",
            network_policy="provider-default",
        )
    )

    assert (
        runner_for(invalid).trial(request_for()).error_code == "CANDIDATE_OUTPUT_INVALID"
    )
    assert (
        runner_for(truncated).trial(request_for()).error_code
        == "CANDIDATE_OUTPUT_LIMIT_EXCEEDED"
    )


def test_fixed_runner_output_limit_is_reported_as_a_bounded_failure() -> None:
    backend = RecordingBackend(
        result=ExecutionResult(
            backend="fc-agent-sandbox",
            sandbox_id="candidate-sandbox",
            exit_code=86,
            stdout="",
            stderr="",
            duration_ms=1,
            command_profile="model-candidate",
            bundle_bytes=100,
            template="code-interpreter-v1",
            network_policy="provider-default",
        )
    )

    result = runner_for(backend).trial(request_for())

    assert result.error_code == "CANDIDATE_OUTPUT_LIMIT_EXCEEDED"
    assert result.output_truncated is True


def test_input_is_json_only_and_sensitive_fields_are_not_uploaded() -> None:
    runner = runner_for(RecordingBackend())

    result = runner.trial(request_for(input_data={"api_key": "secret"}))

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_INPUT_SENSITIVE"


def test_nested_sensitive_fields_are_rejected_even_under_non_string_keys() -> None:
    result = runner_for(RecordingBackend()).trial(
        request_for(input_data={0: {"nested_api_key": "secret"}})
    )

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_INPUT_SENSITIVE"


def test_trial_budget_is_shared_across_concurrent_calls() -> None:
    backend = RecordingBackend()
    runner = runner_for(backend)

    results = [
        runner.trial(
            request_for(
                task_id="task-budget",
                project_id="project-1",
                trial_id=f"trial-{index}",
                session_id="session-budget",
                input_data={"name": "Alice"},
            )
        )
        for index in range(CANDIDATE_MAX_TRIALS + 1)
    ]

    assert [result.status for result in results] == ["passed"] * CANDIDATE_MAX_TRIALS + ["failed"]
    assert results[-1].error_code == "CANDIDATE_BUDGET_EXCEEDED"
    assert len(backend.requests) == CANDIDATE_MAX_TRIALS


def test_live_execution_uses_a_separate_task_budget() -> None:
    backend = RecordingBackend()
    store = MemoryIdempotencyStore()
    runner = runner_for(backend, store=store)

    for index in range(CANDIDATE_MAX_TRIALS):
        result = runner.trial(request_for(trial_id=f"trial-{index}"))
        assert result.status == "passed"

    live = replace(request_for(trial_id="call-1"), execution_mode="live")
    result = runner.execute(live)

    assert result.status == "passed"
    assert store.get_candidate_budget("task-1")[0] == CANDIDATE_MAX_TRIALS
    assert store.get_candidate_budget("candidate-live:task-1")[0] == 1


def test_trial_timeout_and_output_limits_are_bound_into_request() -> None:
    backend = RecordingBackend()
    request = request_for()

    result = runner_for(backend).trial(request)

    sent = backend.requests[0]
    assert result.status == "passed"
    assert sent.timeout_seconds == 30
    assert sent.max_output_bytes == CANDIDATE_MAX_OUTPUT_BYTES
    assert CANDIDATE_MAX_TRIAL_SECONDS == 120


def test_result_is_safe_to_serialize_and_records_cleanup_state() -> None:
    backend = RecordingBackend()
    backend.get_run_state = lambda _run_id: {"cleanup_status": "cleaned"}  # type: ignore[attr-defined]

    result = runner_for(backend).trial(request_for())
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.cleanup_status == "cleaned"
    assert "source_code" not in serialized
    assert "input_data" not in serialized
    assert "source_hash" in serialized
    assert isinstance(result, CandidateExecutionResult)


def test_candidate_output_is_redacted_before_result_persistence() -> None:
    backend = RecordingBackend(
        result=ExecutionResult(
            backend="fc-agent-sandbox",
            sandbox_id="candidate-sandbox",
            exit_code=0,
            stdout='{"api_key":"secret","message":"ok"}',
            stderr="",
            duration_ms=1,
            command_profile="model-candidate",
            bundle_bytes=100,
            template="code-interpreter-v1",
            network_policy="provider-default",
        )
    )

    result = runner_for(backend).trial(request_for())

    assert result.status == "passed"
    assert result.output == {"api_key": "[REDACTED]", "message": "ok"}


def test_persisted_result_is_replayed_after_runner_restart() -> None:
    store = MemoryIdempotencyStore()
    grant = request_for().grant
    first_backend = RecordingBackend()
    first = runner_for(first_backend, store=store).trial(request_for(grant=grant))

    second_backend = RecordingBackend()
    second = runner_for(second_backend, store=store).trial(request_for(grant=grant))

    assert first.status == "passed"
    assert second == first
    assert second_backend.requests == []


def test_persisted_candidate_record_requires_scope_and_operation_hash() -> None:
    store = MemoryIdempotencyStore()
    request = request_for()
    first = runner_for(RecordingBackend(), store=store).trial(request)
    assert first.status == "passed"

    operation_key = runner_for(RecordingBackend(), store=store)._operation_key(request)
    request_hash, response_json = store.records[operation_key]
    payload = json.loads(response_json or "")
    payload.pop("operation_hash")
    store.records[operation_key] = (request_hash, json.dumps(payload, sort_keys=True))

    result = runner_for(RecordingBackend(), store=store).trial(request)

    assert result.status == "failed"
    assert result.error_code == "CANDIDATE_PERSISTENCE_INVALID"


def test_persisted_budget_survives_runner_restart() -> None:
    store = MemoryIdempotencyStore()
    grant = request_for().grant
    first_backend = RecordingBackend()
    first_runner = runner_for(first_backend, store=store)
    assert first_runner.trial(request_for(grant=grant)).status == "passed"

    second_backend = RecordingBackend()
    second_runner = runner_for(second_backend, store=store)
    for index in range(2, CANDIDATE_MAX_TRIALS + 1):
        request = replace(
            request_for(grant=grant),
            trial_id=f"trial-{index}",
        )
        assert second_runner.trial(request).status == "passed"

    exceeded = second_runner.trial(replace(request_for(grant=grant), trial_id="trial-over"))

    assert exceeded.status == "failed"
    assert exceeded.error_code == "CANDIDATE_BUDGET_EXCEEDED"
    assert len(first_backend.requests) + len(second_backend.requests) == CANDIDATE_MAX_TRIALS


def test_operation_progress_can_be_queried_cancelled_and_persisted() -> None:
    store = MemoryIdempotencyStore()
    request = request_for()
    with pytest.raises(KeyboardInterrupt):
        runner_for(CrashBackend(), store=store).trial(request)

    cancel_backend = RecordingBackend()
    runner = runner_for(cancel_backend, store=store)
    progress = runner.operation_status(request)
    cancelled = runner.cancel(request)

    assert progress["status"] == "in_progress"
    assert cancelled.status == "cancelled"
    assert cancel_backend.cancelled
    assert runner.operation_status(request)["status"] == "cancelled"


def test_cancel_requires_provider_confirmation_before_completing_operation() -> None:
    class UnconfirmedCancelBackend(RecordingBackend):
        def cancel(self, run_id: str) -> dict[str, str]:
            self.cancelled.append(run_id)
            return {
                "run_id": run_id,
                "sandbox_id": "candidate-sandbox",
                "status": "cancelled",
                "cleanup_status": "pending",
            }

    store = MemoryIdempotencyStore()
    request = request_for()
    with pytest.raises(KeyboardInterrupt):
        runner_for(CrashBackend(), store=store).trial(request)

    result = runner_for(UnconfirmedCancelBackend(), store=store).cancel(request)

    assert result.status == "unknown"
    assert result.error_code == "CANDIDATE_CANCEL_UNCONFIRMED"
    assert (
        runner_for(UnconfirmedCancelBackend(), store=store).operation_status(request)["status"]
        == "in_progress"
    )


def test_recover_uses_provider_state_without_starting_a_second_run() -> None:
    store = MemoryIdempotencyStore()
    request = request_for()
    with pytest.raises(KeyboardInterrupt):
        runner_for(CrashBackend(), store=store).trial(request)

    backend = RecoverBackend()
    result = runner_for(backend, store=store).recover(request)

    assert result.status == "passed"
    assert result.output == {"message": "recovered"}
    assert result.run_id is not None
    assert backend.requests == []


def test_project_store_budget_survives_reload_and_fails_closed_on_corruption(tmp_path: Any) -> None:
    store = ProjectStore(tmp_path / "devpilot.db")
    store.initialize()

    assert store.reserve_candidate_budget("task", 2, 60, 30, "op-1")[0] is True
    assert store.settle_candidate_budget("task", "op-1", 1) is True
    assert store.reserve_candidate_budget("task", 2, 60, 30, "op-2")[0] is True
    assert store.settle_candidate_budget("task", "op-2", 2) is True
    assert store.reserve_candidate_budget("task", 2, 60, 30, "op-3")[0] is False

    reopened = ProjectStore(tmp_path / "devpilot.db")
    assert reopened.get_candidate_budget("task") == (2, 60)
    key = reopened._candidate_budget_key("task")
    reopened.complete_idempotency(key, "not-json")

    with pytest.raises(ValueError, match="candidate budget state"):
        reopened.get_candidate_budget("task")


def test_project_store_budget_is_atomic_across_processes(tmp_path: Any) -> None:
    database_path = str(tmp_path / "concurrent.db")
    store = ProjectStore(database_path)
    store.initialize()
    context = multiprocessing.get_context("spawn")

    with context.Pool(CANDIDATE_MAX_TRIALS + 2) as pool:
        results = pool.starmap(
            _reserve_budget_in_process,
            [(database_path, index) for index in range(CANDIDATE_MAX_TRIALS + 2)],
        )

    assert sum(results) == CANDIDATE_MAX_TRIALS
    assert store.get_candidate_budget("shared-task") == (
        CANDIDATE_MAX_TRIALS,
        CANDIDATE_MAX_TRIALS * 30,
    )
