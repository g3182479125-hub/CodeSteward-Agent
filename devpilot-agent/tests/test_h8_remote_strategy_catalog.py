from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from nanobot.agent.tools.context import RequestContext, request_context

from devpilot.candidate_runner import (
    CandidateExecutionResult,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
)
from devpilot.model_plugins import ModelCandidateController
from devpilot.remote_loop_strategy import RemoteStrategyCatalog

SOURCE = (
    "def transform(payload):\n"
    "    return {'schema': 'h8-remote-driver-projection-v1', "
    "'action': 'request_model', 'reason': 'continue'}\n"
)
ISSUER = CandidateTrialGrantIssuer(b"h8-remote-strategy-test-secret-0123456789")


class FakeCandidateRunner:
    def __init__(self) -> None:
        self.trials: list[CandidateTrialRequest] = []
        self.executions: list[CandidateTrialRequest] = []

    @staticmethod
    def _result(request: CandidateTrialRequest, output: object) -> CandidateExecutionResult:
        source_hash = hashlib.sha256(request.source.encode("utf-8")).hexdigest()
        input_hash = hashlib.sha256(
            json.dumps(request.input_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return CandidateExecutionResult(
            status="passed",
            candidate_id=str(request.manifest["plugin_id"]),
            version=str(request.manifest["version"]),
            trial_id=request.trial_id,
            run_id="fake-run",
            backend="fake-fc",
            sandbox_id="fake-sandbox",
            exit_code=0,
            output=output,
            error_code=None,
            error_message=None,
            duration_ms=1,
            cleanup_status="cleaned",
            source_hash=source_hash,
            input_hash=input_hash,
        )

    def trial(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        self.trials.append(request)
        return self._result(request, {"trial": True})

    def execute(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        self.executions.append(request)
        return self._result(
            request,
            {
                "schema": "h8-remote-driver-projection-v1",
                "action": "request_model",
                "reason": "continue",
            },
        )


def _projection() -> dict[str, object]:
    return {
        "schema": "h8-remote-driver-projection-v1",
        "task_ref": "opaque-task",
        "project_ref": "opaque-project",
        "step": 2,
        "remaining_iterations": 5,
        "completed_result_count": 1,
        "pending_tool_count": 0,
        "allowed_actions": ["finish", "pause", "request_model"],
    }


def test_h8_remote_strategy_is_immutable_trialed_and_receives_only_projection(
    tmp_path: Path,
) -> None:
    runner = FakeCandidateRunner()
    catalog = RemoteStrategyCatalog(tmp_path, runner=runner, grant_issuer=ISSUER)
    artifact = catalog.define("session-private", "strategy-one", "1.0.0", SOURCE)

    catalog.trial(artifact, task_id="task-private", project_id="project-private")
    decision = catalog.decide(
        artifact,
        session_id="session-private",
        task_id="task-private",
        project_id="project-private",
        projection=_projection(),
    )

    assert decision["action"] == "request_model"
    assert len(runner.trials) == 1
    assert len(runner.executions) == 1
    payload = runner.executions[0].input_data
    assert payload == {
        "schema": "h8-remote-driver-projection-v1",
        "task_ref": "opaque-task",
        "project_ref": "opaque-project",
        "step": 2,
        "remaining_iterations": 5,
        "completed_result_count": 1,
        "pending_tool_count": 0,
    }
    assert "session-private" not in str(payload)
    assert "task-private" not in str(payload)
    assert "project-private" not in str(payload)


def test_h8_remote_strategy_rejects_untrialed_or_hash_mismatched_artifact(tmp_path: Path) -> None:
    catalog = RemoteStrategyCatalog(tmp_path, runner=FakeCandidateRunner(), grant_issuer=ISSUER)
    artifact = catalog.define("session-private", "strategy-one", "1.0.0", SOURCE)

    with pytest.raises(ValueError, match="trial"):
        catalog.decide(
            artifact,
            session_id="session-private",
            task_id="task-private",
            project_id="project-private",
            projection=_projection(),
        )
    catalog.trial(artifact, task_id="task-private", project_id="project-private")
    with pytest.raises(ValueError, match="hash"):
        catalog.decide(
            replace(artifact, source_hash="0" * 64),
            session_id="session-private",
            task_id="task-private",
            project_id="project-private",
            projection=_projection(),
        )


@pytest.mark.asyncio
async def test_h8_controller_executes_only_a_session_bound_remote_strategy(tmp_path: Path) -> None:
    runner = FakeCandidateRunner()
    controller = ModelCandidateController(tmp_path, runner=runner, grant_issuer=ISSUER)
    artifact = controller.define_remote_strategy("session-private", "strategy-one", "1.0.0", SOURCE)
    controller.trial_remote_strategy(artifact, task_id="task-private", project_id="project-private")
    context = RequestContext(
        channel="test",
        chat_id="session-private",
        session_key="session-private",
        sender_id="user:one",
        workspace=tmp_path,
        attributes={"devpilot_task_id": "task-private", "devpilot_project_id": "project-private"},
    )

    with request_context(context):
        decision = await controller.execute_remote_strategy(
            artifact.strategy_id,
            artifact.version,
            artifact.source_hash,
            _projection(),
        )

    assert decision["action"] == "request_model"
