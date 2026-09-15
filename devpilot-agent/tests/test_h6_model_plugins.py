from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.plugins.store import SqlitePluginStateStore
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)

from devpilot.candidate_runner import (
    CandidateExecutionResult,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
)
from devpilot.harness_oracle import build_default_harness_case_oracle
from devpilot.harness_selection import HarnessSelectionStore
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
            side_effect_count=0,
            permission_violation=False,
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

    def can_activate_live(self, _task_id: str) -> bool:
        return True


class SourceExecutingCandidateRunner(FakeCandidateRunner):
    """Test-only FC double that executes the immutable candidate source."""

    def execute(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        self.executions.append(request)
        namespace: dict[str, object] = {}
        exec(request.source, namespace)
        transform = namespace["transform"]
        assert callable(transform)
        return self._result(request, output=transform(dict(request.input_data)))


class ExhaustedLiveBudgetRunner(FakeCandidateRunner):
    def can_activate_live(self, _task_id: str) -> bool:
        return False


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
    variants = {
        branch["properties"]["kind"]["enum"][0]: branch
        for branch in item["oneOf"]
    }
    assert variants["integer"]["properties"]["min_value"]["type"] == "integer"
    assert variants["number"]["properties"]["min_value"]["type"] == "number"
    assert "min_value" not in variants["string"]["properties"]
    assert tool.validate_params(
        {
            "action": "define",
            "input_rules": [
                {"field": "name", "kind": "string", "max_length": 64},
            ],
        }
    ) == []
    assert tool.validate_params(
        {
            "action": "define",
            "input_rules": [
                {
                    "field": "count",
                    "kind": "number",
                    "max_length": 64,
                    "min_value": 1.5,
                },
            ],
        }
    ) == []
    integer_bound_errors = tool.validate_params(
        {
            "action": "define",
            "input_rules": [
                {
                    "field": "count",
                    "kind": "integer",
                    "max_length": 64,
                    "min_value": 1.5,
                },
            ],
        }
    )
    assert any("integer rule limits must be integers" in error for error in integer_bound_errors)
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
async def test_h6_inspect_exposes_candidate_schema_and_live_budget_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    controller = ModelCandidateController(
        tmp_path, runner=FakeCandidateRunner(), grant_issuer=ISSUER
    )
    manager = PluginManager(ToolRegistry(), candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path)):
        inspected = json.loads(await tool.execute(action="inspect"))

    constraints = inspected["result"]["constraints"]
    assert constraints["live_execution"] == {"max_calls": 30, "max_seconds": 600}
    assert constraints["input_rules"]["required"] == ["field", "kind", "max_length"]
    assert constraints["input_rules"]["oneOf"]


