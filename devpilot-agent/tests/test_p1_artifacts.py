from pathlib import Path

from devpilot.artifacts import ArtifactService
from devpilot.paths import AllowedRoots
from devpilot.services import ProjectAnalysisService
from devpilot.storage import ArtifactRecord, ProjectRecord, ProjectStore, RunEventRecord, RunRecord


def test_analysis_persists_run_events_and_artifact(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "python-project"
    store = ProjectStore(tmp_path / "devpilot.db")
    store.initialize()
    service = ProjectAnalysisService(
        allowed_roots=AllowedRoots.from_paths([fixture.parent]), store=store
    )

    outcome = service.inspect(fixture)
    write = service.publish_deterministic_overview(outcome=outcome, artifacts_root=tmp_path)

    assert write.path.exists()
    assert len(write.sha256) == 64
    assert store.count(ProjectRecord) == 1
    assert store.count(RunRecord) == 1
    assert store.count(RunEventRecord) == 3
    assert store.count(ArtifactRecord) == 1


def test_artifact_name_and_size_are_server_controlled(tmp_path: Path) -> None:
    service = ArtifactService(tmp_path)

    try:
        service.write_markdown(project_id="p", run_id="r", name="../escape.md", content="# x")
    except ValueError as exc:
        assert "project-overview.md" in str(exc)
    else:
        raise AssertionError("unsupported Artifact name was accepted")


def test_artifact_ids_cannot_escape_server_owned_path(tmp_path: Path) -> None:
    service = ArtifactService(tmp_path)

    try:
        service.write_markdown(
            project_id="../outside",
            run_id="00000000-0000-0000-0000-000000000001",
            name="project-overview.md",
            content="# x",
        )
    except ValueError as exc:
        assert "UUID" in str(exc)
    else:
        raise AssertionError("non-UUID Artifact id was accepted")
