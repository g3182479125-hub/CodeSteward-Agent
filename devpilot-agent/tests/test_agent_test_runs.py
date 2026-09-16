from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import devpilot.nanobot_tools as nanobot_tools
from devpilot.code_access import CodeAccessService
from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.execution import ExecutionResult, FakeExecutionBackend
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
    assert result.command == "python3 -m pytest -q"
    state = store.get_run_state(result.failure_run_id)
    assert state["kind"] == "test-check"
    assert state["status"] == "failed"
    event_types = [event["event_type"] for event in state["events"]]
    assert "test.execution.completed" in event_types
    assert "test_check.failed" in event_types


def test_project_test_api_requires_explicit_project_trust(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
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


def test_project_test_api_returns_a_real_failure_receipt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
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
    assert store.get_run_state(payload["run_id"])["status"] == "failed"


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
