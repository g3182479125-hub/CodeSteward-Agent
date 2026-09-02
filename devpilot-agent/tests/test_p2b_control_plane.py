from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.execution import FakeExecutionBackend
from devpilot.nanobot_tools import (
    ExecuteChangeSetTool,
    ProposeChangeSetTool,
    ToolResult,
    VerifyChangeSetTool,
)
from devpilot.patches import FilePatch, PatchError
from devpilot.paths import AllowedRoots
from devpilot.verification import VerificationProfile


def make_manager(tmp_path: Path) -> tuple[ControlPlaneManager, Path]:
    project = tmp_path / "project"
    project.mkdir()
    return (
        ControlPlaneManager(allowed_roots=AllowedRoots.from_paths([tmp_path])),
        project,
    )


def test_control_plane_can_verify_applied_changeset_in_backend(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    backend = FakeExecutionBackend(stdout="syntax ok")
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        execution_backend=backend,
    )
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))

    verify_approval = manager.approve(change_set_id, "r2.verify")
    verified = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_SYNTAX,
    )

    assert verified["status"] == "verified"
    assert backend.requests[0].command.startswith("python3 -m py_compile")
    assert backend.requests[0].files[0].content == b"print('ok')\n"

    rollback_approval = manager.approve(change_set_id, "r1.rollback")
    reverted = manager.rollback(change_set_id, str(rollback_approval["approval_token"]))
    assert reverted["status"] == "reverted"


def test_control_plane_can_run_python_self_test_in_backend(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    backend = FakeExecutionBackend(stdout="all self-tests passed")
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        execution_backend=backend,
    )
    content = "assert 2 + 2 == 4\nprint('all self-tests passed')\n"
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, content)],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))

    verify_approval = manager.approve(change_set_id, "r2.verify")
    verified = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_RUN,
    )

    assert verified["status"] == "verified"
    assert backend.requests[0].command == "python3 /workspace/devpilot/main.py"
    assert backend.requests[0].network_allowed is False
    assert backend.requests[0].files[0].content == content.encode("utf-8")


def test_control_plane_fail_closed_for_non_fake_backend_without_p6_gate(tmp_path: Path) -> None:
    class RemoteBackend:
        name = "fc-agent-sandbox"

        def run(self, _request: object) -> object:
            raise AssertionError("remote backend must not start before P6 gate")

    project = tmp_path / "project"
    project.mkdir()
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        execution_backend=RemoteBackend(),  # type: ignore[arg-type]
    )
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    with pytest.raises(PatchError, match="EXECUTION_BACKEND_UNAVAILABLE"):
        manager.verify(change_set_id, str(verify_approval["approval_token"]))


def test_python_run_rejects_multiple_python_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        execution_backend=FakeExecutionBackend(),
    )
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch("main.py", FileOperation.CREATE, "print('main')\n"),
            FilePatch("helper.py", FileOperation.CREATE, "print('helper')\n"),
        ],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    with pytest.raises(PatchError, match="VERIFICATION_PROFILE_UNSUPPORTED"):
        manager.verify(
            change_set_id,
            str(verify_approval["approval_token"]),
            profile=VerificationProfile.PYTHON_RUN,
        )


def test_control_plane_manager_preview_approve_apply_and_rollback(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path)
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("src/main.py", FileOperation.CREATE, "print('ok')\n")],
    )

    assert preview["status"] == "planned"
    assert "+++ b/src/main.py" in preview["diff"]
    change_set_id = str(preview["change_set_id"])

    approval = manager.approve(change_set_id)
    applied = manager.apply(change_set_id, str(approval["approval_token"]))
    assert applied["status"] == "applied"
    assert (project / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')\n"

    rollback_approval = manager.approve(change_set_id, "r1.rollback")
    reverted = manager.rollback(change_set_id, str(rollback_approval["approval_token"]))
    assert reverted["status"] == "reverted"
    assert not (project / "src" / "main.py").exists()


def test_control_plane_requires_approval_and_consumes_token(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path)
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "new\n")],
    )
    change_set_id = str(preview["change_set_id"])

    with pytest.raises(PatchError, match="APPROVAL_INVALID"):
        manager.apply(change_set_id, "missing")

    token = str(manager.approve(change_set_id)["approval_token"])
    manager.apply(change_set_id, token)
    with pytest.raises(PatchError, match="APPROVAL_INVALID"):
        manager.apply(change_set_id, token)


