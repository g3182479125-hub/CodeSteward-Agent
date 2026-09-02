from __future__ import annotations

from pathlib import Path

import pytest

from devpilot.control_plane import ControlPlaneManager
from devpilot.domain import FileOperation
from devpilot.execution import ExecutionRequest, ExecutionResult, FakeExecutionBackend
from devpilot.patches import FilePatch
from devpilot.paths import AllowedRoots
from devpilot.skill_manager import validate_skill
from devpilot.storage import ArtifactRecord, ProjectStore
from devpilot.workflows import FeatureDeliveryWorkflow, WorkflowError


def _manager(tmp_path: Path, backend: object) -> tuple[ProjectStore, Path, ControlPlaneManager]:
    project = tmp_path / "fastapi-fixture"
    project.mkdir()
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )
    return store, project, manager


def _patch() -> FilePatch:
    return FilePatch(
        "app.py",
        FileOperation.CREATE,
        'def health() -> dict[str, str]:\n    return {"status": "ok"}\n',
    )


def test_feature_delivery_g01_completes_approved_fake_path_and_artifact(
    tmp_path: Path,
) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend(stdout="fake-ok"))
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")

    proposal = workflow.propose(
        project_path=project,
        patches=[_patch()],
        evidence_ids=["inspection-run-1", "read-app-location-1"],
    )

    assert proposal.to_dict()["requires_user_confirmation"] is True
    assert not (project / "app.py").exists()

    result = workflow.execute(proposal, user_confirmation="执行")

    assert result.status == "verified"
    assert result.apply["status"] == "applied"
    assert result.verification["backend"] == "fake"
    assert result.verification["exit_code"] == 0
    assert (project / "app.py").read_text(encoding="utf-8").startswith("def health")
    assert proposal.run_id is not None
    expected_artifact = (
        tmp_path
        / "data"
        / "artifacts"
        / proposal.project_id
        / proposal.run_id
        / "feature-delivery.md"
    )
    assert result.artifact.path == expected_artifact
    report = result.artifact.path.read_text(encoding="utf-8")
    assert "feature-delivery" in report
    assert proposal.patch_hash in report
    assert "fake-ok" not in report
    assert store.count(ArtifactRecord) == 1


def test_feature_delivery_never_applies_without_exact_user_confirmation(tmp_path: Path) -> None:
    _store, project, manager = _manager(tmp_path, FakeExecutionBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch()],
        evidence_ids=["inspection-1"],
    )

    with pytest.raises(WorkflowError, match="USER_CONFIRMATION_REQUIRED"):
        workflow.execute(proposal, user_confirmation="现在执行")

    assert not (project / "app.py").exists()


class _UnapprovedRemoteBackend:
    name = "fc-agent-sandbox"

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        raise AssertionError("P4 must not start real execution before the P6 Gate")


def test_feature_delivery_blocks_real_backend_before_p6(tmp_path: Path) -> None:
    _store, project, manager = _manager(tmp_path, _UnapprovedRemoteBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch()],
        evidence_ids=["inspection-1"],
    )

    with pytest.raises(WorkflowError, match="AUTONOMOUS_REPAIR_BLOCKED"):
        workflow.execute(proposal, user_confirmation="执行")

    assert not (project / "app.py").exists()


def test_feature_delivery_skill_waits_for_independent_review() -> None:
    skill = Path(__file__).parent.parent / "skills" / "catalog" / "feature-delivery" / "SKILL.md"
    result = validate_skill(skill)

    assert result.name == "feature-delivery"
    assert result.status == "draft"
    assert result.reviewed is False
    assert "metadata.devpilot.reviewed must be true before publication" in result.errors
