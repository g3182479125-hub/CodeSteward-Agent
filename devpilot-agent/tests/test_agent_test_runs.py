from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nanobot.agent.tools.context import RequestContext, request_context

import devpilot.nanobot_tools as nanobot_tools
from devpilot.code_access import CodeAccessService
from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.execution import (
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
    FakeExecutionBackend,
)
from devpilot.nanobot_tools import ProjectTestRunTool
from devpilot.patches import FilePatch
from devpilot.paths import AllowedRoots
from devpilot.services import ProjectAnalysisService
from devpilot.storage import ProjectStore
from devpilot.test_runs import ProjectTestRunner
from devpilot.workflows import BugFixWorkflow


def _trusted_store(tmp_path: Path, project: Path) -> tuple[ProjectStore, AllowedRoots]:
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    roots = AllowedRoots.from_paths([tmp_path])
    root_id, fingerprint = roots.binding_for(project)
    project_id = store.ensure_project(
        str(project.resolve()), allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    store.trust_project(project_id, allowed_root_id=root_id, repo_fingerprint=fingerprint)
    return store, roots


def _untrusted_store(tmp_path: Path, project: Path) -> tuple[ProjectStore, AllowedRoots]:
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    roots = AllowedRoots.from_paths([tmp_path])
    root_id, fingerprint = roots.binding_for(project)
    store.ensure_project(
        str(project.resolve()), allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    return store, roots


def test_agent_can_create_trusted_failure_run_from_a_project_pytest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text("from main import price\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)
    runner = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="1 failed"),
    )

    result = runner.run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "failed"
    assert result.failure_run_id is not None
    assert result.command == "python -m pytest -q"
    state = store.get_run_state(result.failure_run_id)
    assert state["kind"] == "test-check"
    assert state["status"] == "failed"
    event_types = [event["event_type"] for event in state["events"]]
    assert "test.execution.completed" in event_types
    assert "test_check.failed" in event_types
    assert result.error_code == "TEST_FAILED"
    assert result.error_category == "test_assertion"
    assert result.retryable is False


def test_execution_failure_has_structured_recovery_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class UnavailableBackend:
        name = "unavailable-backend"

        def run(self, _request: object) -> ExecutionResult:
            raise ExecutionError("SANDBOX_UNAVAILABLE", "provider is temporarily unavailable")

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=UnavailableBackend(),  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="test cannot run",
        expected_behavior="test should run",
    )

    assert result.status == "unknown"
    assert result.phase == "execution"
    assert result.error_code == "SANDBOX_UNAVAILABLE"
    assert result.error_category == "sandbox"
    assert result.retryable is True
    assert result.next_action is not None
    assert result.failure_run_id is None


