"""Deterministic, approval-bound workflows built on the P2B control plane."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError

from .artifacts import ArtifactService, ArtifactWrite
from .control_plane import ControlPlaneManager
from .execution import FakeExecutionBackend
from .patches import FilePatch, PatchError
from .paths import PathBoundaryError
from .verification import VerificationProfile

_CONFIRMATIONS = frozenset({"执行", "确认执行", "confirmed"})


class WorkflowError(ValueError):
    """Stable error for a workflow that cannot safely progress."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class FeatureDeliveryProposal:
    """Immutable caller-facing handle for one exact ChangeSet preview."""

    change_set_id: str
    project_id: str
    run_id: str | None
    patch_hash: str
    scope_hash: str
    verification_profile: VerificationProfile
    evidence_ids: tuple[str, ...]
    preview: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": "feature-delivery",
            "change_set_id": self.change_set_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "patch_hash": self.patch_hash,
            "scope_hash": self.scope_hash,
            "verification_profile": self.verification_profile.value,
            "evidence_ids": list(self.evidence_ids),
            "status": "awaiting_user_approval",
            "requires_user_confirmation": True,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class FeatureDeliveryResult:
    """Bounded result of an explicitly approved feature delivery."""

    change_set_id: str
    status: str
    apply: dict[str, Any]
    verification: dict[str, Any]
    artifact: ArtifactWrite

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": "feature-delivery",
            "change_set_id": self.change_set_id,
            "status": self.status,
            "apply": self.apply,
            "verification": self.verification,
            "artifact": {
                "path": str(self.artifact.path),
                "sha256": self.artifact.sha256,
                "size_bytes": self.artifact.size_bytes,
            },
        }


