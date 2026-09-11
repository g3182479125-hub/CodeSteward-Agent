from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from devpilot.candidate_runner import (
    CANDIDATE_MAX_OUTPUT_BYTES,
    CANDIDATE_MAX_TRIAL_SECONDS,
    CANDIDATE_MAX_TRIALS,
    CandidateExecutionResult,
    CandidateTrialRequest,
    CandidateTrialRunner,
)
from devpilot.execution import ExecutionError, ExecutionResult

VALID_SOURCE = "def transform(payload):\n    return {'message': 'Hello, ' + payload['name']}\n"


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
    **manifest_overrides: object,
) -> CandidateTrialRequest:
    return CandidateTrialRequest(
        task_id="task-1",
        project_id="project-1",
        trial_id="trial-1",
        manifest=manifest_for(source, **manifest_overrides),
        source=source,
        input_data=input_data if input_data is not None else {"name": "Alice"},
    )


class RecordingBackend:
    name = "fc-agent-sandbox"
    verification_available = True

    def __init__(self, result: ExecutionResult | None = None, error: Exception | None = None):
        self.result = result or ExecutionResult(
            backend=self.name,
            sandbox_id="candidate-sandbox",
            exit_code=0,
            stdout='{"message":"Hello, Alice"}',
            stderr="",
            duration_ms=12,
            command_profile="model-candidate",
            bundle_bytes=100,
        )
        self.error = error
        self.requests: list[Any] = []

    def run(self, request: Any) -> ExecutionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def test_trial_runs_only_the_fixed_json_candidate_bundle() -> None:
    backend = RecordingBackend()

    result = CandidateTrialRunner(backend).trial(request_for())

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

    result = CandidateTrialRunner(backend).execute(request_for())

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
    result = CandidateTrialRunner(backend).trial(
        request_for(source=source, **manifest_overrides)
    )

    assert result.status == "rejected"
    assert result.error_code == error_code
    assert backend.requests == []


def test_candidate_content_hash_must_match_before_backend_call() -> None:
    backend = RecordingBackend()
    result = CandidateTrialRunner(backend).trial(
        request_for(content_hash="0" * 64)
    )

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_HASH_MISMATCH"
    assert backend.requests == []


def test_backend_is_required_and_never_falls_back_to_host_execution() -> None:
    result = CandidateTrialRunner(None).trial(request_for())

    assert result.status == "failed"
    assert result.error_code == "CANDIDATE_BACKEND_UNAVAILABLE"
    assert result.output is None


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
    result = CandidateTrialRunner(backend).trial(request_for())

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
        )
    )

    result = CandidateTrialRunner(backend).trial(request_for())

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
        )
    )

    assert (
        CandidateTrialRunner(invalid).trial(request_for()).error_code
        == "CANDIDATE_OUTPUT_INVALID"
    )
    assert (
        CandidateTrialRunner(truncated).trial(request_for()).error_code
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
        )
    )

    result = CandidateTrialRunner(backend).trial(request_for())

    assert result.error_code == "CANDIDATE_OUTPUT_LIMIT_EXCEEDED"
    assert result.output_truncated is True


def test_input_is_json_only_and_sensitive_fields_are_not_uploaded() -> None:
    runner = CandidateTrialRunner(RecordingBackend())

    result = runner.trial(request_for(input_data={"api_key": "secret"}))

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_INPUT_SENSITIVE"


def test_nested_sensitive_fields_are_rejected_even_under_non_string_keys() -> None:
    result = CandidateTrialRunner(RecordingBackend()).trial(
        request_for(input_data={0: {"nested_api_key": "secret"}})
    )

    assert result.status == "rejected"
    assert result.error_code == "CANDIDATE_INPUT_SENSITIVE"


def test_trial_budget_is_shared_across_concurrent_calls() -> None:
    backend = RecordingBackend()
    runner = CandidateTrialRunner(backend)

    results = [
        runner.trial(
            CandidateTrialRequest(
                task_id="task-budget",
                project_id="project-1",
                trial_id=f"trial-{index}",
                manifest=manifest_for(),
                source=VALID_SOURCE,
                input_data={"name": "Alice"},
            )
        )
        for index in range(CANDIDATE_MAX_TRIALS + 1)
    ]

    assert [result.status for result in results] == ["passed"] * CANDIDATE_MAX_TRIALS + ["failed"]
    assert results[-1].error_code == "CANDIDATE_BUDGET_EXCEEDED"
    assert len(backend.requests) == CANDIDATE_MAX_TRIALS


def test_trial_timeout_and_output_limits_are_bound_into_request() -> None:
    backend = RecordingBackend()
    request = request_for()

    result = CandidateTrialRunner(backend).trial(request)

    sent = backend.requests[0]
    assert result.status == "passed"
    assert sent.timeout_seconds == 30
    assert sent.max_output_bytes == CANDIDATE_MAX_OUTPUT_BYTES
    assert CANDIDATE_MAX_TRIAL_SECONDS == 120


def test_result_is_safe_to_serialize_and_records_cleanup_state() -> None:
    backend = RecordingBackend()
    backend.get_run_state = lambda _run_id: {"cleanup_status": "cleaned"}  # type: ignore[attr-defined]

    result = CandidateTrialRunner(backend).trial(request_for())
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
        )
    )

    result = CandidateTrialRunner(backend).trial(request_for())

    assert result.status == "passed"
    assert result.output == {"api_key": "[REDACTED]", "message": "ok"}
