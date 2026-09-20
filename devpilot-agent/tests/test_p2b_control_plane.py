from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from devpilot.control_plane import (
    ControlPlaneManager,
    _candidate_trial_runner_from_environment,
    create_app,
)
from devpilot.domain import FileOperation
from devpilot.execution import FakeExecutionBackend
from devpilot.fc_sandbox import FCSandboxBackend, FCSandboxConfig
from devpilot.nanobot_tools import (
    ExecuteChangeSetTool,
    ProposeChangeSetTool,
    ToolResult,
    VerifyChangeSetTool,
)
from devpilot.patches import FilePatch, PatchError
from devpilot.paths import AllowedRoots
from devpilot.storage import ProjectStore
from devpilot.verification import VerificationProfile


def make_manager(
    tmp_path: Path, execution_backend: object | None = None
) -> tuple[ControlPlaneManager, Path]:
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    roots = AllowedRoots.from_paths([tmp_path])
    root_id, fingerprint = roots.binding_for(project)
    project_id = store.ensure_project(
        str(project), allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    store.trust_project(
        project_id, allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    return (
        ControlPlaneManager(
            allowed_roots=roots,
            store=store,
            execution_backend=execution_backend,  # type: ignore[arg-type]
        ),
        project,
    )


def test_control_plane_can_verify_applied_changeset_in_backend(tmp_path: Path) -> None:
    backend = FakeExecutionBackend(stdout="syntax ok")
    manager, project = make_manager(tmp_path, execution_backend=backend)
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


def test_full_access_can_apply_and_verify_without_approval_token(tmp_path: Path) -> None:
    backend = FakeExecutionBackend(stdout="syntax ok")
    manager, project = make_manager(tmp_path, execution_backend=backend)
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )

    result = manager.full_access_apply_verify(
        str(preview["change_set_id"]),
        idempotency_key="full-access-direct-test",
    )

    assert result["mode"] == "full_access"
    assert result["status"] == "verified"
    assert (project / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert backend.requests[0].command.startswith("python3 -m py_compile")


def test_full_access_http_action_requires_internal_adapter_auth(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path, execution_backend=FakeExecutionBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    token = "full-access-control-plane-token-0123456789"
    app = create_app(manager=manager, internal_auth_token=token)

    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        rejected = client.post(
            f"/v1/change-sets/{change_set_id}/full-access-apply-verify",
            json={"profile": "python-syntax"},
            headers={"Idempotency-Key": "full-access-http-test"},
        )
        accepted = client.post(
            f"/v1/change-sets/{change_set_id}/full-access-apply-verify",
            json={"profile": "python-syntax"},
            headers={
                "Idempotency-Key": "full-access-http-test",
                "X-DevPilot-Internal-Token": token,
            },
        )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "verified"


def test_control_plane_can_run_python_self_test_in_backend(tmp_path: Path) -> None:
    backend = FakeExecutionBackend(stdout="all self-tests passed")
    manager, project = make_manager(tmp_path, execution_backend=backend)
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


def test_control_plane_routes_python_regression_to_fc_pytest_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, project = make_manager(tmp_path)
    (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_main.py").write_text(
        "def test_main():\n    assert True\n", encoding="utf-8"
    )
    events: list[tuple[object, ...]] = []

    class FakeFiles:
        def make_dir(self, path: str) -> None:
            events.append(("mkdir", path))

        def write(self, path: str, content: bytes) -> None:
            events.append(("write", path, content))

    class FakeCommands:
        def run(
            self, command: str, *, timeout: int, cwd: str | None = None
        ) -> SimpleNamespace:
            events.append(("run", command, timeout, cwd))
            return SimpleNamespace(exit_code=0, stdout="5 passed", stderr="")

    class FakeSandbox:
        sandbox_id = "control-plane-regression-sandbox"
        files = FakeFiles()
        commands = FakeCommands()

        def kill(self) -> None:
            events.append(("kill",))

    class Factory:
        @classmethod
        def create(cls, **_kwargs: object) -> FakeSandbox:
            return FakeSandbox()

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=Factory))
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            network_policy_mode="provider-default",
            allowed_project_paths=(str(project),),
            state_dir=str(tmp_path / "fc-state"),
        )
    )
    manager.execution_backend = backend
    expected_sha = hashlib.sha256((project / "main.py").read_bytes()).hexdigest()
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch(
                "main.py",
                FileOperation.UPDATE,
                "VALUE = 2\n",
                expected_sha,
            )
        ],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))

    verify_approval = manager.approve(change_set_id, "r2.verify")
    verified = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_REGRESSION,
    )

    assert verified["status"] == "verified"
    assert verified["command_profile"] == "python-regression"
    assert [event[:2] for event in events if event[0] == "run"] == [
        ("run", "python -m pip install --disable-pip-version-check pytest"),
        ("run", "python -m pytest -q"),
    ]