def test_control_plane_stale_plan_has_zero_controlled_writes(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path)
    target = project / "main.py"
    target.write_text("old\n", encoding="utf-8", newline="")
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch(
                "main.py",
                FileOperation.UPDATE,
                "new\n",
                hashlib.sha256(b"old\n").hexdigest(),
            )
        ],
    )
    target.write_text("user changed\n", encoding="utf-8", newline="")
    token = str(manager.approve(str(preview["change_set_id"]))["approval_token"])

    with pytest.raises(PatchError, match="PRECONDITION_HASH_MISMATCH"):
        manager.apply(str(preview["change_set_id"]), token)
    assert target.read_text(encoding="utf-8") == "user changed\n"


def test_proposal_tool_only_returns_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    from devpilot import nanobot_tools

    response = {"change_set_id": "cs-1", "status": "planned", "diff": "safe diff"}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(response).encode("utf-8")

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        assert "127.0.0.1:18800/v1/change-sets/preview" in str(getattr(request, "full_url", ""))
        assert timeout == 10
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = asyncio.run(
        ProposeChangeSetTool().execute(
            project_path="E:/project",
            patches=[{"relative_path": "main.py", "operation": "create", "content": "ok\n"}],
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    payload = json.loads(str(result))
    assert payload["status"] == "planned"
    assert payload["requires_user_confirmation"] is True
    assert "devpilot_execute_changeset" in payload["next_step"]


def test_proposal_tool_rejects_non_local_control_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVPILOT_CONTROL_PLANE_URL", "https://example.invalid")
    result = asyncio.run(
        ProposeChangeSetTool().execute(
            project_path="E:/project",
            patches=[{"relative_path": "main.py", "operation": "create", "content": "ok\n"}],
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "ValueError" in str(result)


def test_execute_changeset_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    from devpilot import nanobot_tools

    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("control plane must not be called without confirmation")

    monkeypatch.setattr(nanobot_tools, "_apply_changeset", unexpected_call)
    result = asyncio.run(
        ExecuteChangeSetTool().execute(
            change_set_id="cs-1",
            verification_profile="python-run",
            approval_token="apply-token",
            user_confirmation="不执行",
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "confirmation required" in str(result)


def test_execute_changeset_applies_with_external_token_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import nanobot_tools

    responses = [
        {"change_set_id": "cs-1", "status": "applied"},
    ]

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(responses.pop(0)).encode("utf-8")

    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        req = request
        body = json.loads(req.data.decode("utf-8"))
        calls.append((str(req.full_url), str(req.method), body))
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = asyncio.run(
        ExecuteChangeSetTool().execute(
            change_set_id="cs-1",
            verification_profile="python-run",
            approval_token="apply-token",
            user_confirmation="执行",
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    payload = json.loads(str(result))
    assert payload["status"] == "applied"
    assert [call[1] for call in calls] == ["POST"]
    assert calls[0][0].endswith("/cs-1/apply")


def test_verify_changeset_uses_external_token_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from devpilot import nanobot_tools

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(
                {"change_set_id": "cs-1", "status": "verified", "exit_code": 0}
            ).encode("utf-8")

    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        req = request
        calls.append(
            (
                str(req.full_url),
                str(req.method),
                json.loads(req.data.decode("utf-8")),
            )
        )
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = asyncio.run(
        VerifyChangeSetTool().execute(
            change_set_id="cs-1",
            verification_profile="python-run",
            approval_token="verify-token",
            user_confirmation="执行",
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    assert json.loads(str(result))["status"] == "verified"
    assert len(calls) == 1
    assert calls[0][0].endswith("/cs-1/verify")
    assert calls[0][2] == {"profile": "python-run"}


def test_execute_changeset_rejects_unsafe_id_before_control_plane_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import nanobot_tools

    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unsafe ChangeSet ID must not reach the control plane")

    monkeypatch.setattr(nanobot_tools, "_post_control_plane", unexpected_call)
    result = asyncio.run(
        ExecuteChangeSetTool().execute(
            change_set_id="../../etc/passwd",
            verification_profile="python-run",
            approval_token="apply-token",
            user_confirmation="执行",
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "ValueError" in str(result)


def test_create_app_exposes_only_local_control_plane_routes(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    app = create_app(manager=manager)
    routes = {route.path for route in app.routes}

    assert "/healthz" in routes
    assert "/v1/change-sets/preview" in routes
    assert "/v1/change-sets/{change_set_id}/apply" in routes
    assert "/v1/change-sets/{change_set_id}/rollback" in routes