class FeatureDeliveryWorkflow:
    """Compose inspect evidence, preview, approval, apply, verify and report.

    The workflow intentionally accepts an already prepared patch proposal rather
    than asking the model for commands. Before P6, only the deterministic Fake
    backend may drive this automated workflow; the existing explicit chat Tool
    remains responsible for the separately approved manual path.
    """

    def __init__(
        self,
        manager: ControlPlaneManager,
        *,
        artifacts_root: str | Path | None = None,
    ) -> None:
        self.manager = manager
        configured_root = manager.artifacts_root
        if configured_root is None:
            raise WorkflowError(
                "WORKFLOW_ARTIFACT_UNAVAILABLE",
                "feature delivery requires a server-configured Artifact root",
            )
        requested_root = (
            Path(artifacts_root).expanduser().resolve()
            if artifacts_root is not None
            else configured_root
        )
        if requested_root != configured_root:
            raise WorkflowError(
                "ARTIFACT_ROOT_INVALID",
                "workflow Artifact root must match the server-configured root",
            )
        self.artifacts_root = configured_root

    def propose(
        self,
        *,
        project_path: str | Path,
        patches: Iterable[FilePatch],
        evidence_ids: Iterable[str],
        verification_profile: VerificationProfile = VerificationProfile.PYTHON_SYNTAX,
    ) -> FeatureDeliveryProposal:
        evidence = tuple(item.strip() for item in evidence_ids if item.strip())
        if not evidence:
            raise WorkflowError(
                "WORKFLOW_EVIDENCE_REQUIRED",
                "feature delivery requires at least one inspection/search/read evidence id",
            )
        store = self.manager.store
        if store is None:
            raise WorkflowError(
                "WORKFLOW_STORE_REQUIRED", "feature delivery requires a durable ProjectStore"
            )
        try:
            project = self.manager.patch_service.allowed_roots.require_project(project_path)
        except PathBoundaryError as exc:
            raise WorkflowError("PATH_DENIED", "project is outside allowed roots") from exc
        if (
            project == self.artifacts_root
            or self.artifacts_root in project.parents
            or project in self.artifacts_root.parents
        ):
            raise WorkflowError(
                "ARTIFACT_ROOT_INVALID",
                "Artifact root must not overlap the target project",
            )
        project_id = store.ensure_project(str(project))
        try:
            evidence_matches = all(
                store.run_belongs_to_project(project_id=project_id, run_id=item)
                and store.get_run_kind(item) == "repo-analysis"
                for item in evidence
            )
        except KeyError:
            evidence_matches = False
        if not evidence_matches:
            raise WorkflowError(
                "WORKFLOW_EVIDENCE_INVALID",
                "evidence ids must reference runs belonging to the target project",
            )
        try:
            profile = VerificationProfile(verification_profile)
        except ValueError as exc:
            raise WorkflowError(
                "VERIFICATION_PROFILE_UNSUPPORTED", "verification profile is not allowlisted"
            ) from exc
        preview = self.manager.preview(
            project_path=project,
            project_id=None,
            patches=list(patches),
        )
        return FeatureDeliveryProposal(
            change_set_id=str(preview["change_set_id"]),
            project_id=str(preview["project_id"]),
            run_id=self.manager.get_run_id(str(preview["change_set_id"])),
            patch_hash=str(preview["patch_hash"]),
            scope_hash=str(preview["scope_hash"]),
            verification_profile=profile,
            evidence_ids=evidence,
            preview=preview,
        )

    def apply(
        self,
        proposal: FeatureDeliveryProposal,
        *,
        user_confirmation: str,
        approval_token: str,
    ) -> dict[str, Any]:
        if user_confirmation not in _CONFIRMATIONS:
            raise WorkflowError(
                "USER_CONFIRMATION_REQUIRED",
                "the exact feature-delivery proposal requires explicit user confirmation",
            )
        if not approval_token:
            raise WorkflowError("APPROVAL_TOKEN_REQUIRED", "an external Apply approval is required")
        return self.manager.apply(
            proposal.change_set_id,
            approval_token,
            idempotency_key=f"feature-delivery-apply-{proposal.change_set_id}",
        )

    def verify(
        self,
        proposal: FeatureDeliveryProposal,
        *,
        approval_token: str,
        apply_result: dict[str, Any] | None = None,
    ) -> FeatureDeliveryResult:
        backend = self.manager.execution_backend
        if not isinstance(backend, FakeExecutionBackend):
            self.manager.deny_execution(
                proposal.change_set_id, "EXECUTION_BACKEND_UNAVAILABLE"
            )
            raise WorkflowError(
                "AUTONOMOUS_REPAIR_BLOCKED",
                "real execution remains blocked until the P6 Sandbox Gate passes",
            )
        store = self.manager.store
        if not approval_token:
            raise WorkflowError(
                "APPROVAL_TOKEN_REQUIRED", "an external Verification approval is required"
            )
        if store is None or proposal.run_id is None:
            raise WorkflowError(
                "WORKFLOW_ARTIFACT_UNAVAILABLE",
                "feature delivery requires durable store and run-backed Artifact output",
            )
        try:
            verification = self.manager.verify(
                proposal.change_set_id,
                approval_token,
                profile=proposal.verification_profile,
                idempotency_key=f"feature-delivery-verify-{proposal.change_set_id}",
                finalize_run=False,
            )
        except PatchError as exc:
            self.manager.finalize_run(
                proposal.change_set_id,
                "failed",
                {"code": exc.code},
            )
            raise
        applied = apply_result or {
            "change_set_id": proposal.change_set_id,
            "status": "applied",
        }
        report = self._render_report(
            proposal,
            applied,
            verification,
        )
        try:
            artifact = ArtifactService(self.artifacts_root).write_markdown(
                project_id=proposal.project_id,
                run_id=proposal.run_id,
                name="feature-delivery.md",
                content=report,
            )
            artifact_id = str(uuid.uuid4())
            relative_path = artifact.path.relative_to(self.artifacts_root).as_posix()
            store.record_artifact(
                artifact_id=artifact_id,
                project_id=proposal.project_id,
                run_id=proposal.run_id,
                relative_path=relative_path,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
            )
        except (OSError, ValueError, SQLAlchemyError) as exc:
            self.manager.finalize_run(
                proposal.change_set_id,
                "failed",
                {"code": "ARTIFACT_WRITE_FAILED"},
            )
            raise WorkflowError(
                "WORKFLOW_ARTIFACT_WRITE_FAILED", "feature delivery Artifact could not be recorded"
            ) from exc
        self.manager.finalize_run(
            proposal.change_set_id,
            "completed" if verification.get("exit_code") == 0 else "failed",
            {
                "change_set_id": proposal.change_set_id,
                "status": verification.get("status", "unknown"),
                "artifact_id": artifact_id,
            },
        )
        return FeatureDeliveryResult(
            change_set_id=proposal.change_set_id,
            status=str(verification.get("status", "unknown")),
            apply=applied,
            verification=verification,
            artifact=artifact,
        )

    @staticmethod
    def _render_report(
        proposal: FeatureDeliveryProposal,
        applied: dict[str, Any],
        verification: dict[str, Any],
    ) -> str:
        files = cast(list[object], proposal.preview.get("files", []))
        file_lines: list[str] = []
        for raw_item in files:
            if isinstance(raw_item, dict):
                item = cast(dict[str, Any], raw_item)
                path = str(item.get("relative_path", "<unknown>"))
                operation = str(item.get("operation", "unknown"))
                file_lines.append(f"- `{path}` ({operation})")
        if not file_lines:
            file_lines.append("- 未提供文件元数据")
        # Do not copy stdout/stderr into the server-owned Artifact; they may
        # contain user data. The structured verification receipt remains the
        # source of truth for the run.
        return "\n".join(
            [
                "# Feature delivery report",
                "",
                "- Workflow: `feature-delivery`",
                f"- ChangeSet: `{proposal.change_set_id}`",
                f"- Project: `{proposal.project_id}`",
                f"- Patch hash: `{proposal.patch_hash}`",
                f"- Scope hash: `{proposal.scope_hash}`",
                f"- Apply status: `{applied.get('status', 'unknown')}`",
                f"- Verification status: `{verification.get('status', 'unknown')}`",
                "",
                "## Changed files",
                *file_lines,
                "",
                "## Verification",
                f"- Profile: `{verification.get('profile', proposal.verification_profile.value)}`",
                f"- Backend: `{verification.get('backend', 'unknown')}`",
                f"- Exit code: `{verification.get('exit_code', 'unknown')}`",
                f"- Duration ms: `{verification.get('duration_ms', 'unknown')}`",
                "",
                "## Evidence",
                *[f"- `{item}`" for item in proposal.evidence_ids],
                "",
                "## Safety boundary",
                "- Preview and Apply use the same immutable ChangeSet and patch hash.",
                "- Apply and verification require separate capabilities.",
                "- Verification uses a server allowlisted profile; no model command is accepted.",
                "- stdout/stderr are intentionally excluded from this Artifact.",
            ]
        ) + "\n"