def test_control_plane_fail_closed_for_non_fake_backend_without_p6_gate(tmp_path: Path) -> None:
    class RemoteBackend:
        name = "fc-agent-sandbox"

        def run(self, _request: object) -> object:
            raise AssertionError("remote backend must not start before P6 gate")

    manager, project = make_manager(tmp_path, execution_backend=RemoteBackend())
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


def test_control_plane_allows_explicitly_available_remote_backend(tmp_path: Path) -> None:
    class AvailableBackend:
        name = "remote-test-double"
        verification_available = True

        def __init__(self) -> None:
            self.delegate = FakeExecutionBackend(stdout="remote verification ok")

        def run(self, request: object) -> object:
            return self.delegate.run(request)  # type: ignore[arg-type]

        def supports_profile(self, profile: str) -> bool:
            return profile == "python-syntax"

        def project_allowed(self, _project_path: str) -> bool:
            return True

    manager, project = make_manager(tmp_path, execution_backend=AvailableBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    result = manager.verify(change_set_id, str(verify_approval["approval_token"]))

    assert result["status"] == "verified"


def test_control_plane_remote_backend_rejects_unproven_profile(tmp_path: Path) -> None:
    class ProfileBoundBackend:
        name = "fc-agent-sandbox"
        verification_available = True

        def supports_profile(self, profile: str) -> bool:
            return profile in {"python-syntax", "node-syntax"}

        def project_allowed(self, _project_path: str) -> bool:
            return True

        def run(self, _request: object) -> object:
            raise AssertionError("react-static must be rejected before provider execution")

    manager, project = make_manager(tmp_path, execution_backend=ProfileBoundBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch("Form.jsx", FileOperation.CREATE, "export default function Form() {}\n")
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
            profile=VerificationProfile.REACT_STATIC,
        )


def test_control_plane_remote_backend_requires_project_allowlist(tmp_path: Path) -> None:
    class AllowlistBackend:
        name = "fc-agent-sandbox"
        verification_available = True

        def supports_profile(self, _profile: str) -> bool:
            return True

        def project_allowed(self, _project_path: str) -> bool:
            return False

        def run(self, _request: object) -> object:
            raise AssertionError("non-allowlisted project must not reach provider")

    manager, project = make_manager(tmp_path, execution_backend=AllowlistBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    with pytest.raises(PatchError, match="EXECUTION_PROJECT_NOT_ALLOWLISTED"):
        manager.verify(change_set_id, str(verify_approval["approval_token"]))


def test_python_run_rejects_multiple_python_files(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path, execution_backend=FakeExecutionBackend())
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


def test_ephemeral_manager_fails_closed_before_r1_or_r2_operation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = ControlPlaneManager(allowed_roots=AllowedRoots.from_paths([tmp_path]))
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])

    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.request_approval(change_set_id, "r1.apply", "执行")
    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.approve(change_set_id)
    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.apply(change_set_id, "unused", idempotency_key="apply-key")
    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.verify(change_set_id, "unused", idempotency_key="verify-key")
    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.rollback(change_set_id, "unused", idempotency_key="rollback-key")
    assert not (project / "main.py").exists()


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
        request_body = json.loads(bytes(getattr(request, "data", b"")).decode("utf-8"))
        assert request_body["patches"][0]["expected_sha256"] == "a" * 64
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = asyncio.run(
        ProposeChangeSetTool().execute(
            project_path="E:/project",
            patches=[
                {
                    "relative_path": "main.py",
                    "operation": "create",
                    "content": "ok\n",
                    "expected_sha256": "A" * 64,
                }
            ],
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    payload = json.loads(str(result))
    assert payload["status"] == "planned"
    assert payload["requires_user_confirmation"] is True
    assert "devpilot_confirmed_action" in payload["next_step"]
    assert "devpilot_request_approval" not in payload["next_step"]


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


def test_h5_factory_injects_durable_store_fc_backend_and_host_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path / "candidate" / "devpilot.db")
    store.initialize()
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            network_policy_mode="provider-default",
        )
    )
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_CANDIDATE_GRANT_SECRET", "h5-test-secret-012345678901234567890")

    runner = _candidate_trial_runner_from_environment(
        store=store, execution_backend=backend
    )
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=backend,
        candidate_trial_runner=runner,
    )

    assert runner is not None
    assert manager.candidate_trial_runner is runner
