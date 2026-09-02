from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from devpilot.code_access import CodeAccessService
from devpilot.control_plane import ControlPlaneManager
from devpilot.domain import FileOperation
from devpilot.execution import ExecutionRequest, ExecutionResult, FakeExecutionBackend
from devpilot.patches import FilePatch
from devpilot.paths import AllowedRoots
from devpilot.services import ProjectAnalysisService
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
        artifacts_root=tmp_path / "data",
    )
    return store, project, manager


_BASELINE = (
    "from fastapi import FastAPI\n\n"
    "app = FastAPI()\n\n"
    '@app.get("/health")\n'
    "def health() -> dict[str, str]:\n"
    '    return {"status": "old"}\n'
)
_UPDATED = _BASELINE.replace('"old"', '"ok"')


def _patch(project: Path) -> FilePatch:
    return FilePatch(
        "app.py",
        FileOperation.UPDATE,
        _UPDATED,
        hashlib.sha256(_BASELINE.encode()).hexdigest(),
    )


def _evidence(project: Path, store: ProjectStore) -> str:
    (project / "app.py").write_text(_BASELINE, encoding="utf-8", newline="")
    roots = AllowedRoots.from_paths([project.parent])
    outcome = ProjectAnalysisService(allowed_roots=roots, store=store).inspect(project)
    access = CodeAccessService(allowed_roots=roots, store=store)
    assert access.search(outcome.project_id, "FastAPI").matches
    assert access.read(outcome.project_id, "app.py").content
    return outcome.run_id


def test_feature_delivery_g01_completes_approved_fake_path_and_artifact(
    tmp_path: Path,
) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend(stdout="fake-ok"))
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, store)

    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )

    assert proposal.to_dict()["requires_user_confirmation"] is True
    assert (project / "app.py").read_text(encoding="utf-8") == _BASELINE

    apply_approval = manager.approve(proposal.change_set_id)
    applied = workflow.apply(
        proposal,
        user_confirmation="执行",
        approval_token=str(apply_approval["approval_token"]),
    )
    verify_approval = manager.approve(proposal.change_set_id, "r2.verify")
    result = workflow.verify(
        proposal,
        approval_token=str(verify_approval["approval_token"]),
        apply_result=applied,
    )

    assert result.status == "verified"
    assert result.apply["status"] == "applied"
    assert result.verification["backend"] == "fake"
    assert result.verification["exit_code"] == 0
    assert 'return {"status": "ok"}' in (project / "app.py").read_text(encoding="utf-8")
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
    assert proposal.run_id is not None
    assert store.get_run_status(proposal.run_id) == "completed"


def test_feature_delivery_run_link_survives_manager_restart(tmp_path: Path) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )

    recovered = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=FakeExecutionBackend(),
        artifacts_root=tmp_path / "data",
    )

    assert recovered.get_run_id(proposal.change_set_id) == proposal.run_id
    assert proposal.run_id is not None
    assert store.get_run_status(proposal.run_id) == "running"


def test_feature_delivery_records_failed_verification_and_artifact(tmp_path: Path) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend(exit_code=1))
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )
    apply_approval = manager.approve(proposal.change_set_id)
    applied = workflow.apply(
        proposal,
        user_confirmation="执行",
        approval_token=str(apply_approval["approval_token"]),
    )
    verify_approval = manager.approve(proposal.change_set_id, "r2.verify")

    result = workflow.verify(
        proposal,
        approval_token=str(verify_approval["approval_token"]),
        apply_result=applied,
    )

    assert result.status == "verification_failed"
    assert result.verification["exit_code"] == 1
    assert result.artifact.path.exists()
    assert proposal.run_id is not None
    assert store.get_run_status(proposal.run_id) == "failed"


def test_feature_delivery_never_applies_without_exact_user_confirmation(tmp_path: Path) -> None:
    _store, project, manager = _manager(tmp_path, FakeExecutionBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, _store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )

    with pytest.raises(WorkflowError, match="USER_CONFIRMATION_REQUIRED"):
        workflow.apply(
            proposal,
            user_confirmation="现在执行",
            approval_token="not-consumed",
        )

    assert (project / "app.py").read_text(encoding="utf-8") == _BASELINE


class _UnapprovedRemoteBackend:
    name = "fc-agent-sandbox"

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        raise AssertionError("P4 must not start real execution before the P6 Gate")


def test_feature_delivery_blocks_real_backend_before_p6(tmp_path: Path) -> None:
    _store, project, manager = _manager(tmp_path, _UnapprovedRemoteBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, _store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )

    apply_approval = manager.approve(proposal.change_set_id)
    applied = workflow.apply(
        proposal,
        user_confirmation="执行",
        approval_token=str(apply_approval["approval_token"]),
    )
    verify_approval = manager.approve(proposal.change_set_id, "r2.verify")
    with pytest.raises(WorkflowError, match="AUTONOMOUS_REPAIR_BLOCKED"):
        workflow.verify(
            proposal,
            approval_token=str(verify_approval["approval_token"]),
            apply_result=applied,
        )

    assert (project / "app.py").read_text(encoding="utf-8") == _UPDATED


def test_feature_delivery_skill_waits_for_independent_review() -> None:
    skill = Path(__file__).parent.parent / "skills" / "catalog" / "feature-delivery" / "SKILL.md"
    result = validate_skill(skill)

    assert result.name == "feature-delivery"
    assert result.status == "draft"
    assert result.reviewed is False
    assert "metadata.devpilot.reviewed must be true before publication" in result.errors
