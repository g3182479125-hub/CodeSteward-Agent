from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from devpilot.control_plane import ControlPlaneManager
from devpilot.domain import FileOperation
from devpilot.execution import (
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
    FakeExecutionBackend,
)
from devpilot.fc_sandbox import FCSandboxBackend, FCSandboxConfig
from devpilot.patches import FilePatch
from devpilot.paths import AllowedRoots
from devpilot.verification import VerificationProfile, build_verification_request


def test_verification_uses_complete_bounded_snapshot_and_patch_overlay(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "helper.py").write_text("VALUE = 41\n", encoding="utf-8")
    (project / ".env").write_text("E2B_API_KEY=should-not-leave\n", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "ignored.py").write_text("raise RuntimeError\n", encoding="utf-8")
    manager = ControlPlaneManager(allowed_roots=AllowedRoots.from_paths([tmp_path]))
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch(
                "main.py",
                FileOperation.CREATE,
                "from helper import VALUE\nassert VALUE == 42\n",
            )
        ],
    )
    entry = manager._require(str(preview["change_set_id"]))

    request = build_verification_request(entry.plan, VerificationProfile.PYTHON_SYNTAX)
    snapshot = {item.relative_path: item.content for item in request.files}
    assert snapshot["main.py"] == b"from helper import VALUE\nassert VALUE == 42\n"
    assert snapshot["helper.py"].replace(b"\r\n", b"\n") == b"VALUE = 41\n"
    assert ".env" not in snapshot
    assert "node_modules/ignored.py" not in snapshot
    assert request.command == "python3 -m py_compile /workspace/devpilot/main.py"
    assert request.command_profile == "python-syntax"


def test_execution_port_rejects_model_supplied_shell_or_non_allowlisted_command() -> None:
    request = ExecutionRequest(
        run_id="run-1",
        project_id="project-1",
        files=(ExecutionFile("main.py", b"print(1)"),),
        command="python3 -c \"print(1)\" && whoami",
    )
    with pytest.raises(ExecutionError, match="EXECUTION_COMMAND_FORBIDDEN"):
        FakeExecutionBackend().run(request)


def test_execution_port_enforces_bundle_size() -> None:
    request = ExecutionRequest(
        run_id="run-1",
        project_id="project-1",
        files=(ExecutionFile("main.py", b"x" * (512 * 1024 + 1)),),
        command="python3 /workspace/devpilot/main.py",
    )
    with pytest.raises(ExecutionError, match="EXECUTION_FILE_LIMIT_EXCEEDED"):
        FakeExecutionBackend().run(request)


def test_fc_result_contains_audit_metadata_and_bounded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFiles:
        def make_dir(self, path: str) -> None:
            return None

        def write(self, path: str, content: bytes) -> None:
            return None

    class FakeCommands:
        def run(self, command: str, *, timeout: int) -> SimpleNamespace:
            return SimpleNamespace(
                exit_code=0,
                stdout="x" * 20_000,
                stderr="y" * 20_000,
            )

    class FakeSandbox:
        sandbox_id = "audit-sandbox"
        files = FakeFiles()
        commands = FakeCommands()

        def kill(self) -> None:
            return None

    class Factory:
        create_kwargs: dict[str, object] = {}

        @classmethod
        def create(cls, **kwargs: object) -> FakeSandbox:
            cls.create_kwargs = kwargs
            return FakeSandbox()

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=Factory))
    result = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="cn-hangzhou.e2b.fc.aliyuncs.com",
            template="code-interpreter-v1",
        )
    ).run(
        ExecutionRequest(
            run_id="run-1",
            project_id="project-1",
            files=(ExecutionFile("main.py", b"print(1)"),),
            command="python3 /workspace/devpilot/main.py",
            command_profile="python-run",
        )
    )
    assert len(result.stdout) == 16 * 1024
    assert len(result.stderr) == 16 * 1024
    assert result.command_profile == "python-run"
    assert result.bundle_bytes == len(b"print(1)")
    assert result.region == ""
    assert result.template == "code-interpreter-v1"
    assert Factory.create_kwargs["allow_internet_access"] is False
