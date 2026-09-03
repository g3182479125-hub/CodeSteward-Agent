"""Deterministic, approval-bound workflows built on the P2B control plane."""

from __future__ import annotations

import hashlib
import json
import os
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
_NON_DELIVERY_VERIFY_CODES = frozenset(
    {
        "APPROVAL_INVALID",
        "APPROVAL_ALREADY_ISSUED",
        "CHANGESET_NOT_VERIFIABLE",
        "VERIFICATION_PROFILE_UNSUPPORTED",
        "IDEMPOTENCY_INVALID",
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_IN_PROGRESS",
    }
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


@dataclass(frozen=True, slots=True)
class BugFixProposal:
    """Immutable proposal bound to both inspection and saved failure evidence."""

    change_set_id: str
    project_id: str
    run_id: str | None
    patch_hash: str
    scope_hash: str
    verification_profile: VerificationProfile
    analysis_evidence_ids: tuple[str, ...]
    failure_evidence_ids: tuple[str, ...]
    failure_evidence_hash: str
    symptom: str
    expected_behavior: str
    non_goals: tuple[str, ...]
    allowed_files: tuple[str, ...]
    preview: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": "bug-fix",
            "change_set_id": self.change_set_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "patch_hash": self.patch_hash,
            "scope_hash": self.scope_hash,
            "verification_profile": self.verification_profile.value,
            "analysis_evidence_ids": list(self.analysis_evidence_ids),
            "failure_evidence_ids": list(self.failure_evidence_ids),
            "failure_evidence_hash": self.failure_evidence_hash,
            "symptom": self.symptom,
            "expected_behavior": self.expected_behavior,
            "non_goals": list(self.non_goals),
            "allowed_files": list(self.allowed_files),
            "status": "awaiting_user_approval",
            "requires_user_confirmation": True,
            "preview": _jsonable(self.preview),
        }


@dataclass(frozen=True, slots=True)
class BugFixResult:
    """Bounded result of an explicitly approved bug-fix delivery."""

    change_set_id: str
    status: str
    apply: dict[str, Any]
    verification: dict[str, Any]
    artifact: ArtifactWrite

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": "bug-fix",
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
            self.manager.mark_delivery_failed(
                proposal.change_set_id,
                "ARTIFACT_WRITE_FAILED",
            )
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


