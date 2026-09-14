from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry

from devpilot.candidate_runner import (
    CandidateExecutionResult,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
)
from devpilot.harness_selection import HarnessSelectionStore
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
        self.cancellations: list[CandidateTrialRequest] = []

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

    def cancel(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        self.cancellations.append(request)
        return self._result(request, {"cancelled": True})

    def recover(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        return self._result(request, {"recovered": True})


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


def test_h8_remote_strategy_artifact_layout_survives_a_long_windows_data_path(
    tmp_path: Path,
) -> None:
    """Immutable artifacts must remain usable below a normal deep workspace path."""
    catalog = RemoteStrategyCatalog(
        tmp_path / ("workspace-segment-" * 4),
        runner=FakeCandidateRunner(),
        grant_issuer=ISSUER,
    )

    artifact = catalog.define("session-private", "strategy-one", "1.0.0", SOURCE)

    assert (artifact.root / "candidate.py").is_file()
    assert catalog.load(
        "session-private", "strategy-one", "1.0.0", artifact.source_hash
    ) == artifact


def test_h8_remote_strategy_loads_an_existing_legacy_artifact_layout(tmp_path: Path) -> None:
    catalog = RemoteStrategyCatalog(tmp_path, runner=FakeCandidateRunner(), grant_issuer=ISSUER)
    artifact = catalog.define("session-private", "strategy-one", "1.0.0", SOURCE)
    legacy_root = catalog._legacy_artifact_root(
        artifact.session_id,
        artifact.strategy_id,
        artifact.version,
        artifact.source_hash,
    )
    legacy_root.mkdir(parents=True)
    for filename in ("candidate.py", "manifest.json"):
        (artifact.root / filename).replace(legacy_root / filename)

    restored = catalog.load(
        artifact.session_id,
        artifact.strategy_id,
        artifact.version,
        artifact.source_hash,
    )

    assert restored.root == legacy_root


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


def test_h8_remote_strategy_cancels_the_exact_durable_candidate_request(tmp_path: Path) -> None:
    runner = FakeCandidateRunner()
    catalog = RemoteStrategyCatalog(tmp_path, runner=runner, grant_issuer=ISSUER)
    artifact = catalog.define("session-private", "strategy-one", "1.0.0", SOURCE)
    catalog.trial(artifact, task_id="task-private", project_id="project-private")
    request = catalog.decision_request(
        artifact,
        session_id="session-private",
        task_id="task-private",
        project_id="project-private",
        projection=_projection(),
    )

    result = catalog.cancel_decision(request)

    assert result.status == "passed"
    assert runner.cancellations == [request]


def test_h8_remote_decision_request_survives_catalog_restart(tmp_path: Path) -> None:
    runner = FakeCandidateRunner()
    catalog = RemoteStrategyCatalog(tmp_path, runner=runner, grant_issuer=ISSUER)
    artifact = catalog.define("session-private", "strategy-one", "1.0.0", SOURCE)
    catalog.trial(artifact, task_id="task-private", project_id="project-private")
    request = catalog.decision_request(
        artifact,
        session_id="session-private",
        task_id="task-private",
        project_id="project-private",
        projection=_projection(),
    )

    restarted = RemoteStrategyCatalog(tmp_path, runner=runner, grant_issuer=ISSUER)
    restored = restarted.load_decision_request(
        "session-private",
        "strategy-one",
        "1.0.0",
        artifact.source_hash,
        request.trial_id,
    )

    assert restored.trial_id == request.trial_id
    assert restored.task_id == request.task_id
    assert restored.project_id == request.project_id
    assert restored.input_data == request.input_data
    cancelled = restarted.cancel_decision(restored)
    assert cancelled.status == "passed"
    assert runner.cancellations[-1].trial_id == request.trial_id


def test_h8_remote_recovery_queries_the_durable_request_without_reexecution(tmp_path: Path) -> None:
    runner = FakeCandidateRunner()
    catalog = RemoteStrategyCatalog(tmp_path, runner=runner, grant_issuer=ISSUER)
    artifact = catalog.define("session-private", "strategy-one", "1.0.0", SOURCE)
    catalog.trial(artifact, task_id="task-private", project_id="project-private")
    request = catalog.decision_request(
        artifact,
        session_id="session-private",
        task_id="task-private",
        project_id="project-private",
        projection=_projection(),
    )

    restarted = RemoteStrategyCatalog(tmp_path, runner=runner, grant_issuer=ISSUER)
    result = restarted.recover_decision(
        "session-private",
        "strategy-one",
        "1.0.0",
        artifact.source_hash,
        request.trial_id,
    )

    assert result.status == "passed"
    assert runner.executions == []


def test_h9_remote_strategy_project_promotion_is_visible_to_a_new_session(tmp_path: Path) -> None:
    runner = FakeCandidateRunner()
    catalog = RemoteStrategyCatalog(tmp_path, runner=runner, grant_issuer=ISSUER)
    artifact = catalog.define("session-private", "strategy-project", "1.0.0", SOURCE)
    catalog.trial(artifact, task_id="task-private", project_id="project-private")

    promoted = catalog.promote_to_project(artifact, project_id="project-private")
    restored = catalog.load_for_scope(
        "session-new",
        "project-private",
        "strategy-project",
        "1.0.0",
        artifact.source_hash,
    )

    assert restored.root == promoted.root
    assert restored.session_id == "session-new"
    catalog.require_trialled(restored)


@pytest.mark.asyncio
async def test_h9_remote_loop_candidate_uses_the_same_fixed_selection_gate(
    tmp_path: Path,
) -> None:
    harness_db = tmp_path / "harness.db"
    with sqlite3.connect(harness_db) as connection:
        connection.execute(
            "create table harness_selection_records ("
            "record_id text primary key, record_type text not null, payload_json text not null, "
            "record_mac text not null, created_at text not null)"
        )
    harness = HarnessSelectionStore(harness_db, integrity_key=b"h9-remote-loop-test-key")
    runner = FakeCandidateRunner()
    controller = ModelCandidateController(tmp_path, runner=runner, grant_issuer=ISSUER)
    controller.bind_plugin_manager(PluginManager(ToolRegistry()))
    controller.bind_harness_selection(
        harness,
        case_oracle=lambda _category, version, _scenario, _repeat, _fixture, _result: {
            "success": version == "candidate",
        },
    )
    candidate_source = SOURCE.replace("continue", "candidate")
    baseline = controller.define_remote_strategy(
        "session-private", "strategy-h9", "1.0.0", SOURCE
    )
    candidate = controller.define_remote_strategy(
        "session-private", "strategy-h9", "1.1.0", candidate_source
    )
    controller.trial_remote_strategy(
        baseline, task_id="task-h9-loop", project_id="project-h9-loop"
    )
    controller.trial_remote_strategy(
        candidate, task_id="task-h9-loop", project_id="project-h9-loop"
    )
    context = RequestContext(
        channel="test",
        chat_id="session-private",
        session_key="session-private",
        sender_id="user:one",
        original_user_text="确认试用",
        workspace=tmp_path,
        attributes={
            "devpilot_task_id": "task-h9-loop",
            "devpilot_project_id": "project-h9-loop",
            "devpilot_model_candidates_enabled": True,
        },
    )
    evidence_id = harness.record_evidence(
        task_id="task-h9-loop",
        project_id="project-h9-loop",
        workspace=str(tmp_path.resolve()),
        version_hash=baseline.source_hash,
        source_type="task_result",
        summary="remote Loop baseline task result",
        usage={"calls": 1},
    )
    context.attributes["devpilot_harness_evidence_refs"] = [evidence_id]

    with request_context(context):
        evaluated = json.loads(
            await controller.evaluate_loop(
                {
                    "strategy_id": "strategy-h9",
                    "baseline_version": "1.0.0",
                    "version": "1.1.0",
                    "baseline_source_hash": baseline.source_hash,
                    "source_hash": candidate.source_hash,
                    "round_id": "round-h9-loop",
                    "max_candidates": 2,
                    "max_evaluations": 48,
                    "max_tokens": 1000,
                },
                controller._context(),
            )
        )

    assert evaluated["ok"] is True
    assert evaluated["result"]["eligible"] is True
    assert evaluated["result"]["total_runs"] == 24
    assert len(runner.executions) == 24


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