@pytest.mark.asyncio
async def test_h6_activation_fails_closed_when_live_budget_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    controller = ModelCandidateController(
        tmp_path, runner=ExhaustedLiveBudgetRunner(), grant_issuer=ISSUER
    )
    manager = PluginManager(ToolRegistry(), candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path, text="确认试用")):
        assert json.loads(
            await tool.execute(
                action="define", plugin_id="model-budget", version="1.0.0", source=SOURCE
            )
        )["ok"]
        assert json.loads(
            await tool.execute(action="trial", plugin_id="model-budget", version="1.0.0")
        )["ok"]
        activated = json.loads(
            await tool.execute(action="activate", plugin_id="model-budget", version="1.0.0")
        )

    assert activated["code"] == "CANDIDATE_LIVE_BUDGET_EXCEEDED"


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
async def test_h11_webui_modes_auto_trial_but_only_full_auto_calls_real_candidate_data(
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

    restricted_scope = bind_workspace_scope(
        build_workspace_scope(tmp_path, "restricted", source_channel="websocket")
    )
    try:
        with request_context(_context(tmp_path, text="ordinary request")):
            assert json.loads(
                await tool.execute(
                    action="define", plugin_id="model-h11", version="1.0.0", source=SOURCE
                )
            )["ok"]
            assert json.loads(
                await tool.execute(action="trial", plugin_id="model-h11", version="1.0.0")
            )["ok"]
            assert json.loads(
                await tool.execute(action="activate", plugin_id="model-h11", version="1.0.0")
            )["ok"]
            view = manager.live_tools_for_session(base, "session:one")
            blocked = json.loads(await view.execute("model-h11", {"value": "real task"}))
            assert blocked["code"] == "CANDIDATE_REAL_INPUT_CONFIRMATION_REQUIRED"
            assert "已验证临时工具" in blocked["message"]
            assert "本次任务" in blocked["message"]
            assert runner.executions == []
    finally:
        reset_workspace_scope(restricted_scope)

    full_scope = bind_workspace_scope(
        build_workspace_scope(tmp_path, "full", source_channel="websocket")
    )
    try:
        with request_context(_context(tmp_path, text="ordinary request")):
            view = manager.live_tools_for_session(base, "session:one")
            result = json.loads(await view.execute("model-h11", {"value": "real task"}))
    finally:
        reset_workspace_scope(full_scope)

    assert result["ok"] is True
    assert len(runner.executions) == 1
    assert runner.executions[0].autonomy == {
        "access_mode": "full",
        "decision": "notice",
        "input_class": "task",
    }


@pytest.mark.asyncio
async def test_h11_full_webui_scope_rejects_sensitive_candidate_input_before_fc(
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
    scope = bind_workspace_scope(
        build_workspace_scope(tmp_path, "full", source_channel="websocket")
    )
    try:
        with request_context(_context(tmp_path, text="ordinary request")):
            for action in ("define", "trial", "activate"):
                response = await tool.execute(
                    action=action, plugin_id="model-red", version="1.0.0", source=SOURCE
                ) if action == "define" else await tool.execute(
                    action=action, plugin_id="model-red", version="1.0.0"
                )
                assert json.loads(response)["ok"]
            view = manager.live_tools_for_session(base, "session:one")
            blocked = await view.execute("model-red", {"token": "sk-not-for-fc"})
    finally:
        reset_workspace_scope(scope)

    assert "unexpected parameter token" in blocked
    assert runner.executions == []


def test_h11_does_not_apply_to_h9_authorization_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    controller = ModelCandidateController(
        tmp_path, runner=FakeCandidateRunner(), grant_issuer=ISSUER
    )
    manager = PluginManager(ToolRegistry(), candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    scope = bind_workspace_scope(
        build_workspace_scope(tmp_path, "full", source_channel="websocket")
    )
    try:
        request = _context(tmp_path, text="ordinary request")
        with request_context(request):
            context = controller._context()
            assert context is not None
            blocked = controller._authorize(context)
    finally:
        reset_workspace_scope(scope)

    assert blocked is not None
    assert "PLUGIN_AUTHORIZATION_REQUIRED" in blocked


def test_h11_full_scope_defaults_to_confirmation_after_controller_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    controller = ModelCandidateController(
        tmp_path, runner=FakeCandidateRunner(), grant_issuer=ISSUER
    )
    manager = PluginManager(ToolRegistry(), candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    scope = bind_workspace_scope(
        build_workspace_scope(tmp_path, "full", source_channel="websocket")
    )
    try:
        request = _context(tmp_path, text="ordinary request")
        with request_context(request):
            context = controller._context()
            assert context is not None
            blocked = controller._authorize(
                context,
                apply_autonomy=True,
                candidate_id="recovered",
                candidate_version="1.0.0",
            )
    finally:
        reset_workspace_scope(scope)

    assert blocked is not None
    assert "PLUGIN_AUTHORIZATION_REQUIRED" in blocked


@pytest.mark.asyncio
async def test_h11_revoke_stops_candidate_routes_and_records_decisions(
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
    scope = bind_workspace_scope(
        build_workspace_scope(tmp_path, "full", source_channel="websocket")
    )
    try:
        with request_context(_context(tmp_path, text="ordinary request")):
            for action in ("define", "trial", "activate"):
                response = await tool.execute(
                    action=action,
                    plugin_id="model-revoke",
                    version="1.0.0",
                    source=SOURCE,
                ) if action == "define" else await tool.execute(
                    action=action, plugin_id="model-revoke", version="1.0.0"
                )
                assert json.loads(response)["ok"]
            view = manager.live_tools_for_session(base, "session:one")
            assert "model-revoke" in view.tool_names
            revoked = json.loads(await tool.execute(action="revoke"))
            assert revoked["result"]["status"] == "authorization_revoked"
            assert len(revoked["result"]["stopped"]) == 1
            assert revoked["result"]["stopped"][0]["details"][
                "autonomy_access_mode"
            ] == "full"
            assert "model-revoke" not in manager.live_tools_for_session(
                base, "session:one"
            ).tool_names
    finally:
        reset_workspace_scope(scope)

    assert any(
        operation.action == "candidate_autonomy"
        and operation.result.details_dict["decision"] in {"auto", "notice"}
        for operation in manager._operations.values()
    )


@pytest.mark.asyncio
async def test_h11_restricted_confirmation_is_invalidated_by_a_new_candidate_version(
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
    scope = bind_workspace_scope(
        build_workspace_scope(tmp_path, "restricted", source_channel="websocket")
    )
    try:
        with request_context(_context(tmp_path, text="ordinary request")):
            for action in ("define", "trial", "activate"):
                response = await tool.execute(
                    action=action,
                    plugin_id="model-versioned",
                    version="1.0.0",
                    source=SOURCE,
                ) if action == "define" else await tool.execute(
                    action=action, plugin_id="model-versioned", version="1.0.0"
                )
                assert json.loads(response)["ok"]
        with request_context(_context(tmp_path, text="确认试用")):
            view = manager.live_tools_for_session(base, "session:one")
            assert json.loads(await view.execute("model-versioned", {"value": "v1"}))["ok"]
        with request_context(_context(tmp_path, text="ordinary request")):
            for action in ("define", "trial", "activate"):
                response = await tool.execute(
                    action=action,
                    plugin_id="model-versioned",
                    version="2.0.0",
                    source=SOURCE_V2,
                ) if action == "define" else await tool.execute(
                    action=action, plugin_id="model-versioned", version="2.0.0"
                )
                assert json.loads(response)["ok"]
            view = manager.live_tools_for_session(base, "session:one")
            blocked = json.loads(await view.execute("model-versioned", {"value": "v2"}))
    finally:
        reset_workspace_scope(scope)

    assert blocked["code"] == "CANDIDATE_REAL_INPUT_CONFIRMATION_REQUIRED"
    assert len(runner.executions) == 1


@pytest.mark.asyncio
async def test_h9_promotes_a_tested_candidate_and_starts_it_only_for_a_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    controller = ModelCandidateController(
        tmp_path, runner=FakeCandidateRunner(), grant_issuer=ISSUER
    )
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    tool = ModelPluginManagementTool(controller)

    with request_context(_context(tmp_path, text="确认试用")):
        assert json.loads(
            await tool.execute(
                action="define", plugin_id="model-h9", version="1.0.0", source=SOURCE
            )
        )["ok"]
        assert json.loads(
            await tool.execute(action="trial", plugin_id="model-h9", version="1.0.0")
        )["ok"]

    promoted = manager.promote_candidate_to_project(
        "model-h9", "1.0.0", session_id="session:one", project_id="project-h9"
    )
    assert promoted.ok
    started = manager.start_h9_selected_candidate(
        "model-h9", "1.0.0", session_id="session:new", project_id="project-h9"
    )
    assert started.ok
    assert "model-h9" not in manager.live_tools_for_session(base, "session:one").tool_names
    assert "model-h9" in manager.live_tools_for_session(base, "session:new").tool_names


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
        assert manager.is_authorized(
            "session:two", PluginManager.project_id_for_path(tmp_path)
        ) is False

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


@pytest.mark.asyncio
async def test_h9_model_candidate_action_runs_the_host_fixed_suite_before_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    harness_db = tmp_path / "harness.db"
    with sqlite3.connect(harness_db) as connection:
        connection.execute(
            "create table harness_selection_records ("
            "record_id text primary key, record_type text not null, payload_json text not null, "
            "record_mac text not null, created_at text not null)"
        )
    harness = HarnessSelectionStore(harness_db, integrity_key=b"h9-controller-test-key")

    runner = FakeCandidateRunner()
    controller = ModelCandidateController(tmp_path, runner=runner, grant_issuer=ISSUER)
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    controller.bind_harness_selection(
        harness,
        case_oracle=lambda _category, version, _scenario, _repeat, _fixture, _result: {
            "success": version == "candidate",
        },
    )
    tool = ModelPluginManagementTool(controller)
    context = _context(tmp_path, text="确认试用")
    project_id = PluginManager.project_id_for_path(tmp_path)
    context.attributes.update(
        {
            "devpilot_project_id": project_id,
            "devpilot_task_id": "task-h9-controller",
        }
    )
    baseline_hash = hashlib.sha256(SOURCE.encode()).hexdigest()
    evidence_id = harness.record_evidence(
        task_id="task-h9-controller",
        project_id=project_id,
        workspace=str(tmp_path.resolve()),
        version_hash=baseline_hash,
        source_type="task_result",
        summary="baseline task result",
        usage={"calls": 1},
    )
    context.attributes["devpilot_harness_evidence_refs"] = [evidence_id]

    with request_context(context):
        assert json.loads(
            await tool.execute(
                action="define",
                plugin_id="model-h9-controller",
                version="1.0.0",
                source=SOURCE,
                input_rules=[{"field": "name", "kind": "string", "max_length": 64}],
            )
        )["ok"]
        assert json.loads(
            await tool.execute(
                action="trial",
                plugin_id="model-h9-controller",
                version="1.0.0",
            )
        )["ok"]
        assert json.loads(
            await tool.execute(
                action="activate",
                plugin_id="model-h9-controller",
                version="1.0.0",
            )
        )["ok"]
        assert json.loads(
            await tool.execute(
                action="define",
                plugin_id="model-h9-controller",
                version="1.1.0",
                source=SOURCE_V2,
                input_rules=[{"field": "name", "kind": "string", "max_length": 64}],
            )
        )["ok"]
        evaluated = json.loads(
            await tool.execute(
                action="evaluate",
                plugin_id="model-h9-controller",
                baseline_version="1.0.0",
                version="1.1.0",
                round_id="round-h9-controller",
                max_candidates=2,
                max_evaluations=48,
                max_tokens=1000,
            )
        )

    assert evaluated["ok"] is True
    assert evaluated["result"]["eligible"] is True
    assert evaluated["result"]["candidate_status"] == "tested"
    assert evaluated["result"]["total_runs"] == 24
    assert len(runner.executions) == 24
    assert {tuple(request.input_data) for request in runner.executions} == {("name",)}


@pytest.mark.asyncio
async def test_h9_default_host_oracle_rejects_buggy_calculator_and_accepts_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H9-A: only the host's six immutable calculator cases can promote a repair."""
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    harness_db = tmp_path / "harness.db"
    with sqlite3.connect(harness_db) as connection:
        connection.execute(
            "create table harness_selection_records ("
            "record_id text primary key, record_type text not null, payload_json text not null, "
            "record_mac text not null, created_at text not null)"
        )
    harness = HarnessSelectionStore(harness_db, integrity_key=b"h9-default-oracle-test-key")
    runner = SourceExecutingCandidateRunner()
    controller = ModelCandidateController(tmp_path, runner=runner, grant_issuer=ISSUER)
    base = ToolRegistry()
    manager = PluginManager(base, candidate_tool_factory=controller.build_proxy)
    controller.bind_plugin_manager(manager)
    controller.bind_harness_selection(harness, case_oracle=build_default_harness_case_oracle())
    tool = ModelPluginManagementTool(controller)
    context = _context(tmp_path, text="确认试用")
    project_id = PluginManager.project_id_for_path(tmp_path)
    context.attributes.update(
        {
            "devpilot_project_id": project_id,
            "devpilot_task_id": "task-h9-default-calculator",
        }
    )
    buggy = (
        "def transform(payload):\n"
        "    return {'discounted_price': payload['price'] - payload['price'] * "
        "payload['discount_percent'] * 0.1}\n"
    )
    repaired = buggy.replace("* 0.1", "* 0.01")
    rules = [
        {"field": "price", "kind": "number", "max_length": 16},
        {"field": "discount_percent", "kind": "number", "max_length": 16},
    ]
    baseline_hash = hashlib.sha256(buggy.encode()).hexdigest()
    evidence_id = harness.record_evidence(
        task_id="task-h9-default-calculator",
        project_id=project_id,
        workspace=str(tmp_path.resolve()),
        version_hash=baseline_hash,
        source_type="user_correction",
        summary="discount result is ten times too large",
        usage={"calls": 1},
    )
    context.attributes["devpilot_harness_evidence_refs"] = [evidence_id]

    with request_context(context):
        for version, source in (("1.0.0", buggy), ("1.1.0", repaired)):
            defined = json.loads(
                await tool.execute(
                    action="define",
                    plugin_id="h9.discount-calculator",
                    version=version,
                    source=source,
                    input_rules=rules,
                )
            )
            assert defined["ok"], defined
        assert json.loads(
            await tool.execute(
                action="trial", plugin_id="h9.discount-calculator", version="1.0.0"
            )
        )["ok"]
        assert json.loads(
            await tool.execute(
                action="activate", plugin_id="h9.discount-calculator", version="1.0.0"
            )
        )["ok"]
        evaluated = json.loads(
            await tool.execute(
                action="evaluate",
                plugin_id="h9.discount-calculator",
                baseline_version="1.0.0",
                version="1.1.0",
                round_id="round-h9-default-calculator",
                max_candidates=2,
                max_evaluations=48,
                max_tokens=1000,
            )
        )

    assert evaluated["ok"] is True
    assert evaluated["result"]["eligible"] is True
    assert evaluated["result"]["total_runs"] == 24
    assert evaluated["result"]["baseline_successes"] == 2
    assert evaluated["result"]["candidate_successes"] == 12
    assert {tuple(sorted(request.input_data)) for request in runner.executions[-24:]} == {
        ("discount_percent", "price")
    }
