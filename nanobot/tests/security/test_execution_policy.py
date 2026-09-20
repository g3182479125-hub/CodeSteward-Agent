"""Contract tests for unified execution decisions and tool failure metadata."""

from __future__ import annotations

import pytest

from nanobot.agent.hook import AgentHookContext
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.security.execution_policy import (
    ExecutionDecision,
    ExecutionErrorCode,
    classify_exception,
)
from nanobot.security.workspace_policy import WorkspaceBoundaryError
from nanobot.utils.progress_events import build_tool_event_finish_payloads


def test_policy_classification_does_not_confuse_os_permission_with_policy() -> None:
    policy = classify_exception(WorkspaceBoundaryError("outside workspace"))
    operating_system = classify_exception(PermissionError("access denied"))

    assert policy.code is ExecutionErrorCode.POLICY_DENIED
    assert policy.decision is ExecutionDecision.BLOCKED_BY_POLICY
    assert not policy.retryable
    assert operating_system.code is ExecutionErrorCode.OS_PERMISSION_DENIED
    assert operating_system.decision is ExecutionDecision.PRECONDITION_FAILED


def test_tool_result_metadata_is_string_compatible() -> None:
    result = ToolResult.error(
        "blocked",
        code=ExecutionErrorCode.POLICY_DENIED,
        decision=ExecutionDecision.BLOCKED_BY_POLICY,
        retryable=False,
    )

    assert str(result) == "blocked"
    assert result.is_error
    assert result.code == "POLICY_DENIED"
    assert result.decision == "BLOCKED_BY_POLICY"
    assert result.retryable is False
    assert result.with_content("blocked\n\nhint").code == "POLICY_DENIED"


def test_shell_safety_guard_is_machine_readable(tmp_path) -> None:
    result = ExecTool(working_dir=str(tmp_path))._guard_command("rm -rf target", str(tmp_path))

    assert isinstance(result, ToolResult)
    assert result.is_error
    assert result.code == "POLICY_DENIED"
    assert result.decision == "BLOCKED_BY_POLICY"
    assert result.retryable is False


def test_progress_event_exposes_execution_metadata() -> None:
    result = ToolResult.error(
        "blocked",
        code=ExecutionErrorCode.POLICY_DENIED,
        decision=ExecutionDecision.BLOCKED_BY_POLICY,
        retryable=False,
    )
    context = AgentHookContext(iteration=1, messages=[])
    context.tool_calls = []
    context.tool_results = [result]
    context.tool_events = [{"name": "exec", "status": "error"}]

    # The helper uses the tool-call list as the stable join key.  Construct a
    # tiny request object rather than coupling this contract test to a model.
    from nanobot.providers.base import ToolCallRequest

    context.tool_calls = [ToolCallRequest(id="call-1", name="exec", arguments={})]
    payload = build_tool_event_finish_payloads(context)[0]

    assert payload["code"] == "POLICY_DENIED"
    assert payload["decision"] == "BLOCKED_BY_POLICY"
    assert payload["retryable"] is False


@pytest.mark.asyncio
async def test_allowed_shell_command_failure_is_not_reported_as_policy_denial() -> None:
    result = await ExecTool(timeout=5).execute(command="python -c \"raise SystemExit(7)\"")

    assert isinstance(result, ToolResult)
    assert result.is_error
    assert "Exit code: 7" in result
    assert result.code == "COMMAND_FAILED"
    assert result.decision == "AUTO_EXECUTE"


@pytest.mark.asyncio
async def test_restricted_file_failure_is_machine_readable(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    result = await ReadFileTool(workspace=workspace, allowed_dir=workspace).execute(path=str(secret))

    assert result.is_error
    assert result.code == "POLICY_DENIED"
    assert result.decision == "BLOCKED_BY_POLICY"
    assert result.retryable is False


class _FailingTool(Tool):
    @property
    def name(self) -> str:
        return "failing_test_tool"

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult.error(
            "policy failure",
            code=ExecutionErrorCode.POLICY_DENIED,
            decision=ExecutionDecision.BLOCKED_BY_POLICY,
            retryable=False,
        )


@pytest.mark.asyncio
async def test_registry_preserves_failure_metadata_when_adding_hint() -> None:
    registry = ToolRegistry()
    registry.register(_FailingTool())

    result = await registry.execute("failing_test_tool", {})

    assert result.is_error
    assert "Analyze the error" in result
    assert result.code == "POLICY_DENIED"
    assert result.decision == "BLOCKED_BY_POLICY"
    assert result.retryable is False
