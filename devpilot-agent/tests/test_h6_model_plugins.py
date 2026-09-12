from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.plugins.store import SqlitePluginStateStore
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry

from devpilot.candidate_runner import (
    CandidateExecutionResult,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
)
from devpilot.model_plugins import ModelCandidateController, ModelPluginManagementTool

SOURCE = "def transform(payload):\n    return {'message': 'hello ' + payload['name']}\n"
SOURCE_V2 = "def transform(payload):\n    return {'message': 'hi again ' + payload['name']}\n"
ISSUER = CandidateTrialGrantIssuer(b"h6-model-candidate-test-secret-012345")


class FakeCandidateRunner:
    def __init__(self, *, reject_trials: bool = False) -> None:
        self.trials: list[CandidateTrialRequest] = []
        self.executions: list[CandidateTrialRequest] = []
        self.reject_trials = reject_trials

    @staticmethod
    def _result(request: CandidateTrialRequest, *, output: object) -> CandidateExecutionResult:
        source_hash = hashlib.sha256(request.source.encode("utf-8")).hexdigest()
        input_hash = hashlib.sha256(
            json.dumps(
                request.input_data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
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
            operation_id="fake-operation",
        )

    def trial(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        self.trials.append(request)
        result = self._result(request, output={"trial": True})
        return (
            replace(
                result,
                status="rejected",
                error_code="CANDIDATE_SOURCE_INVALID",
                error_message="fixture rejection",
            )
            if self.reject_trials
            else result
        )

    def execute(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        self.executions.append(request)
        return self._result(request, output={"echo": request.input_data})


def _context(
    tmp_path: Path,
    *,
    session: str = "session:one",
    text: str = "define",
    candidate_enabled: bool | None = True,
) -> RequestContext:
    return RequestContext(
        channel="test",
        chat_id=session,
        session_key=session,
        sender_id="user:one",
        original_user_text=text,
        turn_id=f"turn:{session}",
        workspace=tmp_path,
        attributes=(
            {"devpilot_model_candidates_enabled": candidate_enabled}
            if candidate_enabled is not None
            else {}
        ),
    )


def test_h6_management_schema_describes_input_rule_contract() -> None:
    tool = ModelPluginManagementTool(None)  # type: ignore[arg-type]
    schema = tool.parameters["properties"]["input_rules"]
    item = schema["items"]

    assert item["required"] == ["field", "kind", "max_length"]
    assert item["properties"]["kind"]["enum"] == [
        "string",
        "integer",
        "number",
        "boolean",
    ]
    assert tool.validate_params(
        {
            "action": "define",
            "input_rules": [
                {"field": "name", "kind": "string", "max_length": 64},
            ],
        }
    ) == []
    errors = tool.validate_params(
        {
            "action": "define",
            "input_rules": [
                {"field": "name", "type": "string", "max_length": 64},
            ],
        }
    )
    assert any("missing required input_rules[0].kind" in error for error in errors)
    assert any("unexpected parameter input_rules[0].type" in error for error in errors)


@pytest.mark.asyncio
async def test_h6_define_trial_activate_and_call_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    runner = FakeCandidateRunner()
    controller = ModelCandidateController(tmp_path, runner=runner, grant_issuer=ISSUER)
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path)):
        defined = json.loads(
            await tool.execute(
                action="define",
                plugin_id="model-greeting",
                version="1.0.0",
                source=SOURCE,
                input_rules=[{"field": "name", "kind": "string", "max_length": 64}],
            )
        )
        assert defined["ok"] is True
        denied = json.loads(
            await tool.execute(
                action="trial",
                plugin_id="model-greeting",
                version="1.0.0",
                input_data={"name": "Alice"},
            )
        )
        assert denied["code"] == "PLUGIN_AUTHORIZATION_REQUIRED"

    confirmed = _context(tmp_path, text="确认试用")
    with request_context(confirmed):
        trial = json.loads(
            await tool.execute(
                action="trial",
                plugin_id="model-greeting",
                version="1.0.0",
                trial_id="trial-1",
                input_data={"name": "Alice"},
            )
        )
        assert trial["ok"] is True
        assert trial["result"]["candidate_status"] == "tested"

        activated = json.loads(
            await tool.execute(
                action="activate",
                plugin_id="model-greeting",
                version="1.0.0",
            )
        )
        assert activated["ok"] is True
        assert activated["result"]["details"]["candidate_activation"] == "true"

        # The dynamic view is the same live manager instance; no process or
        # AgentLoop restart is needed before the next schema is built.
        view = manager.live_tools_for_session(base, "session:one")
        assert "model-greeting" in view.tool_names
        result = json.loads(await view.execute("model-greeting", {"name": "Alice"}))
        assert result["ok"] is True
        assert result["result"]["output"] == {"echo": {"name": "Alice"}}
        assert len(runner.trials) == 1
        assert len(runner.executions) == 1
        assert runner.trials[0].input_data == {"name": "fixture"}

        stopped = json.loads(
            await tool.execute(
                action="stop",
                plugin_id="model-greeting",
                version="1.0.0",
            )
        )
        assert stopped["ok"] is True


@pytest.mark.asyncio
async def test_h6_candidate_is_session_scoped_and_cross_session_activation_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    controller = ModelCandidateController(
        tmp_path,
        runner=FakeCandidateRunner(),
        grant_issuer=ISSUER,
    )
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path, text="确认试用")):
        await tool.execute(
            action="define",
            plugin_id="model-private",
            version="1.0.0",
            source=SOURCE,
        )

    other_context = _context(tmp_path, session="session:two", text="确认启用")
    with request_context(other_context):
        inspected = json.loads(await tool.execute(action="inspect"))
        assert inspected["result"]["candidates"] == []
        result = json.loads(
            await tool.execute(
                action="activate",
                plugin_id="model-private",
                version="1.0.0",
            )
        )
        assert result["code"] == "PLUGIN_NOT_FOUND"