def test_missing_pytest_uses_a_dependency_free_plain_runner(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return 80\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_main.py").write_text(
        "from main import price\n\n\ndef test_price():\n    assert price() == 80\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)

    class MissingPytestThenPlainRunner:
        name = "test-backend"

        def __init__(self) -> None:
            self.requests: list[object] = []

        def run(self, request: object) -> ExecutionResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ExecutionResult(
                    backend=self.name,
                    sandbox_id="sandbox-pytest",
                    exit_code=1,
                    stdout="",
                    stderr="/workspace/devpilot: No module named pytest",
                    duration_ms=1,
                    command_profile="project-pytest",
                    bundle_bytes=1,
                    output_truncated=False,
                    side_effect_count=0,
                    permission_violation=False,
                )
            return ExecutionResult(
                backend=self.name,
                sandbox_id="sandbox-plain",
                exit_code=0,
                stdout="plain-python-tests: passed",
                stderr="",
                duration_ms=1,
                command_profile="project-pytest",
                bundle_bytes=1,
                output_truncated=False,
                side_effect_count=0,
                permission_violation=False,
            )

    backend = MissingPytestThenPlainRunner()
    result = ProjectTestRunner(
        allowed_roots=roots, store=store, execution_backend=backend  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "passed"
    assert result.command == "python /workspace/devpilot/.devpilot/plain_test_runner.py"
    assert len(backend.requests) == 2
    fallback = backend.requests[1]
    assert isinstance(fallback, ExecutionRequest)
    assert fallback.run_id.endswith("-plain")
    assert any(
        item.relative_path == ".devpilot/plain_test_runner.py"
        for item in fallback.files
    )


def test_project_test_selects_nested_working_directory_and_dependency_profile(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    seed = project / "seed_repo"
    tests = seed / "tests"
    tests.mkdir(parents=True)
    (seed / "main.py").write_text("def price():\n    return 80\n", encoding="utf-8")
    (seed / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
    (tests / "test_main.py").write_text(
        "from main import price\n\n\ndef test_price():\n    assert price() == 80\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)
    backend = FakeExecutionBackend(exit_code=0)

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["seed_repo/main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "passed"
    assert result.working_directory == "seed_repo"
    assert result.dependency_profile == "python-requirements"
    assert len(result.dependency_fingerprint) == 64
    request = backend.requests[0]
    assert request.working_directory == "seed_repo"
    assert request.dependency_profile == "python-requirements"


def test_missing_pytest_does_not_turn_unsupported_fixture_into_failure_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return 80\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "main_test.py").write_text(
        "def test_price(tmp_path):\n    assert True\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)

    class MissingPytestThenUnsupportedRunner:
        name = "test-backend"

        def __init__(self) -> None:
            self.requests: list[object] = []

        def run(self, request: object) -> ExecutionResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ExecutionResult(
                    backend=self.name,
                    sandbox_id="sandbox-pytest",
                    exit_code=1,
                    stdout="",
                    stderr="No module named pytest",
                    duration_ms=1,
                    command_profile="project-pytest",
                    bundle_bytes=1,
                    output_truncated=False,
                    side_effect_count=0,
                    permission_violation=False,
                )
            return ExecutionResult(
                backend=self.name,
                sandbox_id="sandbox-plain",
                exit_code=1,
                stdout="",
                stderr="RuntimeError: unsupported pytest fixture: tests/main_test.py:test_price",
                duration_ms=1,
                command_profile="project-pytest",
                bundle_bytes=1,
                output_truncated=False,
                side_effect_count=0,
                permission_violation=False,
            )

    backend = MissingPytestThenUnsupportedRunner()
    result = ProjectTestRunner(
        allowed_roots=roots, store=store, execution_backend=backend  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["tests/main_test.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "unknown"
    assert result.failure_run_id is None
    fallback = backend.requests[1]
    assert isinstance(fallback, ExecutionRequest)
    runner = next(
        item for item in fallback.files if item.relative_path == ".devpilot/plain_test_runner.py"
    )
    assert b'rglob("*.py")' in runner.content
    assert b'test_file.name.startswith("test_")' in runner.content
    assert b'test_file.name.endswith("_test.py")' in runner.content
    plain_project = tmp_path / "plain-project"
    plain_tests = plain_project / "tests"
    plain_tests.mkdir(parents=True)
    (plain_tests / "conftest.py").write_text(
        "raise RuntimeError('conftest must not run')\n", encoding="utf-8"
    )
    (plain_project / "test_root.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    runner_path = plain_project / "plain_test_runner.py"
    runner_path.write_bytes(runner.content)
    plain_result = subprocess.run(
        [sys.executable, str(runner_path)],
        cwd=plain_project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert plain_result.returncode == 0, plain_result.stderr
    state = store.get_run_state(result.run_id)
    assert "test.execution.incomplete" in [event["event_type"] for event in state["events"]]
    assert "test_check.failed" not in [event["event_type"] for event in state["events"]]
    assert ProjectTestRunner._fallback_failure_is_unsupported(  # pyright: ignore[reportPrivateUsage]
        ExecutionResult(
            backend="test-backend",
            sandbox_id="sandbox-plain",
            exit_code=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'pytest'",
            duration_ms=1,
            command_profile="project-pytest",
            bundle_bytes=1,
            output_truncated=False,
            side_effect_count=0,
            permission_violation=False,
        )
    )


def test_project_test_api_requires_explicit_project_trust(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _untrusted_store(tmp_path, project)
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1),
    )

    with pytest.raises(Exception, match="PROJECT_TRUST_REQUIRED"):
        manager.run_project_test(
            project_path=project,
            test_kind="python-pytest",
            target_files=["main.py"],
            symptom="test fails",
            expected_behavior="test passes",
        )


def test_full_access_can_enroll_project_for_current_session_without_persistent_trust(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _untrusted_store(tmp_path, project)
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=0),
    )

    result = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="test cannot run",
        expected_behavior="test passes",
        session_id="websocket:full-access-session",
        session_access_mode="full",
    )

    assert result["status"] == "passed"
    assert result["phase"] == "test"
    assert result["sandbox_id"] == "fake-sandbox"
    security = store.get_project_security(result["project_id"])
    assert security["trust_state"] != "trusted"
    assert ("websocket:full-access-session", result["project_id"]) not in manager._session_project_bindings  # type: ignore[reportPrivateUsage]

    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
    response = client.post(
        "/v1/project-tests",
        headers={
            "Idempotency-Key": "full-access-project-test-1",
            "X-DevPilot-Workspace-Access": "full",
        },
        json={
            "project_path": str(project),
            "session_id": "websocket:full-access-http-session",
            "test_kind": "python-pytest",
            "target_files": ["main.py"],
            "symptom": "test cannot run",
            "expected_behavior": "test passes",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "passed"


def test_project_test_api_returns_a_real_failure_receipt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text("from main import price\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="1 failed"),
    )
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )

    response = client.post(
        "/v1/project-tests",
        headers={"Idempotency-Key": "project-test-1"},
        json={
            "project_path": str(project),
            "test_kind": "python-pytest",
            "target_files": ["main.py"],
            "symptom": "price test fails",
            "expected_behavior": "price test passes",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["failure_run_id"] == payload["run_id"]
    assert payload["phase"] == "test"
    assert payload["error_code"] == "TEST_FAILED"
    assert payload["error_category"] == "test_assertion"
    assert payload["retryable"] is False
    assert payload["sandbox_id"] == "fake-sandbox"
    assert store.get_run_state(payload["run_id"])["status"] == "failed"
    replay = client.post(
        "/v1/project-tests",
        headers={"Idempotency-Key": "project-test-1"},
        json={
            "project_path": str(project),
            "test_kind": "python-pytest",
            "target_files": ["main.py"],
            "symptom": "price test fails",
            "expected_behavior": "price test passes",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["run_id"] == payload["run_id"]


def test_missing_test_entry_does_not_create_a_failure_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)
    starts: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        store,
        "start_run",
        lambda *args, **kwargs: starts.append((*args, *kwargs.values())) or "not-used",
    )

    with pytest.raises(Exception, match="PROJECT_TEST_INVALID"):
        ControlPlaneManager(
            allowed_roots=roots,
            store=store,
            execution_backend=FakeExecutionBackend(exit_code=1),
        ).run_project_test(
            project_path=project,
            test_kind="python-pytest",
            target_files=["main.py"],
            symptom="price test fails",
            expected_behavior="price test passes",
        )
    assert starts == []


def test_agent_can_select_the_allowlisted_node_test_kind(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(
        '{"scripts":{"test":"node --test"}}\n', encoding="utf-8"
    )
    (project / "index.js").write_text("export const price = -100;\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="1 failing test"),
    ).run(
        project_path=project,
        test_kind="node-test",
        target_files=["index.js"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.command == "npm test"
    assert result.status == "failed"


def test_changed_project_file_cannot_become_failure_evidence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text("from main import price\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class ChangingBackend:
        name = "changing-test-backend"

        def run(self, _request: object) -> ExecutionResult:
            source.write_text("def price():\n    return -80\n", encoding="utf-8")
            return ExecutionResult(
                backend=self.name,
                sandbox_id="sandbox-1",
                exit_code=1,
                stdout="1 failed",
                stderr="",
                duration_ms=1,
                command_profile="project-pytest",
                bundle_bytes=1,
                output_truncated=False,
                side_effect_count=0,
                permission_violation=False,
            )

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=ChangingBackend(),  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "unknown"
    assert result.failure_run_id is None
    events = store.list_run_events(result.run_id)
    assert [event["event_type"] for event in events].count("test_check.failed") == 0
    assert "test.failure_fact.rejected" in [event["event_type"] for event in events]


def test_truncated_test_receipt_cannot_become_failure_evidence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text("from main import price\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class TruncatedBackend:
        name = "truncated-test-backend"

        def run(self, _request: object) -> ExecutionResult:
            return ExecutionResult(
                backend=self.name,
                sandbox_id="sandbox-1",
                exit_code=1,
                stdout="truncated",
                stderr="",
                duration_ms=1,
                command_profile="project-pytest",
                bundle_bytes=1,
                output_truncated=True,
                side_effect_count=0,
                permission_violation=False,
            )

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=TruncatedBackend(),  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "unknown"
    assert result.failure_run_id is None
    assert "test.execution.incomplete" in [
        event["event_type"] for event in store.list_run_events(result.run_id)
    ]


def test_bug_fix_proposal_accepts_a_server_owned_project_test_failure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text(
        "from main import price\n\n\ndef test_price():\n    assert price() == -80\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)
    analysis = ProjectAnalysisService(allowed_roots=roots, store=store).inspect(project)
    access = CodeAccessService(allowed_roots=roots, store=store)
    access.search(analysis.project_id, "price", evidence_run_id=analysis.run_id)
    access.read(analysis.project_id, "main.py", evidence_run_id=analysis.run_id)
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="1 failed"),
        artifacts_root=tmp_path / "data",
    )
    failure = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price returns the discounted amount",
    )
    before = source.read_text(encoding="utf-8")
    failure_event = [
        event
        for event in store.list_run_events(str(failure["failure_run_id"]))
        if event["event_type"] == "test_check.failed"
    ][0]
    assert failure_event["payload"]["before_hashes"]["main.py"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    proposal = BugFixWorkflow(manager, artifacts_root=tmp_path / "data").propose(
        project_path=project,
        patches=[
            FilePatch(
                "main.py",
                FileOperation.UPDATE,
                before.replace("-100", "-80"),
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        ],
        analysis_evidence_ids=[analysis.run_id],
        failure_evidence_ids=[str(failure["failure_run_id"])],
        symptom="price test fails",
        expected_behavior="price returns the discounted amount",
        non_goals=["dependency changes"],
        allowed_files=["main.py"],
    )

    assert proposal.preview["failure_evidence_ids"] == (failure["failure_run_id"],)


def test_agent_tool_posts_only_a_structured_project_test_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        nanobot_tools,
        "_post_project_test",
        lambda payload: captured.update(payload) or {"status": "failed", "run_id": "run-1"},
    )

    result = asyncio.run(
        ProjectTestRunTool().execute(
            project_path="E:/workspace/project",
            test_kind="python-pytest",
            target_files=["main.py"],
            symptom="price test fails",
            expected_behavior="price test passes",
        )
    )

    assert "run-1" in str(result)
    assert captured["test_kind"] == "python-pytest"
    assert ProjectTestRunTool().read_only is True


def test_agent_project_test_tool_resolves_relative_path_from_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        nanobot_tools,
        "_post_project_test",
        lambda payload: captured.update(payload) or {"status": "passed", "run_id": "run-2"},
    )

    with request_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            workspace=tmp_path,
        )
    ):
        result = asyncio.run(
            ProjectTestRunTool().execute(
                project_path="loop-engineering-demo-external",
                test_kind="python-pytest",
                target_files=["seed_repo/tests/test_pricing.py"],
                symptom="test fails",
                expected_behavior="test passes",
            )
        )

    assert "run-2" in str(result)
    assert captured["project_path"] == str(tmp_path / "loop-engineering-demo-external")


def test_project_test_request_waits_for_the_fc_execution_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"status":"failed","run_id":"run-1"}'

    def fake_urlopen(_request: object, timeout: int) -> FakeResponse:
        assert timeout == 330
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = nanobot_tools._post_project_test(
        {
            "project_path": "E:/workspace/project",
            "test_kind": "python-pytest",
            "target_files": ["tests/main_test.py"],
            "symptom": "tests fail",
            "expected_behavior": "tests pass",
        }
    )

    assert result["run_id"] == "run-1"