class BugFixWorkflow:
    """Turn a saved failure Run into a bounded, approval-gated ChangeSet.

    P4-02 deliberately stops at the same Fake-only verification boundary as
    feature delivery.  The workflow may prepare and apply a user-approved
    patch, but it cannot execute an arbitrary command or mint approval tokens.
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
                "bug fix requires a server-configured Artifact root",
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

    @classmethod
    def load(
        cls,
        manager: ControlPlaneManager,
        change_set_id: str,
        *,
        artifacts_root: str | Path | None = None,
    ) -> BugFixProposal:
        """Rebuild a proposal from the server-owned ChangeSet and Run event."""
        cls(manager, artifacts_root=artifacts_root)
        store = manager.store
        if store is None:
            raise WorkflowError(
                "WORKFLOW_STORE_REQUIRED", "bug fix requires a durable ProjectStore"
            )
        preview = manager.get(change_set_id)
        raw_contract = preview.get("bug_fix_contract")
        if not isinstance(raw_contract, Mapping):
            raise WorkflowError(
                "BUG_FIX_NOT_FOUND", "ChangeSet is not a persisted bug-fix proposal"
            )
        contract = cast(Mapping[str, Any], raw_contract)
        if contract.get("workflow") != "bug-fix":
            raise WorkflowError(
                "BUG_FIX_NOT_FOUND", "ChangeSet is not a persisted bug-fix proposal"
            )
        try:
            profile = VerificationProfile(str(contract["verification_profile"]))
            run_id = str(preview["run_id"]) if preview.get("run_id") is not None else None
            return BugFixProposal(
                change_set_id=str(preview["change_set_id"]),
                project_id=str(preview["project_id"]),
                run_id=run_id,
                patch_hash=str(preview["patch_hash"]),
                scope_hash=str(preview["scope_hash"]),
                verification_profile=profile,
                analysis_evidence_ids=tuple(
                    str(item) for item in cast(list[Any], contract["analysis_evidence_ids"])
                ),
                failure_evidence_ids=tuple(
                    str(item) for item in cast(list[Any], contract["failure_evidence_ids"])
                ),
                failure_evidence_hash=str(contract["failure_evidence_hash"]),
                symptom=str(contract["symptom"]),
                expected_behavior=str(contract["expected_behavior"]),
                non_goals=tuple(str(item) for item in cast(list[Any], contract["non_goals"])),
                allowed_files=tuple(
                    str(item) for item in cast(list[Any], contract["allowed_files"])
                ),
                preview=cast(Mapping[str, Any], _freeze(preview)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkflowError(
                "BUG_FIX_PERSISTENCE_INVALID", "bug-fix contract is incomplete"
            ) from exc

    def propose(
        self,
        *,
        project_path: str | Path,
        patches: Iterable[FilePatch],
        analysis_evidence_ids: Iterable[str],
        failure_evidence_ids: Iterable[str],
        symptom: str,
        expected_behavior: str,
        non_goals: Iterable[str],
        allowed_files: Iterable[str],
        verification_profile: VerificationProfile = VerificationProfile.REACT_STATIC,
    ) -> BugFixProposal:
        store = self.manager.store
        if store is None:
            raise WorkflowError(
                "WORKFLOW_STORE_REQUIRED", "bug fix requires a durable ProjectStore"
            )
        analysis_ids = self._identifiers(analysis_evidence_ids, "analysis evidence")
        failure_ids = self._identifiers(failure_evidence_ids, "failure evidence")
        if len(set(analysis_ids)) != len(analysis_ids) or len(set(failure_ids)) != len(
            failure_ids
        ):
            raise WorkflowError("BUG_FIX_INPUT_INVALID", "evidence ids must be unique")
        clean_symptom = self._text(symptom, "symptom")
        clean_expected = self._text(expected_behavior, "expected_behavior")
        clean_non_goals = self._texts(non_goals, "non_goals")
        clean_allowed = self._paths(allowed_files, "allowed_files")
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
                "ARTIFACT_ROOT_INVALID", "Artifact root must not overlap the target project"
            )
        project_id = store.ensure_project(str(project))
        analysis_facts = self._analysis_facts(store, project_id, analysis_ids)
        failure_facts, target_files, before_hashes = self._failure_facts(
            store,
            project_id,
            failure_ids,
            clean_symptom,
            clean_expected,
        )
        patch_values = tuple(patches)
        if not patch_values:
            raise WorkflowError("BUG_FIX_INPUT_INVALID", "at least one patch is required")
        patch_paths = tuple(self._path(item.relative_path, "patch path") for item in patch_values)
        if len(set(patch_paths)) != len(patch_paths):
            raise WorkflowError("BUG_FIX_SCOPE_INVALID", "patch paths must be unique")
        allowed_set = {self._path_key(item) for item in clean_allowed}
        target_set = {self._path_key(item) for item in target_files}
        for patch, relative_path in zip(patch_values, patch_paths, strict=True):
            path_key = self._path_key(relative_path)
            if path_key not in allowed_set or path_key not in target_set:
                raise WorkflowError(
                    "BUG_FIX_SCOPE_INVALID",
                    "every patch file must be listed in both failure evidence and allowed_files",
                )
            if patch.operation.value in {"update", "delete"}:
                expected_hash = patch.expected_sha256
                if expected_hash != before_hashes.get(path_key):
                    raise WorkflowError(
                        "BUG_FIX_FAILURE_EVIDENCE_INVALID",
                        "patch expected_sha256 must match the saved failure before hash",
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
            patches=list(patch_values),
        )
        run_id = self.manager.get_run_id(str(preview["change_set_id"]))
        analysis_hash = self._hash(analysis_facts)
        failure_hash = self._hash(failure_facts)
        evidence_hash = self._hash(
            {"analysis": analysis_facts, "failure": failure_facts}
        )
        check_ids = [profile.value]
        stop_conditions = [*_STOP_CONDITIONS, "failure_evidence_changed"]
        contract_payload = {
            "plan_version": 1,
            "workflow": "bug-fix",
            "patch_hash": preview["patch_hash"],
            "scope_hash": preview["scope_hash"],
            "analysis_evidence_hash": analysis_hash,
            "failure_evidence_hash": failure_hash,
            "evidence_hash": evidence_hash,
            "symptom": clean_symptom,
            "expected_behavior": clean_expected,
            "non_goals": list(clean_non_goals),
            "allowed_files": list(clean_allowed),
            "check_ids": check_ids,
            "stop_conditions": stop_conditions,
        }
        contract_hash = self._hash(contract_payload)
        store.update_change_plan_contract(
            str(preview["plan_id"]),
            check_ids=check_ids,
            stop_conditions=stop_conditions,
            evidence_hash=evidence_hash,
            contract_hash=contract_hash,
        )
        preview.update(
            {
                "workflow": "bug-fix",
                "analysis_evidence_ids": list(analysis_ids),
                "failure_evidence_ids": list(failure_ids),
                "analysis_evidence_hash": analysis_hash,
                "failure_evidence_hash": failure_hash,
                "evidence_hash": evidence_hash,
                "verification_profile": profile.value,
                "symptom": clean_symptom,
                "expected_behavior": clean_expected,
                "non_goals": list(clean_non_goals),
                "allowed_files": list(clean_allowed),
                "check_ids": check_ids,
                "stop_conditions": stop_conditions,
                "contract_hash": contract_hash,
            }
        )
        if run_id is not None:
            store.append_event(
                run_id,
                "bug_fix.proposed",
                {
                    "workflow": "bug-fix",
                    "change_set_id": preview["change_set_id"],
                    "patch_hash": preview["patch_hash"],
                    "scope_hash": preview["scope_hash"],
                    "analysis_evidence_ids": list(analysis_ids),
                    "failure_evidence_ids": list(failure_ids),
                    "analysis_evidence_hash": analysis_hash,
                    "failure_evidence_hash": failure_hash,
                    "symptom": clean_symptom,
                    "expected_behavior": clean_expected,
                    "non_goals": list(clean_non_goals),
                    "allowed_files": list(clean_allowed),
                    "verification_profile": profile.value,
                    "contract_hash": contract_hash,
                },
            )
        return BugFixProposal(
            change_set_id=str(preview["change_set_id"]),
            project_id=str(preview["project_id"]),
            run_id=run_id,
            patch_hash=str(preview["patch_hash"]),
            scope_hash=str(preview["scope_hash"]),
            verification_profile=profile,
            analysis_evidence_ids=analysis_ids,
            failure_evidence_ids=failure_ids,
            failure_evidence_hash=failure_hash,
            symptom=clean_symptom,
            expected_behavior=clean_expected,
            non_goals=clean_non_goals,
            allowed_files=clean_allowed,
            preview=cast(Mapping[str, Any], _freeze(preview)),
        )

    def apply(
        self,
        proposal: BugFixProposal,
        *,
        user_confirmation: str,
        approval_token: str,
    ) -> dict[str, Any]:
        if user_confirmation not in _CONFIRMATIONS:
            raise WorkflowError(
                "USER_CONFIRMATION_REQUIRED",
                "the exact bug-fix proposal requires explicit user confirmation",
            )
        if not approval_token:
            raise WorkflowError("APPROVAL_TOKEN_REQUIRED", "an external Apply approval is required")
        return self.manager.apply(
            proposal.change_set_id,
            approval_token,
            idempotency_key=f"bug-fix-apply-{proposal.change_set_id}",
        )

    def verify(
        self,
        proposal: BugFixProposal,
        *,
        approval_token: str,
        apply_result: dict[str, Any] | None = None,
    ) -> BugFixResult:
        if not approval_token:
            raise WorkflowError(
                "APPROVAL_TOKEN_REQUIRED", "an external Verification approval is required"
            )
        store = self.manager.store
        if store is None or proposal.run_id is None:
            raise WorkflowError(
                "WORKFLOW_ARTIFACT_UNAVAILABLE",
                "bug fix requires durable store and run-backed Artifact output",
            )
        if type(self.manager.execution_backend) is not FakeExecutionBackend:
            self.manager.deny_execution(proposal.change_set_id, "EXECUTION_BACKEND_UNAVAILABLE")
            failure_artifact = self._record_failure_artifact(
                proposal,
                {
                    "status": "execution_blocked",
                    "profile": proposal.verification_profile.value,
                    "backend": "unavailable",
                    "exit_code": None,
                    "error_code": "EXECUTION_BACKEND_UNAVAILABLE",
                },
                store,
            )
            try:
                store.append_event(
                    proposal.run_id,
                    "bug_fix.failed",
                    {
                        "code": "EXECUTION_BACKEND_UNAVAILABLE",
                        "workflow": "bug-fix",
                        "artifact": failure_artifact,
                        "verification_profile": proposal.verification_profile.value,
                        "backend": "unavailable",
                        **self._bound_hashes(proposal),
                    },
                )
            except (OSError, ValueError, SQLAlchemyError):
                pass
            raise WorkflowError(
                "AUTONOMOUS_REPAIR_BLOCKED",
                "real execution remains blocked until the P6 Sandbox Gate passes",
            )
        try:
            verification = self.manager.verify(
                proposal.change_set_id,
                approval_token,
                profile=proposal.verification_profile,
                idempotency_key=f"bug-fix-verify-{proposal.change_set_id}",
                finalize_run=False,
            )
        except PatchError as exc:
            if exc.code in _NON_DELIVERY_VERIFY_CODES:
                # These are caller/state/configuration errors, not a failed
                # verification run.  Do not create a misleading failure
                # Artifact or close the workflow-owned Run.
                raise
            failure = {
                "status": "verification_failed",
                "profile": proposal.verification_profile.value,
                "backend": "not-run",
                "exit_code": None,
                "error_code": exc.code,
            }
            failure_artifact = self._record_failure_artifact(proposal, failure, store)
            try:
                store.append_event(
                    proposal.run_id,
                    "bug_fix.failed",
                    {
                        "code": exc.code,
                        "workflow": "bug-fix",
                        "artifact": failure_artifact,
                        "verification_profile": proposal.verification_profile.value,
                        "backend": "not-run",
                        **self._bound_hashes(proposal),
                    },
                )
            except (OSError, ValueError, SQLAlchemyError):
                pass
            self.manager.finalize_run(proposal.change_set_id, "failed", {"code": exc.code})
            raise
        applied = apply_result or {
            "change_set_id": proposal.change_set_id,
            "status": "applied",
        }
        successful = verification.get("exit_code") == 0
        artifact_name = "bug-fix.md" if successful else "bug-fix-failure.md"
        artifact_kind = "bug-fix" if successful else "bug-fix-failure"
        artifact: ArtifactWrite | None = None
        artifact_id = str(uuid.uuid4())
        try:
            report = self._render_report(
                proposal,
                applied,
                verification,
                successful,
                artifact_id=artifact_id,
            )
            artifact = ArtifactService(self.artifacts_root).write_markdown(
                project_id=proposal.project_id,
                run_id=proposal.run_id,
                name=artifact_name,
                content=report,
            )
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
            if artifact is not None:
                try:
                    artifact.path.unlink(missing_ok=True)
                except OSError:
                    pass
            failure_artifact = self._record_failure_artifact(
                proposal,
                {
                    "status": "artifact_persistence_failed",
                    "profile": proposal.verification_profile.value,
                    "backend": verification.get("backend", "unknown"),
                    "exit_code": verification.get("exit_code"),
                    "error_code": "ARTIFACT_WRITE_FAILED",
                },
                store,
            )
            self.manager.mark_delivery_failed(
                proposal.change_set_id,
                "ARTIFACT_WRITE_FAILED",
                workflow="bug-fix",
                artifact_id=str(failure_artifact["artifact_id"]),
                event_payload={
                    "workflow": "bug-fix",
                    "artifact": failure_artifact,
                    "verification_profile": proposal.verification_profile.value,
                    "backend": verification.get("backend", "unknown"),
                    "exit_code": verification.get("exit_code"),
                    **self._bound_hashes(proposal),
                },
            )
            self.manager.finalize_run(
                proposal.change_set_id, "failed", {"code": "ARTIFACT_WRITE_FAILED"}
            )
            raise WorkflowError(
                "WORKFLOW_ARTIFACT_WRITE_FAILED", "bug-fix Artifact could not be recorded"
            ) from exc
        assert artifact is not None
        artifact_info = {
            "artifact_id": artifact_id,
            "path": artifact.path.relative_to(self.artifacts_root).as_posix(),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "indexed": True,
        }
        store.append_event(
            proposal.run_id,
            "bug_fix.completed" if successful else "bug_fix.failed",
            {
                "workflow": "bug-fix",
                "change_set_id": proposal.change_set_id,
                "status": verification.get("status", "unknown"),
                "exit_code": verification.get("exit_code"),
                "artifact_id": artifact_id,
                "artifact": artifact_info,
                "verification_profile": proposal.verification_profile.value,
                "backend": verification.get("backend", "unknown"),
                **self._bound_hashes(proposal),
            },
        )
        self.manager.finalize_run(
            proposal.change_set_id,
            "completed" if successful else "failed",
            {
                "change_set_id": proposal.change_set_id,
                "status": verification.get("status", "unknown"),
                "artifact_id": artifact_id,
            },
        )
        return BugFixResult(
            change_set_id=proposal.change_set_id,
            status=str(verification.get("status", "unknown")),
            apply=applied,
            verification=verification,
            artifact=artifact,
        )

    @staticmethod
    def _identifiers(values: Iterable[str], label: str) -> tuple[str, ...]:
        result = tuple(str(item).strip() for item in values if str(item).strip())
        if not result:
            raise WorkflowError("BUG_FIX_INPUT_INVALID", f"at least one {label} is required")
        if any(len(item) > 128 for item in result):
            raise WorkflowError("BUG_FIX_INPUT_INVALID", f"{label} id is too long")
        return result

    @staticmethod
    def _text(value: str, field: str) -> str:
        result = str(value).strip()
        if not result or len(result) > 4000:
            raise WorkflowError("BUG_FIX_INPUT_INVALID", f"{field} must be 1–4000 characters")
        return result

    @classmethod
    def _texts(cls, values: Iterable[str], field: str) -> tuple[str, ...]:
        result = tuple(cls._text(str(item), field) for item in values)
        if len(result) > 32 or len(set(result)) != len(result):
            raise WorkflowError("BUG_FIX_INPUT_INVALID", f"{field} must contain unique values")
        return result

    @staticmethod
    def _path(value: str, field: str) -> str:
        result = str(value).replace("\\", "/").strip()
        parts = tuple(item for item in result.split("/") if item)
        if (
            not result
            or result.startswith("/")
            or any(item in {".", ".."} or ":" in item for item in parts)
            or not parts
        ):
            raise WorkflowError("BUG_FIX_SCOPE_INVALID", f"{field} must be a safe relative path")
        return "/".join(parts)

    @staticmethod
    def _path_key(value: str) -> str:
        return value.casefold() if os.name == "nt" else value

    @classmethod
    def _paths(cls, values: Iterable[str], field: str) -> tuple[str, ...]:
        result = tuple(cls._path(str(item), field) for item in values)
        keys = tuple(cls._path_key(item) for item in result)
        if not result or len(result) > 32 or len(set(keys)) != len(result):
            raise WorkflowError("BUG_FIX_SCOPE_INVALID", f"{field} must contain unique paths")
        return result

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    @classmethod
    def _analysis_facts(
        cls, store: Any, project_id: str, evidence_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        try:
            if not all(
                store.run_belongs_to_project(project_id=project_id, run_id=item)
                and store.get_run_kind(item) == "repo-analysis"
                for item in evidence_ids
            ):
                raise WorkflowError(
                    "WORKFLOW_EVIDENCE_INVALID",
                    "analysis evidence must belong to the target repo-analysis project",
                )
        except KeyError as exc:
            raise WorkflowError(
                "WORKFLOW_EVIDENCE_INVALID", "analysis evidence Run does not exist"
            ) from exc
        required = (
            "evidence.inspect.completed",
            "evidence.search.completed",
            "evidence.read.completed",
        )
        facts: list[dict[str, Any]] = []
        for evidence_id in evidence_ids:
            by_type = {
                str(item["event_type"]): cast(dict[str, Any], item["payload"])
                for item in store.list_run_events(evidence_id)
                if str(item["event_type"]) in required
            }
            if any(item not in by_type for item in required):
                raise WorkflowError(
                    "WORKFLOW_EVIDENCE_INVALID",
                    "analysis evidence must contain inspect/search/read facts",
                )
            for payload in by_type.values():
                if payload.get("project_id") != project_id:
                    raise WorkflowError(
                        "WORKFLOW_EVIDENCE_INVALID", "analysis evidence project binding is invalid"
                    )
            read_fact = by_type["evidence.read.completed"]
            if not isinstance(read_fact.get("path"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", str(read_fact.get("sha256", ""))
            ):
                raise WorkflowError(
                    "WORKFLOW_EVIDENCE_INVALID", "read evidence must contain a valid source hash"
                )
            facts.append(
                {
                    "run_id": evidence_id,
                    "events": [
                        {"event_type": item, "payload": by_type[item]} for item in required
                    ],
                }
            )
        return facts

    @classmethod
    def _failure_facts(
        cls,
        store: Any,
        project_id: str,
        evidence_ids: tuple[str, ...],
        symptom: str,
        expected_behavior: str,
    ) -> tuple[list[dict[str, Any]], tuple[str, ...], dict[str, str]]:
        facts: list[dict[str, Any]] = []
        target_files: set[str] = set()
        before_hashes: dict[str, str] = {}
        for evidence_id in evidence_ids:
            try:
                valid_run = store.run_belongs_to_project(
                    project_id=project_id, run_id=evidence_id
                ) and store.get_run_kind(evidence_id) == "bug-check"
                status = store.get_run_status(evidence_id)
            except KeyError as exc:
                raise WorkflowError(
                    "BUG_FIX_FAILURE_EVIDENCE_INVALID", "failure evidence Run does not exist"
                ) from exc
            if not valid_run or status != "failed":
                raise WorkflowError(
                    "BUG_FIX_FAILURE_EVIDENCE_INVALID",
                    "failure evidence must be a failed bug-check Run for the target project",
                )
            failures = [
                item
                for item in store.list_run_events(evidence_id)
                if item["event_type"] == "bug_check.failed"
            ]
            if len(failures) != 1 or not isinstance(failures[0].get("payload"), dict):
                raise WorkflowError(
                    "BUG_FIX_FAILURE_EVIDENCE_INVALID",
                    "failure evidence must contain exactly one bug_check.failed fact",
                )
            payload = cast(dict[str, Any], failures[0]["payload"])
            if (
                payload.get("producer") != "devpilot.bug-check"
                or payload.get("schema_version") != 1
                or payload.get("project_id") != project_id
                or payload.get("status") != "failed"
            ):
                raise WorkflowError(
                    "BUG_FIX_FAILURE_EVIDENCE_INVALID",
                    "failure fact provenance, project or status binding is invalid",
                )
            if (
                payload.get("symptom") != symptom
                or payload.get("expected_behavior") != expected_behavior
            ):
                raise WorkflowError(
                    "BUG_FIX_FAILURE_EVIDENCE_INVALID",
                    "proposal symptom and expected behavior must match saved failure evidence",
                )
            raw_targets = payload.get("target_files")
            raw_hashes = payload.get("before_hashes")
            if not isinstance(raw_targets, list) or not isinstance(raw_hashes, dict):
                raise WorkflowError(
                    "BUG_FIX_FAILURE_EVIDENCE_INVALID",
                    "failure fact must contain target_files and before_hashes",
                )
            target_values = cast(list[Any], raw_targets)
            hash_values = cast(dict[Any, Any], raw_hashes)
            normalized_targets = tuple(
                cls._path_key(cls._path(str(item), "target file")) for item in target_values
            )
            if not normalized_targets or len(set(normalized_targets)) != len(normalized_targets):
                raise WorkflowError(
                    "BUG_FIX_FAILURE_EVIDENCE_INVALID", "failure target files must be unique"
                )
            normalized_hashes: dict[str, str] = {}
            for raw_path, raw_hash in hash_values.items():
                normalized_path = cls._path_key(cls._path(str(raw_path), "before hash path"))
                if not re.fullmatch(r"[0-9a-f]{64}", str(raw_hash)):
                    raise WorkflowError(
                        "BUG_FIX_FAILURE_EVIDENCE_INVALID", "failure before hashes are invalid"
                    )
                normalized_hashes[normalized_path] = str(raw_hash)
            if any(item not in normalized_hashes for item in normalized_targets):
                raise WorkflowError(
                    "BUG_FIX_FAILURE_EVIDENCE_INVALID",
                    "every failure target file must have a before hash",
                )
            for path, value in normalized_hashes.items():
                previous = before_hashes.get(path)
                if previous is not None and previous != value:
                    raise WorkflowError(
                        "BUG_FIX_FAILURE_EVIDENCE_INVALID",
                        "failure evidence contains conflicting before hashes",
                    )
            target_files.update(normalized_targets)
            before_hashes.update(normalized_hashes)
            facts.append(
                {
                    "run_id": evidence_id,
                    "event": {"event_type": "bug_check.failed", "payload": payload},
                }
            )
        return facts, tuple(sorted(target_files)), before_hashes

    @staticmethod
    def _bound_hashes(proposal: BugFixProposal) -> dict[str, Any]:
        before_manifest = [str(item) for item in proposal.preview.get("before_manifest", [])]
        after_manifest = [str(item) for item in proposal.preview.get("after_manifest", [])]
        return {
            "before_manifest": before_manifest,
            "after_manifest": after_manifest,
            "before_manifest_hash": BugFixWorkflow._hash(before_manifest),
            "after_manifest_hash": BugFixWorkflow._hash(after_manifest),
        }

    @staticmethod
    def _render_report(
        proposal: BugFixProposal,
        applied: dict[str, Any],
        verification: dict[str, Any],
        successful: bool,
        *,
        artifact_id: str,
    ) -> str:
        files = cast(list[object], proposal.preview.get("files", []))
        file_lines: list[str] = []
        for raw_item in files:
            if isinstance(raw_item, Mapping):
                item = cast(Mapping[str, Any], raw_item)
                file_lines.append(
                    f"- `{item.get('relative_path', '<unknown>')}` "
                    f"({item.get('operation', 'unknown')}); "
                    f"before=`{item.get('expected_sha256') or '<none>'}`; "
                    f"after=`{item.get('after_sha256') or '<none>'}`"
                )
        if not file_lines:
            file_lines.append("- 未提供文件元数据")
        before_manifest = "; ".join(
            str(item) for item in proposal.preview.get("before_manifest", [])
        )
        after_manifest = "; ".join(
            str(item) for item in proposal.preview.get("after_manifest", [])
        )
        return "\n".join(
            [
                "# Bug-fix delivery report",
                "",
                "- Workflow: `bug-fix`",
                f"- ChangeSet: `{proposal.change_set_id}`",
                f"- Project: `{proposal.project_id}`",
                f"- Patch hash: `{proposal.patch_hash}`",
                f"- Scope hash: `{proposal.scope_hash}`",
                f"- Failure evidence hash: `{proposal.failure_evidence_hash}`",
                f"- Artifact ID: `{artifact_id}`",
                f"- Symptom: {proposal.symptom}",
                f"- Expected behavior: {proposal.expected_behavior}",
                f"- Apply status: `{applied.get('status', 'unknown')}`",
                f"- Verification status: `{verification.get('status', 'unknown')}`",
                f"- Delivery result: `{'success' if successful else 'failed'}`",
                "- Verification profile: "
                f"`{verification.get('profile', proposal.verification_profile.value)}`",
                f"- Verification backend: `{verification.get('backend', 'unknown')}`",
                f"- Exit code: `{verification.get('exit_code', 'unknown')}`",
                "",
                "## Changed files",
                *file_lines,
                "",
                f"- Before manifest: `{before_manifest}`",
                f"- After manifest: `{after_manifest}`",
                "",
                "## Evidence",
                *[f"- Analysis: `{item}`" for item in proposal.analysis_evidence_ids],
                *[f"- Failure: `{item}`" for item in proposal.failure_evidence_ids],
                "",
                "## Safety boundary",
                "- Failure evidence, target files, before hashes and allowed_files are "
                "bound into the contract.",
                "- Apply and verification require separate control-plane capabilities.",
                "- Verification uses the server allowlisted React static contract; no model "
                "command is accepted.",
                "- P4-02 does not execute real React/npm tests; real execution remains a P6 gate.",
                "- stdout/stderr are intentionally excluded from this Artifact.",
                "",
            ]
        )

    def _record_failure_artifact(
        self,
        proposal: BugFixProposal,
        verification: dict[str, Any],
        store: Any,
    ) -> dict[str, Any]:
        """Best-effort failure Artifact plus a durable fallback event."""
        artifact_id = str(uuid.uuid4())
        artifact: ArtifactWrite | None = None
        result: dict[str, Any] = {
            "artifact_id": artifact_id,
            "path": None,
            "sha256": None,
            "size_bytes": None,
            "indexed": False,
            "verification_profile": verification.get("profile"),
            "backend": verification.get("backend"),
            "exit_code": verification.get("exit_code"),
            "before_manifest_hash": self._hash(
                [str(item) for item in proposal.preview.get("before_manifest", [])]
            ),
            "after_manifest_hash": self._hash(
                [str(item) for item in proposal.preview.get("after_manifest", [])]
            ),
        }
        try:
            artifact = ArtifactService(self.artifacts_root).write_markdown(
                project_id=proposal.project_id,
                run_id=proposal.run_id or "",
                name="bug-fix-failure.md",
                content=self._render_report(
                    proposal,
                    {"status": "not-applied"},
                    verification,
                    False,
                    artifact_id=artifact_id,
                ),
            )
            relative_path = artifact.path.relative_to(self.artifacts_root).as_posix()
            store.record_artifact(
                artifact_id=artifact_id,
                project_id=proposal.project_id,
                run_id=proposal.run_id,
                relative_path=relative_path,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                kind="bug-fix-failure",
            )
            result.update(
                {
                    "path": relative_path,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "indexed": True,
                }
            )
        except (OSError, ValueError, SQLAlchemyError):
            try:
                fallback: dict[str, Any] = {
                    "code": "ARTIFACT_WRITE_FAILED",
                    "workflow": "bug-fix",
                    "artifact_id": artifact_id,
                    "path": None,
                    "sha256": None,
                    "size_bytes": None,
                    "indexed": False,
                }
                if artifact is not None:
                    fallback.update(
                        {
                            "artifact_id": artifact_id,
                            "path": artifact.path.relative_to(self.artifacts_root).as_posix(),
                            "sha256": artifact.sha256,
                            "size_bytes": artifact.size_bytes,
                            "indexed": False,
                        }
                    )
                    result.update(
                        {
                            "path": fallback["path"],
                            "sha256": fallback["sha256"],
                            "size_bytes": fallback["size_bytes"],
                        }
                    )
                fallback.update(
                    {
                        "verification_profile": verification.get("profile"),
                        "backend": verification.get("backend"),
                        "exit_code": verification.get("exit_code"),
                        **self._bound_hashes(proposal),
                    }
                )
                store.append_event(
                    proposal.run_id,
                    "artifact.persistence_failed",
                    fallback,
                )
            except (OSError, ValueError, SQLAlchemyError):
                pass
        return result
