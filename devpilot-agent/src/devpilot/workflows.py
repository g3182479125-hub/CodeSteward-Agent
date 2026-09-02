"""Deterministic, approval-bound workflows built on the P2B control plane."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError

from .artifacts import ArtifactService, ArtifactWrite
from .control_plane import ControlPlaneManager
from .execution import FakeExecutionBackend
from .patches import FilePatch, PatchError
from .paths import PathBoundaryError
from .verification import VerificationProfile

_CONFIRMATIONS = frozenset({"执行", "确认执行", "confirmed"})
_STOP_CONDITIONS = (
    "stale_hash",
    "path_denied",
    "project_locked",
    "rollback_conflict",
    "verification_failed",
)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)
        return MappingProxyType({str(key): _freeze(item) for key, item in mapping.items()})
    if isinstance(value, list):
        items = cast(list[Any], value)
        return tuple(_freeze(item) for item in items)
    if isinstance(value, tuple):
        items = cast(tuple[Any, ...], value)
        return tuple(_freeze(item) for item in items)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return {str(key): _jsonable(item) for key, item in mapping.items()}
    if isinstance(value, tuple):
        items = cast(tuple[Any, ...], value)
        return [_jsonable(item) for item in items]
    return value


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
    evidence_hash: str
    preview: Mapping[str, Any]

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
            "evidence_hash": self.evidence_hash,
            "status": "awaiting_user_approval",
            "requires_user_confirmation": True,
            "preview": _jsonable(self.preview),
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
        if len(set(evidence)) != len(evidence):
            raise WorkflowError("WORKFLOW_EVIDENCE_INVALID", "evidence ids must be unique")
        required_event_types = (
            "evidence.inspect.completed",
            "evidence.search.completed",
            "evidence.read.completed",
        )
        evidence_facts: list[dict[str, Any]] = []
        for evidence_id in evidence:
            by_type = {
                str(item["event_type"]): cast(dict[str, Any], item["payload"])
                for item in store.list_run_events(evidence_id)
                if str(item["event_type"]) in required_event_types
            }
            if any(event_type not in by_type for event_type in required_event_types):
                raise WorkflowError(
                    "WORKFLOW_EVIDENCE_INVALID",
                    "evidence Run must contain server-generated inspect/search/read facts",
                )
            for payload in by_type.values():
                if payload.get("project_id") != project_id:
                    raise WorkflowError(
                        "WORKFLOW_EVIDENCE_INVALID",
                        "evidence facts must belong to the target project",
                    )
            read_fact = by_type["evidence.read.completed"]
            if not isinstance(read_fact.get("path"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", str(read_fact.get("sha256", ""))
            ):
                raise WorkflowError(
                    "WORKFLOW_EVIDENCE_INVALID",
                    "read evidence must contain a valid source hash",
                )
            evidence_facts.append(
                {
                    "run_id": evidence_id,
                    "events": [
                        {"event_type": event_type, "payload": by_type[event_type]}
                        for event_type in required_event_types
                    ],
                }
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
        run_id = self.manager.get_run_id(str(preview["change_set_id"]))
        evidence_hash = hashlib.sha256(
            json.dumps(
                evidence_facts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        check_ids = [profile.value]
        stop_conditions: list[str] = list(_STOP_CONDITIONS)
        contract_payload = {
            "plan_version": 1,
            "patch_hash": preview["patch_hash"],
            "scope_hash": preview["scope_hash"],
            "evidence_hash": evidence_hash,
            "check_ids": check_ids,
            "stop_conditions": stop_conditions,
            "risk_summary": "bounded text patch; apply remains separately authorized",
        }
        contract_hash = hashlib.sha256(
            json.dumps(
                contract_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        store.update_change_plan_contract(
            str(preview["plan_id"]),
            check_ids=check_ids,
            stop_conditions=stop_conditions,
            evidence_hash=evidence_hash,
            contract_hash=contract_hash,
        )
        # Keep the workflow-specific plan binding in the immutable caller-visible preview.
        # The patch engine remains the authority for patch_hash/scope_hash.
        preview.update(
            {
                "workflow": "feature-delivery",
                "evidence_ids": list(evidence),
                "evidence_hash": evidence_hash,
                "check_ids": check_ids,
                "stop_conditions": stop_conditions,
                "contract_hash": contract_hash,
            }
        )
        if run_id is not None:
            store.append_event(
                run_id,
                "feature_delivery.proposed",
                {
                    "workflow": "feature-delivery",
                    "change_set_id": preview["change_set_id"],
                    "patch_hash": preview["patch_hash"],
                    "scope_hash": preview["scope_hash"],
                    "evidence_ids": list(evidence),
                    "evidence_hash": evidence_hash,
                    "check_ids": check_ids,
                    "stop_conditions": stop_conditions,
                    "contract_hash": contract_hash,
                },
            )
        return FeatureDeliveryProposal(
            change_set_id=str(preview["change_set_id"]),
            project_id=str(preview["project_id"]),
            run_id=self.manager.get_run_id(str(preview["change_set_id"])),
            patch_hash=str(preview["patch_hash"]),
            scope_hash=str(preview["scope_hash"]),
            verification_profile=profile,
            evidence_ids=evidence,
            evidence_hash=evidence_hash,
            preview=cast(Mapping[str, Any], _freeze(preview)),
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
        if type(backend) is not FakeExecutionBackend:
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
        artifact: ArtifactWrite | None = None
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
        successful = verification.get("exit_code") == 0
        report = self._render_report(proposal, applied, verification) if successful else (
            self._render_failure_report(proposal, verification)
        )
        artifact_name = "feature-delivery.md" if successful else "feature-delivery-failure.md"
        artifact_kind = "delivery" if successful else "delivery-failure"
        try:
            artifact = ArtifactService(self.artifacts_root).write_markdown(
                project_id=proposal.project_id,
                run_id=proposal.run_id,
                name=artifact_name,
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
                kind=artifact_kind,
            )
        except (OSError, ValueError, SQLAlchemyError) as exc:
            # The successful file is never left behind when its DB index failed.
            # A failure event and failure report are the only durable fallback.
            if artifact is not None:
                try:
                    artifact.path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                fallback = ArtifactService(self.artifacts_root).write_markdown(
                    project_id=proposal.project_id,
                    run_id=proposal.run_id,
                    name="feature-delivery-failure.md",
                    content=self._render_failure_report(proposal, verification),
                )
                store.append_event(
                    proposal.run_id,
                    "artifact.persistence_failed",
                    {
                        "code": "ARTIFACT_WRITE_FAILED",
                        "path": fallback.path.relative_to(self.artifacts_root).as_posix(),
                        "sha256": fallback.sha256,
                        "success_artifact_removed": True,
                    },
                )
            except (OSError, ValueError):
                pass
            self.manager.mark_delivery_failed(proposal.change_set_id, "ARTIFACT_WRITE_FAILED")
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
            "completed" if successful else "failed",
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
            if isinstance(raw_item, Mapping):
                item = cast(Mapping[str, Any], raw_item)
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
                f"- Evidence hash: `{proposal.evidence_hash}`",
                f"- Check IDs: `{', '.join([proposal.verification_profile.value])}`",
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

    @staticmethod
    def _render_failure_report(
        proposal: FeatureDeliveryProposal, verification: dict[str, Any]
    ) -> str:
        return "\n".join(
            [
                "# Feature delivery failure report",
                "",
                f"- ChangeSet: `{proposal.change_set_id}`",
                f"- Run: `{proposal.run_id}`",
                f"- Patch hash: `{proposal.patch_hash}`",
                f"- Evidence hash: `{proposal.evidence_hash}`",
                "- Delivery status: `failed`",
                f"- Verification status: `{verification.get('status', 'unknown')}`",
                "- Failure handling: Artifact persistence or verification failed; "
                "no success is claimed.",
                "- stdout/stderr omitted by policy.",
                "",
            ]
        )
