"""P1 application services shared by CLI, Tools and future FastAPI adapters."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactService, ArtifactWrite
from .inspector import InspectionResult, inspect_project
from .paths import AllowedRoots
from .report import render_project_overview
from .storage import BugCheckFailureFact, ProjectStore


@dataclass(frozen=True)
class AnalysisOutcome:
    project_id: str
    run_id: str
    inspection: InspectionResult


@dataclass(frozen=True)
class BugCheckOutcome:
    project_id: str
    run_id: str
    before_hashes: dict[str, str]


class ProjectAnalysisService:
    def __init__(self, *, allowed_roots: AllowedRoots, store: ProjectStore) -> None:
        self.allowed_roots = allowed_roots
        self.store = store

    def inspect(self, project_path: str | Path) -> AnalysisOutcome:
        inspection = inspect_project(project_path, self.allowed_roots)
        root_id, fingerprint = self.allowed_roots.binding_for(inspection.project_path)
        project_id = self.store.ensure_project(
            inspection.project_path,
            allowed_root_id=root_id,
            repo_fingerprint=fingerprint,
        )
        run_id = self.store.start_run(project_id)
        self.store.append_event(
            run_id,
            "tool.completed",
            {"tool": "devpilot_inspect_project", "files_seen": inspection.files_seen},
        )
        self.store.append_event(
            run_id,
            "evidence.inspect.completed",
            {
                "project_id": project_id,
                "technology_stacks": inspection.technology_stacks,
                "files_seen": inspection.files_seen,
                "metadata_bytes": inspection.metadata_bytes,
                "truncated": inspection.truncated,
            },
        )
        return AnalysisOutcome(project_id=project_id, run_id=run_id, inspection=inspection)

    def publish_overview(
        self, *, project_id: str, run_id: str, content: str, artifacts_root: str | Path
    ) -> ArtifactWrite:
        self.store.validate_open_run(project_id=project_id, run_id=run_id)
        artifact_service = ArtifactService(artifacts_root)
        write = artifact_service.write_markdown(
            project_id=project_id,
            run_id=run_id,
            name="project-overview.md",
            content=content,
        )
        artifact_id = str(uuid.uuid4())
        relative_path = write.path.relative_to(
            Path(artifacts_root).expanduser().resolve()
        ).as_posix()
        self.store.record_artifact(
            artifact_id=artifact_id,
            project_id=project_id,
            run_id=run_id,
            relative_path=relative_path,
            sha256=write.sha256,
            size_bytes=write.size_bytes,
        )
        self.store.finish_run(
            run_id,
            "completed",
            {"artifact_id": artifact_id, "sha256": write.sha256},
        )
        return write

    def publish_deterministic_overview(
        self, *, outcome: AnalysisOutcome, artifacts_root: str | Path
    ) -> ArtifactWrite:
        return self.publish_overview(
            project_id=outcome.project_id,
            run_id=outcome.run_id,
            content=render_project_overview(outcome.inspection),
            artifacts_root=artifacts_root,
        )


class BugCheckService:
    """Create failed-check evidence from server-read project files."""

    def __init__(self, *, allowed_roots: AllowedRoots, store: ProjectStore) -> None:
        self.allowed_roots = allowed_roots
        self.store = store

    def record_failure(
        self,
        project_path: str | Path,
        *,
        symptom: str,
        expected_behavior: str,
        target_files: list[str] | tuple[str, ...],
        code: str,
    ) -> BugCheckOutcome:
        """Read and hash targets before creating the durable bug-check Run.

        Callers provide the observed symptom and target names only.  The
        pre-change hashes are computed here from the allowed, real project.
        """
        project = self.allowed_roots.require_project(project_path)
        if not target_files:
            raise ValueError("bug-check target files must not be empty")
        canonical_targets = [self.store.canonical_failure_path(item) for item in target_files]
        if len(set(canonical_targets)) != len(canonical_targets):
            raise ValueError("bug-check target files must be unique")
        before_hashes: dict[str, str] = {}
        for relative_path in canonical_targets:
            target = (project / relative_path).resolve(strict=True)
            try:
                target.relative_to(project)
            except ValueError as exc:
                raise ValueError("bug-check target path escapes the project") from exc
            if target.is_symlink() or not target.is_file():
                raise ValueError("bug-check targets must be regular files")
            before_hashes[relative_path] = hashlib.sha256(target.read_bytes()).hexdigest()

        root_id, fingerprint = self.allowed_roots.binding_for(project)
        project_id = self.store.ensure_project(
            str(project), allowed_root_id=root_id, repo_fingerprint=fingerprint
        )
        run_id = self.store.start_run(project_id, kind="bug-check")
        self.store.record_bug_check_failure_fact(
            BugCheckFailureFact(
                run_id=run_id,
                project_id=project_id,
                symptom=symptom,
                expected_behavior=expected_behavior,
                target_files=tuple(canonical_targets),
                before_hashes=before_hashes,
                code=code,
            )
        )
        return BugCheckOutcome(
            project_id=project_id, run_id=run_id, before_hashes=dict(before_hashes)
        )
