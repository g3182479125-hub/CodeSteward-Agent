from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.nanobot_tools import ProposeChangeSetTool, ToolResult
from devpilot.patches import FilePatch, PatchError
from devpilot.paths import AllowedRoots


def make_manager(tmp_path: Path) -> tuple[ControlPlaneManager, Path]:
    project = tmp_path / "project"
    project.mkdir()
    return (
        ControlPlaneManager(allowed_roots=AllowedRoots.from_paths([tmp_path])),
        project,
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

    reverted = manager.rollback(change_set_id)
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

    with pytest.raises(PatchError, match="APPROVAL_REQUIRED"):
        manager.apply(change_set_id, "missing")

    token = str(manager.approve(change_set_id)["approval_token"])
    manager.apply(change_set_id, token)
    with pytest.raises(PatchError, match="APPROVAL_REQUIRED"):
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
    assert json.loads(str(result))["status"] == "planned"


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


def test_create_app_exposes_only_local_control_plane_routes(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    app = create_app(manager=manager)
    routes = {route.path for route in app.routes}

    assert "/healthz" in routes
    assert "/v1/change-sets/preview" in routes
    assert "/v1/change-sets/{change_set_id}/apply" in routes
    assert "/v1/change-sets/{change_set_id}/rollback" in routes