@pytest.mark.asyncio
async def test_h6_profile_gate_cannot_be_bypassed_by_chat_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    controller = ModelCandidateController(
        tmp_path,
        runner=FakeCandidateRunner(),
        grant_issuer=ISSUER,
    )
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path, text="确认试用", candidate_enabled=None)):
        result = json.loads(
            await tool.execute(
                action="define",
                plugin_id="model-disabled",
                version="1.0.0",
                source=SOURCE,
            )
        )

    assert result["code"] == "MODEL_CANDIDATES_PROFILE_DISABLED"


@pytest.mark.asyncio
async def test_h6_candidate_trial_binding_rejects_changed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    controller = ModelCandidateController(
        tmp_path,
        runner=FakeCandidateRunner(),
        grant_issuer=ISSUER,
    )
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)
    with request_context(_context(tmp_path, text="确认试用")):
        await tool.execute(
            action="define",
            plugin_id="model-immutable",
            version="1.0.0",
            source=SOURCE,
        )
        registration = manager._find_visible(
            "model-immutable", "1.0.0", "session:one", PluginManager.project_id_for_path(tmp_path)
        )
        assert registration is not None
        registration.artifact_root.joinpath("candidate.py").write_text(
            SOURCE + "\n",
            encoding="utf-8",
        )
        trial = json.loads(
            await tool.execute(
                action="trial",
                plugin_id="model-immutable",
                version="1.0.0",
                input_data={"value": "x"},
            )
        )
        assert trial["code"] == "CANDIDATE_TRIAL_PREPARATION_FAILED"


@pytest.mark.asyncio
async def test_h6_changed_input_contract_invalidates_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    controller = ModelCandidateController(
        tmp_path,
        runner=FakeCandidateRunner(),
        grant_issuer=ISSUER,
    )
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path, text="确认试用")):
        assert json.loads(
            await tool.execute(
                action="define",
                plugin_id="model-contract",
                version="1.0.0",
                source=SOURCE,
                input_rules=[{"field": "name", "kind": "string", "max_length": 64}],
            )
        )["ok"]
        assert json.loads(
            await tool.execute(
                action="trial",
                plugin_id="model-contract",
                version="1.0.0",
            )
        )["ok"]
        registration = manager._find_visible(
            "model-contract",
            "1.0.0",
            "session:one",
            PluginManager.project_id_for_path(tmp_path),
        )
        assert registration is not None
        registration.artifact_root.joinpath("input-rules.json").write_text(
            json.dumps(
                [{"field": "name", "kind": "string", "max_length": 128}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        activated = json.loads(
            await tool.execute(
                action="activate",
                plugin_id="model-contract",
                version="1.0.0",
            )
        )

    assert activated["code"] == "invalid_candidate_state"


@pytest.mark.asyncio
async def test_h6_rejected_trial_cannot_be_activated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    controller = ModelCandidateController(
        tmp_path,
        runner=FakeCandidateRunner(reject_trials=True),
        grant_issuer=ISSUER,
    )
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path, text="确认试用")):
        await tool.execute(
            action="define",
            plugin_id="model-rejected",
            version="1.0.0",
            source=SOURCE,
        )
        trial = json.loads(
            await tool.execute(
                action="trial",
                plugin_id="model-rejected",
                version="1.0.0",
            )
        )
        activated = json.loads(
            await tool.execute(
                action="activate",
                plugin_id="model-rejected",
                version="1.0.0",
            )
        )

    assert trial["result"]["candidate_status"] == "rejected"
    assert activated["code"] == "invalid_candidate_state"


@pytest.mark.asyncio
async def test_h6_tested_revision_replaces_active_route_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    runner = FakeCandidateRunner()
    controller = ModelCandidateController(tmp_path, runner=runner, grant_issuer=ISSUER)
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    confirmed = _context(tmp_path, text="确认试用")
    with request_context(confirmed):
        for version, source in (("1.0.0", SOURCE), ("2.0.0", SOURCE_V2)):
            defined = json.loads(
                await tool.execute(
                    action="define",
                    plugin_id="model-revision",
                    version=version,
                    source=source,
                    input_rules=[{"field": "name", "kind": "string", "max_length": 64}],
                )
            )
            assert defined["ok"] is True
            trial = json.loads(
                await tool.execute(
                    action="trial",
                    plugin_id="model-revision",
                    version=version,
                    input_data={"name": "Alice"},
                )
            )
            assert trial["ok"] is True

        first_activation = json.loads(
            await tool.execute(
                action="activate",
                plugin_id="model-revision",
                version="1.0.0",
            )
        )
        assert first_activation["ok"] is True
        old_view = manager.live_tools_for_session(base, "session:one")
        old_view.get_definitions()
        second_activation = json.loads(
            await tool.execute(
                action="activate",
                plugin_id="model-revision",
                version="2.0.0",
            )
        )
        assert second_activation["ok"] is True
        assert second_activation["result"]["details"]["route_commit"] == "committed"

        old_result = await old_view.execute("model-revision", {"name": "Alice"})
        assert "TOOL_VERSION_RETIRED" in str(old_result)

        view = manager.live_tools_for_session(base, "session:one")
        assert "model-revision" in view.tool_names
        await view.execute("model-revision", {"name": "Alice"})
        assert runner.executions[-1].manifest["version"] == "2.0.0"
        assert manager._find_active("model-revision", "session:one", None)[0] is not None

        wrong_stop = json.loads(
            await tool.execute(
                action="stop",
                plugin_id="model-revision",
                version="1.0.0",
            )
        )
        assert wrong_stop["code"] == "INVALID_CANDIDATE_STATE"
        stopped = json.loads(
            await tool.execute(
                action="stop",
                plugin_id="model-revision",
                version="2.0.0",
            )
        )
        assert stopped["ok"] is True
        removed = json.loads(
            await tool.execute(
                action="remove",
                plugin_id="model-revision",
                version="2.0.0",
            )
        )
        assert removed["ok"] is True


@pytest.mark.asyncio
async def test_h6_active_candidate_route_restores_from_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    state_store = SqlitePluginStateStore(tmp_path / "runtime.db")
    controller = ModelCandidateController(
        tmp_path,
        runner=FakeCandidateRunner(),
        grant_issuer=ISSUER,
    )
    base = ToolRegistry()
    manager = PluginManager(
        base,
        state_store=state_store,
        candidate_tool_factory=controller.build_proxy,
    )
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path, text="确认试用")):
        assert json.loads(
            await tool.execute(
                action="define",
                plugin_id="model-persisted",
                version="1.0.0",
                source=SOURCE,
                input_rules=[{"field": "name", "kind": "string", "max_length": 64}],
            )
        )["ok"]
        assert json.loads(
            await tool.execute(
                action="trial",
                plugin_id="model-persisted",
                version="1.0.0",
                input_data={"name": "Alice"},
            )
        )["ok"]
        assert json.loads(
            await tool.execute(
                action="activate",
                plugin_id="model-persisted",
                version="1.0.0",
            )
        )["ok"]

    restored_controller = ModelCandidateController(
        tmp_path,
        runner=FakeCandidateRunner(),
        grant_issuer=ISSUER,
    )
    restored_base = ToolRegistry()
    restored_manager = PluginManager(
        restored_base,
        state_store=SqlitePluginStateStore(tmp_path / "runtime.db"),
        candidate_tool_factory=restored_controller.build_proxy,
    )
    restored_controller.bind_plugin_manager(restored_manager)
    restored_manager.authorize("session:one", PluginManager.project_id_for_path(tmp_path))
    restored_view = restored_manager.live_tools_for_session(restored_base, "session:one")
    assert "model-persisted" in restored_view.tool_names
