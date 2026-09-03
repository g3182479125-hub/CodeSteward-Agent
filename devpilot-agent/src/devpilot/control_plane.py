"""Durable localhost control plane for the explicit P2B approval loop."""

# FastAPI consumes decorated functions through route registration.
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .domain import ChangeSet, ChangeSetStatus, FileChange, FileOperation
from .execution import ExecutionBackend, ExecutionError, FakeExecutionBackend
from .patches import (
    ApplyReceipt,
    AuthorizationGrant,
    FilePatch,
    PatchError,
    PatchFileRecord,
    PatchPlan,
    StructuredPatchService,
    _Snapshot,
)
from .paths import AllowedRoots, PathBoundaryError
from .redaction import redact_sensitive_text
from .storage import ProjectStore
from .verification import VerificationProfile, build_verification_request

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18800
DEFAULT_TOKEN_TTL = timedelta(minutes=5)
APPLY_CAPABILITY = "r1.apply"
ROLLBACK_CAPABILITY = "r1.rollback"
VERIFY_CAPABILITY = "r2.verify"


class PatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1)
    operation: FileOperation
    content: str | None = None
    expected_sha256: str | None = None

    def to_file_patch(self) -> FilePatch:
        return FilePatch(self.relative_path, self.operation, self.content, self.expected_sha256)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str = Field(min_length=1)
    project_id: str | None = Field(default=None, min_length=1)
    patches: list[PatchPayload] = Field(min_length=1, max_length=12)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(
        default=APPLY_CAPABILITY,
        pattern=r"^(?:r1\.(?:apply|rollback)|r2\.verify)$",
    )


class ApprovalIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(
        default=APPLY_CAPABILITY,
        pattern=r"^(?:r1\.(?:apply|rollback)|r2\.verify)$",
    )
    user_confirmation: str = Field(
        pattern=r"^(?:执行|确认执行|验证|确认验证|撤销|确认撤销|confirmed)$"
    )


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: VerificationProfile = VerificationProfile.PYTHON_SYNTAX


class TrustProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str = Field(pattern=r"^(?:信任项目|trust)$")


class FeatureDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str = Field(min_length=1)
    patches: list[PatchPayload] = Field(min_length=1, max_length=12)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)
    verification_profile: VerificationProfile = VerificationProfile.PYTHON_SYNTAX


class BugFixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str = Field(min_length=1)
    patches: list[PatchPayload] = Field(min_length=1, max_length=12)
    analysis_evidence_ids: list[str] = Field(min_length=1, max_length=32)
    failure_evidence_ids: list[str] = Field(min_length=1, max_length=32)
    symptom: str = Field(min_length=1, max_length=4000)
    expected_behavior: str = Field(min_length=1, max_length=4000)
    non_goals: list[str] = Field(default_factory=list, max_length=32)
    allowed_files: list[str] = Field(min_length=1, max_length=32)
    verification_profile: VerificationProfile = VerificationProfile.REACT_STATIC


class TestRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str = Field(min_length=1)
    patches: list[PatchPayload] = Field(min_length=1, max_length=12)
    analysis_evidence_ids: list[str] = Field(min_length=1, max_length=32)
    failure_evidence_ids: list[str] = Field(min_length=1, max_length=32)
    symptom: str = Field(min_length=1, max_length=4000)
    expected_behavior: str = Field(min_length=1, max_length=4000)
    non_goals: list[str] = Field(default_factory=list, max_length=32)
    allowed_files: list[str] = Field(min_length=1, max_length=32)
    verification_profile: VerificationProfile = VerificationProfile.PYTHON_RUN


class DeliveryDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    user_confirmation: str


class ProjectRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str = Field(min_length=1)


class _PendingChangeSet:
    def __init__(self, plan: PatchPlan, run_id: str | None = None) -> None:
        self.plan = plan
        self.run_id = run_id
        self.receipt: ApplyReceipt | None = None
        self.memory_approvals: dict[str, tuple[str, datetime]] = {}
        self.memory_decisions: dict[str, dict[str, Any]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _configured_roots() -> AllowedRoots:
    raw = os.environ.get("DEVPILOT_ALLOWED_ROOTS", "")
    values: list[str | Path] = [item for item in raw.split(os.pathsep) if item]
    return AllowedRoots.from_paths(values or [Path.cwd()])


def _configured_data_dir() -> Path:
    return Path(os.environ.get("DEVPILOT_DATA_DIR", "./local-data")).expanduser().resolve()


def _project_id(project: Path, requested: str | None) -> str:
    if requested:
        return requested
    digest = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:16]
    return f"local-{digest}"


def _file_change_payload(plan: PatchPlan) -> list[dict[str, Any]]:
    return [
        {
            "relative_path": item.relative_path,
            "operation": item.operation.value,
            "expected_sha256": item.expected_sha256,
            "after_sha256": item.after_sha256,
            "before_size": item.before_size,
        }
        for item in plan.records
    ]


def plan_payload(entry: _PendingChangeSet) -> dict[str, Any]:
    plan = entry.plan
    return {
        "change_set_id": plan.change_set.id,
        "plan_id": plan.change_set.plan_id,
        "project_id": plan.project_id,
        "run_id": plan.run_id,
        "project_path": str(plan.project_path),
        "status": plan.change_set.status.value,
        "patch_hash": plan.patch_hash,
        "scope_hash": plan.scope_hash,
        "plan_version": 1,
        "risk_summary": (
            "R1 bounded text replacement; project code is not executed by this control plane"
        ),
        "verification": ["precondition_sha256", "post_apply_manifest", "rollback_conflict_check"],
        "stop_conditions": ["stale_hash", "path_denied", "project_locked", "rollback_conflict"],
        "files": _file_change_payload(plan),
        "before_manifest": list(plan.change_set.before_manifest),
        "after_manifest": list(plan.change_set.after_manifest),
        "rollback_manifest": list(plan.change_set.rollback_manifest),
        "diff": plan.diff,
    }


class ControlPlaneManager:
    """Persist and reconstruct proposals; only explicit grants reach the patch engine."""

    def __init__(
        self,
        *,
        allowed_roots: AllowedRoots,
        store: ProjectStore | None = None,
        token_ttl: timedelta = DEFAULT_TOKEN_TTL,
        execution_backend: ExecutionBackend | None = None,
        artifacts_root: str | Path | None = None,
    ) -> None:
        self.store = store
        self.token_ttl = token_ttl
        self.execution_backend = execution_backend
        self.artifacts_root = (
            Path(artifacts_root).expanduser().resolve() if artifacts_root is not None else None
        )
        self.patch_service = StructuredPatchService(allowed_roots=allowed_roots, store=store)
        self._entries: dict[str, _PendingChangeSet] = {}
        self._mutex = threading.RLock()
        if store is not None:
            for change_set_id in store.list_change_set_ids():
                try:
                    self._entries[change_set_id] = self._load_entry(change_set_id)
                except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                    continue

    def preview(
        self, *, project_path: str | Path, project_id: str | None, patches: list[FilePatch]
    ) -> dict[str, Any]:
        try:
            project = self.patch_service.allowed_roots.require_project(project_path)
        except PathBoundaryError as exc:
            raise PatchError("PATH_DENIED", "project is outside allowed roots") from exc
        resolved_project_id = _project_id(project, project_id)
        run_id: str | None = None
        if self.store is not None:
            root_id, fingerprint = self.patch_service.allowed_roots.binding_for(project)
            resolved_project_id = self.store.ensure_project(
                str(project), allowed_root_id=root_id, repo_fingerprint=fingerprint
            )
            run_id = self.store.start_run(resolved_project_id, kind="code-delivery")
        plan = self.patch_service.plan(
            project,
            project_id=resolved_project_id,
            task_id=f"task-{uuid.uuid4()}",
            plan_id=f"plan-{uuid.uuid4()}",
            run_id=run_id,
            patches=patches,
        )
        entry = _PendingChangeSet(plan, run_id)
        with self._mutex:
            self._entries[plan.change_set.id] = entry
        self._event(entry, "changeset.previewed", {"files": len(plan.records)})
        return plan_payload(entry)

    def get(self, change_set_id: str) -> dict[str, Any]:
        with self._mutex:
            entry = self._require(change_set_id)
            payload = plan_payload(entry)
            if self.store is not None:
                contract = self.store.get_change_plan_contract(entry.plan.change_set.plan_id)
                if contract["contract_hash"] is not None:
                    payload.update(
                        {
                            "check_ids": contract["check_ids"],
                            "stop_conditions": contract["stop_conditions"],
                            "evidence_hash": contract["evidence_hash"],
                            "contract_hash": contract["contract_hash"],
                        }
                    )
                if entry.run_id is not None:
                    bug_fix_events = [
                        item
                        for item in self.store.list_run_events(entry.run_id)
                        if item["event_type"] == "bug_fix.proposed"
                    ]
                    if bug_fix_events:
                        event_payload = bug_fix_events[-1]["payload"]
                        if isinstance(event_payload, dict):
                            result_contract: dict[str, Any] = {
                                key: event_payload[key]
                                for key in (
                                    "workflow",
                                    "analysis_evidence_ids",
                                    "failure_evidence_ids",
                                    "analysis_evidence_hash",
                                    "failure_evidence_hash",
                                    "symptom",
                                    "expected_behavior",
                                    "non_goals",
                                    "allowed_files",
                                    "verification_profile",
                                )
                                if key in event_payload
                            }
                            result_contract["contract_hash"] = contract["contract_hash"]
                            payload["bug_fix_contract"] = result_contract
                    test_repair_events = [
                        item
                        for item in self.store.list_run_events(entry.run_id)
                        if item["event_type"] == "test_repair.proposed"
                    ]
                    if test_repair_events:
                        event_payload = test_repair_events[-1]["payload"]
                        if isinstance(event_payload, dict):
                            result_contract = {
                                key: event_payload[key]
                                for key in (
                                    "workflow",
                                    "parent_change_set_id",
                                    "root_change_set_id",
                                    "attempt",
                                    "max_attempts",
                                    "analysis_evidence_ids",
                                    "failure_evidence_ids",
                                    "analysis_evidence_hash",
                                    "failure_evidence_hash",
                                    "evidence_hash",
                                    "failure_fingerprints",
                                    "symptom",
                                    "expected_behavior",
                                    "non_goals",
                                    "allowed_files",
                                    "verification_profile",
                                )
                                if key in event_payload
                            }
                            result_contract["contract_hash"] = contract["contract_hash"]
                            payload["test_repair_contract"] = result_contract
            return payload

    def get_run_id(self, change_set_id: str) -> str | None:
        """Return the server-owned run link without changing the Preview contract."""
        with self._mutex:
            return self._require(change_set_id).run_id

    def deny_execution(self, change_set_id: str, code: str) -> None:
        """Persist a server-side execution denial without consuming approval."""
        with self._mutex:
            entry = self._require(change_set_id)
            self._event(entry, "execution.capability_denied", {"code": code})
            self._finish_run(entry, "failed", {"code": code})

    def finalize_run(self, change_set_id: str, result: str, payload: dict[str, Any]) -> None:
        """Finish a workflow-owned Run after its Artifact is durably recorded."""
        with self._mutex:
            self._finish_run(self._require(change_set_id), result, payload)

    def mark_delivery_failed(
        self,
        change_set_id: str,
        reason: str,
        *,
        workflow: str = "feature-delivery",
        artifact_id: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        """Keep ChangeSet state consistent when verification passed but reporting failed."""
        with self._mutex:
            entry = self._require(change_set_id)
            if entry.plan.change_set.status is ChangeSetStatus.VERIFIED:
                entry.plan.change_set.transition_to(ChangeSetStatus.DELIVERY_FAILED)
                self._persist_status(entry, failure_reason=reason)
            event = {"code": reason}
            if artifact_id is not None:
                event["artifact_id"] = artifact_id
            if event_payload:
                event.update(event_payload)
            self._event(entry, f"{workflow.replace('-', '_')}.failed", event)

    def accept(
        self, change_set_id: str, user_confirmation: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Record the user's final acceptance without changing project files."""
        if user_confirmation not in {"接受", "确认接受", "confirmed"}:
            raise PatchError(
                "USER_CONFIRMATION_REQUIRED", "explicit acceptance confirmation is required"
            )
        with self._mutex:
            entry = self._require(change_set_id)
            replay = self._check_idempotency(
                idempotency_key, "accept", change_set_id, user_confirmation
            )
            if replay is not None:
                return replay
            if self._delivery_decision(entry) is not None:
                raise PatchError(
                    "CHANGESET_DECISION_FINAL", "a delivery decision is already recorded"
                )
            if entry.plan.change_set.status not in {
                ChangeSetStatus.VERIFIED,
                ChangeSetStatus.VERIFICATION_FAILED,
            }:
                raise PatchError(
                    "CHANGESET_NOT_ACCEPTABLE",
                    "ChangeSet must finish verification before acceptance",
                )
            self._reserve_idempotency(
                idempotency_key, "accept", change_set_id, user_confirmation
            )
            before = entry.plan.change_set.status.value
            entry.plan.change_set.transition_to(ChangeSetStatus.ACCEPTED)
            failure_reason = None
            if self.store is not None and before == ChangeSetStatus.VERIFICATION_FAILED.value:
                persisted = self.store.get_change_set(change_set_id) or {}
                failure_reason = persisted.get("failure_reason")
            self._persist_status(entry, failure_reason=failure_reason)
            result = {
                "change_set_id": change_set_id,
                "decision": "accept",
                "status": entry.plan.change_set.status.value,
                "previous_status": before,
                "files_changed": False,
            }
            self._event(
                entry,
                "delivery.accepted",
                {"decision": "accept", "previous_status": before},
            )
            entry.memory_decisions["accept"] = result
            self._complete(idempotency_key, result)
            return result

    def stop(
        self, change_set_id: str, user_confirmation: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Record a stop decision; it never claims success or rolls files back."""
        if user_confirmation not in {"停止", "确认停止", "confirmed"}:
            raise PatchError("USER_CONFIRMATION_REQUIRED", "explicit stop confirmation is required")
        with self._mutex:
            entry = self._require(change_set_id)
            replay = self._check_idempotency(
                idempotency_key, "stop", change_set_id, user_confirmation
            )
            if replay is not None:
                return replay
            if self._delivery_decision(entry) is not None:
                raise PatchError(
                    "CHANGESET_DECISION_FINAL", "a delivery decision is already recorded"
                )
            if entry.plan.change_set.status not in {
                ChangeSetStatus.APPLIED,
                ChangeSetStatus.VERIFIED,
                ChangeSetStatus.VERIFICATION_FAILED,
                ChangeSetStatus.DELIVERY_FAILED,
            }:
                raise PatchError(
                    "CHANGESET_NOT_STOPPABLE",
                    "ChangeSet is not at a delivery decision point",
                )
            self._reserve_idempotency(idempotency_key, "stop", change_set_id, user_confirmation)
            current = entry.plan.change_set.status.value
            result = {
                "change_set_id": change_set_id,
                "decision": "stop",
                "status": current,
                "previous_status": current,
                "files_changed": False,
                "success_claimed": False,
            }
            stop_reason = "user_requested_stop"
            if self.store is not None:
                persisted = self.store.get_change_set(change_set_id) or {}
                stop_reason = str(persisted.get("failure_reason") or stop_reason)
            result["stop_reason"] = stop_reason
            self._event(
                entry,
                "delivery.stopped",
                {"decision": "stop", "status": current, "reason": stop_reason},
            )
            if self.store is not None and entry.run_id is not None:
                try:
                    if self.store.get_run_status(entry.run_id) == "running":
                        self._finish_run(entry, "failed", {"code": "DELIVERY_STOPPED"})
                except (KeyError, ValueError):
                    pass
            entry.memory_decisions["stop"] = result
            self._complete(idempotency_key, result)
            return result

    def _delivery_decision(self, entry: _PendingChangeSet) -> str | None:
        if entry.memory_decisions:
            return next(iter(entry.memory_decisions))
        if self.store is not None and entry.run_id is not None:
            for event in reversed(self.store.list_run_events(entry.run_id)):
                if event["event_type"] == "delivery.accepted":
                    return "accept"
                if event["event_type"] == "delivery.stopped":
                    return "stop"
        return None

    def approve(self, change_set_id: str, capability: str = APPLY_CAPABILITY) -> dict[str, Any]:
        if capability not in {APPLY_CAPABILITY, ROLLBACK_CAPABILITY, VERIFY_CAPABILITY}:
            raise PatchError("CAPABILITY_INVALID", "unsupported approval capability")
        with self._mutex:
            entry = self._require(change_set_id)
            self._require_trusted_project(entry)
            if capability == APPLY_CAPABILITY and entry.plan.change_set.status in {
                ChangeSetStatus.APPLIED,
                ChangeSetStatus.REVERTED,
            }:
                raise PatchError("CHANGESET_NOT_APPLICABLE", "ChangeSet is not awaiting apply")
            if (
                capability == VERIFY_CAPABILITY
                and entry.plan.change_set.status is not ChangeSetStatus.APPLIED
            ):
                raise PatchError(
                    "CHANGESET_NOT_VERIFIABLE",
                    "ChangeSet must be applied before verification",
                )
            if self.store is not None and self.store.has_active_approval(change_set_id, capability):
                raise PatchError("APPROVAL_ALREADY_ISSUED", "ChangeSet already has active approval")
            if capability in entry.memory_approvals:
                raise PatchError("APPROVAL_ALREADY_ISSUED", "ChangeSet already has active approval")
            if entry.plan.change_set.status is ChangeSetStatus.PLANNED:
                entry.plan.change_set.transition_to(ChangeSetStatus.APPROVAL_PENDING)
                self._persist_status(entry)
            token = secrets.token_urlsafe(32)
            expires_at = _now() + self.token_ttl
            approval_id = str(uuid.uuid4())
            if self.store is not None:
                contract = self.store.get_change_plan_contract(entry.plan.change_set.plan_id)
                self.store.create_approval(
                    approval_id=approval_id,
                    change_set_id=change_set_id,
                    plan_hash=entry.plan.patch_hash,
                    scope_hash=entry.plan.scope_hash,
                    patch_hash=entry.plan.patch_hash,
                    contract_hash=contract["contract_hash"],
                    capability=capability,
                    token_digest=_digest(token),
                    expires_at=expires_at,
                )
            else:
                entry.memory_approvals[capability] = (token, expires_at)
            self._event(entry, "changeset.approval_issued", {"capability": capability})
            return {
                "change_set_id": change_set_id,
                "capability": capability,
                "status": entry.plan.change_set.status.value,
                "expires_at": expires_at.isoformat(),
                "approval_token": token,
            }

    def verify(
        self,
        change_set_id: str,
        approval_token: str,
        *,
        profile: VerificationProfile = VerificationProfile.PYTHON_SYNTAX,
        idempotency_key: str | None = None,
        finalize_run: bool = True,
    ) -> dict[str, Any]:
        """Run one allowlisted verification profile in the configured backend."""
        with self._mutex:
            entry = self._require(change_set_id)
            self._require_trusted_project(entry)
            replay = self._check_idempotency(
                idempotency_key,
                "verify",
                change_set_id,
                approval_token,
                request_fingerprint=profile.value,
            )
            if replay is not None:
                return replay
            if self.execution_backend is None:
                code = "EXECUTION_BACKEND_UNAVAILABLE"
                self._event(entry, "execution.capability_denied", {"code": code})
                if finalize_run:
                    self._finish_run(entry, "failed", {"code": code})
                raise PatchError(
                    "EXECUTION_BACKEND_UNAVAILABLE", "no execution backend is configured"
                )
            if type(self.execution_backend) is not FakeExecutionBackend:
                self._event(
                    entry,
                    "execution.capability_denied",
                    {"code": "EXECUTION_BACKEND_UNAVAILABLE"},
                )
                if finalize_run:
                    self._finish_run(entry, "failed", {"code": "EXECUTION_BACKEND_UNAVAILABLE"})
                raise PatchError(
                    "EXECUTION_BACKEND_UNAVAILABLE",
                    "real execution is unavailable before the P6 implementation gate",
                )
            if entry.plan.change_set.status is not ChangeSetStatus.APPLIED:
                raise PatchError(
                    "CHANGESET_NOT_VERIFIABLE",
                    "ChangeSet must be applied before verification",
                )
            try:
                request = build_verification_request(entry.plan, profile)
            except ValueError as exc:
                self._event(
                    entry,
                    "changeset.verification_failed",
                    {"code": "VERIFICATION_PROFILE_UNSUPPORTED"},
                )
                if finalize_run:
                    self._finish_run(entry, "failed", {"code": "VERIFICATION_PROFILE_UNSUPPORTED"})
                raise PatchError("VERIFICATION_PROFILE_UNSUPPORTED", str(exc)) from exc
            self._consume_approval(entry, approval_token, VERIFY_CAPABILITY)
            self._reserve_idempotency(
                idempotency_key,
                "verify",
                change_set_id,
                approval_token,
                request_fingerprint=profile.value,
            )
            reserved = idempotency_key is not None
            try:
                entry.plan.change_set.transition_to(ChangeSetStatus.VERIFYING)
                self._persist_status(entry)
                result = self.execution_backend.run(request)
            except ExecutionError as exc:
                if reserved:
                    self._abort(idempotency_key)
                entry.plan.change_set.transition_to(ChangeSetStatus.VERIFICATION_FAILED)
                self._persist_status(entry, failure_reason=exc.code)
                self._event(entry, "changeset.verification_failed", {"code": exc.code})
                if finalize_run:
                    self._finish_run(entry, "failed", {"code": exc.code})
                raise PatchError("VERIFICATION_FAILED", "sandbox verification failed") from exc
            except Exception as exc:
                if reserved:
                    self._abort(idempotency_key)
                entry.plan.change_set.transition_to(ChangeSetStatus.VERIFICATION_FAILED)
                self._persist_status(entry, failure_reason="EXECUTION_BACKEND_FAILED")
                self._event(
                    entry,
                    "changeset.verification_failed",
                    {"code": "EXECUTION_BACKEND_FAILED"},
                )
                if finalize_run:
                    self._finish_run(entry, "failed", {"code": "EXECUTION_BACKEND_FAILED"})
                raise PatchError("VERIFICATION_FAILED", "execution backend failed") from exc
            target = (
                ChangeSetStatus.VERIFIED
                if result.exit_code == 0
                else ChangeSetStatus.VERIFICATION_FAILED
            )
            entry.plan.change_set.transition_to(target)
            self._persist_status(
                entry,
                failure_reason=None if result.exit_code == 0 else "NONZERO_EXIT",
            )
            payload = {
                "backend": result.backend,
                "sandbox_id": result.sandbox_id,
                "profile": profile.value,
                "command_profile": result.command_profile or profile.value,
                "bundle_bytes": result.bundle_bytes,
                "region": result.region,
                "template": result.template,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "stdout": redact_sensitive_text(result.stdout[-8000:]),
                "stderr": redact_sensitive_text(result.stderr[-8000:]),
            }
            self._event(entry, "changeset.verification_completed", payload)
            if finalize_run:
                self._finish_run(
                    entry,
                    "completed" if result.exit_code == 0 else "failed",
                    {
                        "change_set_id": change_set_id,
                        "status": target.value,
                        "exit_code": result.exit_code,
                    },
                )
            response = {
                "change_set_id": change_set_id,
                "status": target.value,
                "backend": result.backend,
                "sandbox_id": result.sandbox_id,
                "profile": profile.value,
                "command_profile": result.command_profile or profile.value,
                "bundle_bytes": result.bundle_bytes,
                "region": result.region,
                "template": result.template,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "stdout": redact_sensitive_text(result.stdout[-8000:]),
                "stderr": redact_sensitive_text(result.stderr[-8000:]),
            }
            self._complete(idempotency_key, response)
            return response

    def apply(
        self, change_set_id: str, approval_token: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        with self._mutex:
            entry = self._require(change_set_id)
            self._require_trusted_project(entry)
            replay = self._check_idempotency(
                idempotency_key, "apply", change_set_id, approval_token
            )
            if replay is not None:
                return replay
            approval = self._consume_approval(entry, approval_token, APPLY_CAPABILITY)
            self._reserve_idempotency(idempotency_key, "apply", change_set_id, approval_token)
            reserved = idempotency_key is not None
            grant = AuthorizationGrant(
                change_set_id=entry.plan.change_set.id,
                project_id=entry.plan.project_id,
                patch_hash=entry.plan.patch_hash,
                scope_hash=entry.plan.scope_hash,
                capability="r1",
                expires_at=approval["expires_at"],
                id=str(approval["id"]),
            )
            try:
                receipt = self.patch_service.apply(entry.plan, grant)
            except PatchError as exc:
                if reserved:
                    self._abort(idempotency_key)
                self._event(entry, "changeset.apply_failed", {"code": exc.code})
                self._finish_run(entry, "failed", {"code": exc.code})
                raise
            except Exception as exc:
                if reserved:
                    self._abort(idempotency_key)
                self._event(entry, "changeset.apply_failed", {"code": "APPLY_FAILED"})
                self._finish_run(entry, "failed", {"code": "APPLY_FAILED"})
                raise RuntimeError("apply operation failed") from exc
            entry.receipt = receipt
            self._event(entry, "changeset.applied", {"files": len(entry.plan.records)})
            result = {
                "change_set_id": change_set_id,
                "status": entry.plan.change_set.status.value,
                "patch_hash": entry.plan.patch_hash,
                "scope_hash": entry.plan.scope_hash,
                "files": _file_change_payload(entry.plan),
            }
            self._complete(idempotency_key, result)
            return result

    def rollback(
        self, change_set_id: str, approval_token: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        with self._mutex:
            entry = self._require(change_set_id)
            self._require_trusted_project(entry)
            replay = self._check_idempotency(
                idempotency_key, "rollback", change_set_id, approval_token
            )
            if replay is not None:
                return replay
            self._consume_approval(entry, approval_token, ROLLBACK_CAPABILITY)
            self._reserve_idempotency(idempotency_key, "rollback", change_set_id, approval_token)
            reserved = idempotency_key is not None
            receipt = entry.receipt or ApplyReceipt(
                change_set_id=entry.plan.change_set.id,
                project_id=entry.plan.project_id,
                project_path=entry.plan.project_path,
                before=entry.plan.records,
                after=entry.plan.records,
                snapshots=entry.plan.snapshots,
            )
            try:
                self.patch_service.rollback(receipt, entry.plan)
            except Exception:
                if reserved:
                    self._abort(idempotency_key)
                raise
            entry.receipt = receipt
            self._event(entry, "changeset.reverted", {})
            result = {"change_set_id": change_set_id, "status": entry.plan.change_set.status.value}
            self._complete(idempotency_key, result)
            return result

    def revoke(self, change_set_id: str) -> dict[str, Any]:
        with self._mutex:
            entry = self._require(change_set_id)
            count = self.store.revoke_approvals(change_set_id) if self.store is not None else 0
            entry.memory_approvals.clear()
            self._event(entry, "changeset.approvals_revoked", {"count": count})
            return {"change_set_id": change_set_id, "revoked": count}

    def trust_project(self, project_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation not in {"信任项目", "trust"}:
            raise PatchError(
                "PROJECT_TRUST_CONFIRMATION_REQUIRED", "explicit project trust is required"
            )
        if self.store is None:
            raise PatchError("PERSISTENCE_REQUIRED", "project trust requires durable storage")
        try:
            project_path = self.store.get_project_path(project_id)
            root_id, fingerprint = self.patch_service.allowed_roots.binding_for(project_path)
            return self.store.trust_project(
                project_id,
                repo_fingerprint=fingerprint,
                allowed_root_id=root_id,
            )
        except (KeyError, PathBoundaryError, ValueError) as exc:
            raise PatchError("PROJECT_TRUST_INVALID", str(exc)) from exc

    def request_approval(
        self, change_set_id: str, capability: str, user_confirmation: str
    ) -> dict[str, Any]:
        """Record a confirmed approval intent without issuing a capability token."""
        expected_confirmations = {
            APPLY_CAPABILITY: {"执行", "确认执行", "confirmed"},
            VERIFY_CAPABILITY: {"验证", "确认验证", "confirmed"},
            ROLLBACK_CAPABILITY: {"撤销", "确认撤销", "confirmed"},
        }
        if capability not in expected_confirmations:
            raise PatchError("CAPABILITY_INVALID", "unsupported approval capability")
        if user_confirmation not in expected_confirmations[capability]:
            raise PatchError("USER_CONFIRMATION_REQUIRED", "confirmation does not match capability")
        with self._mutex:
            entry = self._require(change_set_id)
            self._require_trusted_project(entry)
            if self.store is not None and self.store.has_active_approval(change_set_id, capability):
                raise PatchError("APPROVAL_ALREADY_ISSUED", "ChangeSet already has active approval")
            self._event(entry, "changeset.approval_requested", {"capability": capability})
            return {
                "change_set_id": change_set_id,
                "capability": capability,
                "status": "approval_pending",
                "requires_control_plane_user_action": True,
            }

    def project_security(self, project_id: str) -> dict[str, Any]:
        if self.store is None:
            raise PatchError("PERSISTENCE_REQUIRED", "project state requires durable storage")
        try:
            return self.store.get_project_security(project_id)
        except KeyError as exc:
            raise PatchError("PROJECT_NOT_FOUND", "project does not exist") from exc

    def register_project(self, project_path: str) -> dict[str, Any]:
        if self.store is None:
            raise PatchError(
                "PERSISTENCE_REQUIRED", "project registration requires durable storage"
            )
        try:
            project = self.patch_service.allowed_roots.require_project(project_path)
            root_id, fingerprint = self.patch_service.allowed_roots.binding_for(project)
            project_id = self.store.ensure_project(
                str(project), allowed_root_id=root_id, repo_fingerprint=fingerprint
            )
            return self.store.get_project_security(project_id)
        except (KeyError, PathBoundaryError, ValueError) as exc:
            raise PatchError("PATH_DENIED", "project is outside the allowed roots") from exc

    def _require_trusted_project(self, entry: _PendingChangeSet) -> None:
        if self.store is None:
            raise PatchError(
                "PERSISTENCE_REQUIRED",
                "durable project trust is required for R1/R2 operations",
            )
        try:
            root_id, fingerprint = self.patch_service.allowed_roots.binding_for(
                entry.plan.project_path
            )
            self.store.require_trusted_project(
                entry.plan.project_id,
                repo_fingerprint=fingerprint,
                allowed_root_id=root_id,
            )
        except (KeyError, PathBoundaryError, ValueError) as exc:
            self._event(entry, "project.trust_denied", {"code": "PROJECT_TRUST_REQUIRED"})
            raise PatchError("PROJECT_TRUST_REQUIRED", str(exc)) from exc

    def _consume_approval(
        self, entry: _PendingChangeSet, token: str, capability: str
    ) -> dict[str, Any]:
        if not token:
            raise PatchError("APPROVAL_INVALID", "operator approval is required")
        if self.store is not None:
            candidate = self.store.peek_approval(_digest(token), now=_now())
            if candidate is None:
                raise PatchError("APPROVAL_INVALID", "approval is invalid, expired or consumed")
            if (
                candidate["change_set_id"] != entry.plan.change_set.id
                or candidate["capability"] != capability
            ):
                raise PatchError(
                    "APPROVAL_SCOPE_MISMATCH", "approval scope does not permit operation"
                )
            if (
                candidate["patch_hash"] != entry.plan.patch_hash
                or candidate["scope_hash"] != entry.plan.scope_hash
            ):
                raise PatchError(
                    "APPROVAL_SCOPE_MISMATCH", "approval scope does not match ChangeSet"
                )
            expected_contract = self.store.get_change_plan_contract(entry.plan.change_set.plan_id)[
                "contract_hash"
            ]
            if candidate.get("contract_hash") != expected_contract:
                raise PatchError(
                    "APPROVAL_SCOPE_MISMATCH", "approval contract does not match ChangeSet"
                )
            approval = self.store.consume_approval(_digest(token), now=_now())
            if approval is None:
                raise PatchError("APPROVAL_INVALID", "approval was consumed or expired")
            return approval
        issued = entry.memory_approvals.pop(capability, None)
        if issued is None or _now() >= issued[1] or not secrets.compare_digest(issued[0], token):
            raise PatchError("APPROVAL_INVALID", "approval is invalid, expired or consumed")
        return {"id": f"memory-{uuid.uuid4()}", "expires_at": issued[1]}

    def _request_hash(
        self,
        operation: str,
        change_set_id: str,
        token: str,
        request_fingerprint: str = "",
    ) -> str:
        parts: list[str] = [operation, change_set_id, _digest(token)]
        if request_fingerprint:
            parts.append(request_fingerprint)
        return _digest(
            json.dumps(
                parts,
                sort_keys=True,
            )
        )

    def _check_idempotency(
        self,
        key: str | None,
        operation: str,
        change_set_id: str,
        token: str,
        *,
        request_fingerprint: str = "",
    ) -> dict[str, Any] | None:
        if self.store is None or key is None:
            return None
        if not 1 <= len(key) <= 128:
            raise PatchError("IDEMPOTENCY_INVALID", "Idempotency-Key must be 1–128 characters")
        existing = self.store.get_idempotency(key)
        if existing is None:
            return None
        if existing[0] != self._request_hash(operation, change_set_id, token, request_fingerprint):
            raise PatchError("IDEMPOTENCY_CONFLICT", "Idempotency-Key was reused with new content")
        if existing[1] is None:
            raise PatchError("IDEMPOTENCY_IN_PROGRESS", "request with this key is still running")
        return json.loads(existing[1])

    def _reserve_idempotency(
        self,
        key: str | None,
        operation: str,
        change_set_id: str,
        token: str,
        *,
        request_fingerprint: str = "",
    ) -> None:
        if self.store is None or key is None:
            return
        try:
            state, _response = self.store.reserve_idempotency(
                key, self._request_hash(operation, change_set_id, token, request_fingerprint)
            )
        except ValueError as exc:
            raise PatchError(
                "IDEMPOTENCY_CONFLICT", "Idempotency-Key was reused with new content"
            ) from exc
        if state == "replay":
            return
        if state == "in_progress":
            raise PatchError("IDEMPOTENCY_IN_PROGRESS", "request with this key is still running")

    def _complete(self, key: str | None, result: dict[str, Any]) -> None:
        if self.store is not None and key is not None:
            self.store.complete_idempotency(
                key, json.dumps(result, ensure_ascii=False, sort_keys=True)
            )

    def _abort(self, key: str | None) -> None:
        if self.store is not None and key is not None:
            self.store.abort_idempotency(key)

    def _require(self, change_set_id: str) -> _PendingChangeSet:
        entry = self._entries.get(change_set_id)
        if entry is not None:
            return entry
        if self.store is not None:
            entry = self._load_entry(change_set_id)
            self._entries[change_set_id] = entry
            return entry
        raise PatchError("CHANGESET_NOT_FOUND", "ChangeSet does not exist")

    def _load_entry(self, change_set_id: str) -> _PendingChangeSet:
        if self.store is None:
            raise PatchError("CHANGESET_NOT_FOUND", "ChangeSet does not exist")
        raw = self.store.get_change_set(change_set_id)
        if raw is None:
            raise PatchError("CHANGESET_NOT_FOUND", "ChangeSet does not exist")
        if (
            not raw["project_path"]
            or not raw["patches_json"]
            or not raw["project_id"]
        ):
            raise PatchError("PERSISTENCE_CORRUPT", "ChangeSet recovery material is incomplete")
        patch_values = json.loads(raw["patches_json"])
        patches = tuple(
            FilePatch(
                item["relative_path"],
                FileOperation(item["operation"]),
                item.get("content"),
                item.get("expected_sha256"),
            )
            for item in patch_values
        )
        snapshots = tuple(
            _Snapshot(
                item["relative_path"],
                bool(item["existed"]),
                item["content"].encode("utf-8") if item["content"] is not None else None,
                item.get("sha256"),
            )
            for item in json.loads(raw["snapshots_json"] or "[]")
        )
        records = tuple(
            PatchFileRecord(
                item["relative_path"],
                FileOperation(item["operation"]),
                item.get("expected_sha256"),
                item["content_hash"],
                int(item["before_size"]),
                item.get("after_sha256"),
            )
            for item in raw["file_changes"]
        )
        file_changes = tuple(
            FileChange(
                item.relative_path,
                item.operation,
                item.content_hash,
                item.expected_sha256,
                item.before_size,
                item.after_sha256,
            )
            for item in records
        )
        change_set = ChangeSet(
            task_id=raw["task_id"],
            plan_id=raw["plan_id"],
            patch_hash=raw["patch_hash"],
            id=raw["id"],
            status=ChangeSetStatus(raw["status"]),
            file_changes=file_changes,
            before_manifest=tuple(json.loads(raw["before_manifest_json"])),
            after_manifest=tuple(json.loads(raw["after_manifest_json"])),
            rollback_manifest=tuple(json.loads(raw["rollback_manifest_json"])),
        )
        plan = PatchPlan(
            project_id=raw["project_id"],
            project_path=Path(raw["project_path"]),
            patches=patches,
            change_set=change_set,
            scope_hash=raw["scope_hash"] or "",
            patch_hash=raw["patch_hash"],
            diff=self._diff_from_patches(patches, snapshots),
            records=records,
            snapshots=snapshots,
            run_id=raw.get("run_id"),
        )
        return _PendingChangeSet(plan, raw.get("run_id"))

    @staticmethod
    def _diff_from_patches(patches: tuple[FilePatch, ...], snapshots: tuple[_Snapshot, ...]) -> str:
        import difflib

        before = {item.relative_path: item.content or b"" for item in snapshots}
        parts: list[str] = []
        for patch in patches:
            old = before.get(patch.relative_path, b"").decode("utf-8").splitlines(keepends=True)
            new = (patch.content or "").splitlines(keepends=True)
            parts.extend(
                difflib.unified_diff(
                    old, new, fromfile=f"a/{patch.relative_path}", tofile=f"b/{patch.relative_path}"
                )
            )
        return "".join(parts)

    def _persist_status(
        self, entry: _PendingChangeSet, *, failure_reason: str | None = None
    ) -> None:
        if self.store is not None:
            self.store.update_change_set_status(
                entry.plan.change_set.id,
                entry.plan.change_set.status.value,
                failure_reason=failure_reason,
            )

    def _event(self, entry: _PendingChangeSet, event_type: str, payload: dict[str, Any]) -> None:
        if self.store is not None and entry.run_id is not None:
            self.store.append_event(entry.run_id, event_type, payload)

    def _finish_run(self, entry: _PendingChangeSet, result: str, payload: dict[str, Any]) -> None:
        if self.store is not None and entry.run_id is not None:
            self.store.finish_run(entry.run_id, result, payload)


def _header_token(authorization: str | None) -> str:
    prefix = "DevPilotGrant "
    if authorization is None or not authorization.startswith(prefix):
        raise PatchError("APPROVAL_INVALID", "Authorization must use DevPilotGrant scheme")
    token = authorization[len(prefix) :].strip()
    if not token or len(token) > 512:
        raise PatchError("APPROVAL_INVALID", "approval token is invalid")
    return token


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _request_id(request: Request) -> str:
    candidate = request.headers.get("X-Request-ID")
    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return f"req_{uuid.uuid4().hex}"


def _error_payload(request: Request, code: str, message: str) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message},
        "request_id": getattr(request.state, "request_id", _request_id(request)),
    }


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove local filesystem coordinates from control-plane responses."""
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            return {
                key: clean(item)
                for key, item in mapping.items()
                if key not in {"project_path", "real_path"}
            }
        if isinstance(value, list):
            items = cast(list[Any], value)
            return [clean(item) for item in items]
        return value

    return cast(dict[str, Any], clean(payload))


def _contract_payload(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    return _public_payload(payload) if request.url.path.startswith("/api/v1/") else payload


async def _json_response_with_request_id(response: Any, request_id: str) -> Any:
    content_type = response.headers.get("content-type", "")
    body = getattr(response, "body", None)
    if body is None and "application/json" in content_type:
        body = b"".join([chunk async for chunk in response.body_iterator])
    if "application/json" not in content_type or not body:
        response.headers["X-Request-ID"] = request_id
        return response
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        response.headers["X-Request-ID"] = request_id
        return response
    if not isinstance(payload, dict):
        response.headers["X-Request-ID"] = request_id
        return response
    payload = cast(dict[str, Any], payload)
    payload.setdefault("request_id", request_id)
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type", "x-request-id"}
    }
    return JSONResponse(
        status_code=response.status_code,
        content=payload,
        headers={**headers, "X-Request-ID": request_id},
        background=getattr(response, "background", None),
    )


def _configured_skills_root() -> Path:
    default_root = Path(__file__).resolve().parents[2] / "skills" / "catalog"
    return Path(os.environ.get("DEVPILOT_SKILLS_ROOT", str(default_root))).expanduser().resolve()


def _skill_catalog() -> list[dict[str, Any]]:
    from .skill_manager import validate_skill

    root = _configured_skills_root()
    if not root.is_dir():
        return []
    catalog: list[dict[str, Any]] = []
    for skill_file in sorted(root.glob("*/SKILL.md")):
        result = validate_skill(skill_file)
        catalog.append(
            {
                "id": result.name or skill_file.parent.name,
                "version": result.version,
                "status": result.status or "invalid",
                "reviewed": result.reviewed,
                "valid": result.valid,
                "errors": list(result.errors),
            }
        )
    return catalog


def _backend_catalog(manager: ControlPlaneManager) -> list[dict[str, Any]]:
    backend = manager.execution_backend
    if backend is None:
        return [
            {
                "name": "none",
                "status": "unavailable",
                "verified": False,
                "capability": "verification-only",
                "network_allowed": False,
            }
        ]
    config = getattr(backend, "config", None)
    return [
        {
            "name": backend.name,
            "status": "configured",
            "verified": False,
            "capability": "verification-only",
            "network_allowed": bool(getattr(config, "allow_network", False)),
            "region": str(getattr(config, "region", "")),
            "template": str(getattr(config, "template", "")),
        }
    ]


def create_app(
    *,
    manager: ControlPlaneManager | None = None,
    allowed_roots: AllowedRoots | None = None,
    data_dir: str | Path | None = None,
) -> FastAPI:
    if manager is None:
        resolved_data_dir = (
            Path(data_dir) if data_dir is not None else _configured_data_dir()
        ).expanduser().resolve()
        store = ProjectStore(resolved_data_dir / "devpilot.db")
        store.require_schema_head("0007_project_trust")
        execution_backend: ExecutionBackend | None = None
        if os.environ.get("DEVPILOT_ENABLE_FC_SANDBOX") == "1":
            from .fc_sandbox import build_fc_backend_from_environment

            execution_backend = build_fc_backend_from_environment()
        manager = ControlPlaneManager(
            allowed_roots=allowed_roots or _configured_roots(),
            store=store,
            execution_backend=execution_backend,
            artifacts_root=resolved_data_dir,
        )
    app = FastAPI(title="DevPilot Local Control Plane", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:18801",
            "http://localhost:18801",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    )
    app.state.manager = manager
    app.state.idempotency_request_ids = {}

    @app.middleware("http")
    async def require_loopback_and_contract(request: Request, call_next: Any) -> Any:
        request_id = _request_id(request)
        request.state.request_id = request_id
        client = request.client.host if request.client is not None else None
        forwarded = request.headers.get("Forwarded") or request.headers.get("X-Forwarded-For")
        if client not in {"127.0.0.1", "::1", "localhost"} or forwarded:
            response = JSONResponse(
                status_code=403,
                content=_error_payload(
                    request,
                    "PATH_DENIED",
                    "local control plane only; forwarded requests are not accepted",
                ),
            )
        else:
            response = await call_next(request)
        response_request_id = request_id
        key = request.headers.get("Idempotency-Key")
        if key and response.status_code < 400:
            request_ids = cast(dict[str, str], app.state.idempotency_request_ids)
            response_request_id = request_ids.setdefault(key, request_id)
            if len(request_ids) > 4096:
                request_ids.pop(next(iter(request_ids)))
        return await _json_response_with_request_id(response, response_request_id)

    @app.exception_handler(PatchError)
    async def patch_error_handler(request: Request, exc: PatchError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", _request_id(request))
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": exc.code, "message": exc.message}, "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, _: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", _request_id(request))
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {"code": "REQUEST_INVALID", "message": "request payload is invalid"},
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, _: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", _request_id(request))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {"code": "INTERNAL_ERROR", "message": "internal server error"},
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "devpilot-control-plane"}

    @app.get("/api/v1/projects")
    @app.get("/v1/projects")
    def list_projects() -> dict[str, Any]:
        if manager.store is None:
            raise PatchError("PERSISTENCE_REQUIRED", "project state requires durable storage")
        return {"projects": manager.store.list_projects()}

    @app.post("/api/v1/projects", status_code=status.HTTP_201_CREATED)
    @app.post("/v1/projects", status_code=status.HTTP_201_CREATED)
    def register_project(payload: ProjectRegistrationRequest) -> dict[str, Any]:
        return manager.register_project(payload.project_path)

    @app.get("/api/v1/projects/{project_id}")
    @app.get("/v1/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return manager.project_security(project_id)

    @app.get("/api/v1/security/backends")
    @app.get("/v1/security/backends")
    def list_backends() -> dict[str, Any]:
        return {"backends": _backend_catalog(manager)}

    @app.get("/api/v1/skills")
    @app.get("/v1/skills")
    def list_skills() -> dict[str, Any]:
        return {"skills": _skill_catalog()}

    @app.post("/api/v1/change-sets/preview", status_code=status.HTTP_201_CREATED)
    @app.post("/v1/change-sets/preview", status_code=status.HTTP_201_CREATED)
    def preview(request: Request, payload: PreviewRequest) -> dict[str, Any]:
        return _contract_payload(request, manager.preview(
            project_path=payload.project_path,
            project_id=payload.project_id,
            patches=[item.to_file_patch() for item in payload.patches],
        ))

    @app.post("/api/v1/feature-delivery/proposals", status_code=status.HTTP_201_CREATED)
    @app.post("/v1/feature-delivery/proposals", status_code=status.HTTP_201_CREATED)
    def feature_delivery_proposal(
        request: Request, payload: FeatureDeliveryRequest
    ) -> dict[str, Any]:
        from .workflows import FeatureDeliveryWorkflow, WorkflowError

        try:
            proposal = FeatureDeliveryWorkflow(manager).propose(
                project_path=payload.project_path,
                patches=[item.to_file_patch() for item in payload.patches],
                evidence_ids=payload.evidence_ids,
                verification_profile=payload.verification_profile,
            )
        except WorkflowError as exc:
            raise PatchError(exc.code, exc.message) from exc
        return _contract_payload(request, proposal.to_dict())

    @app.post("/api/v1/bug-fix/proposals", status_code=status.HTTP_201_CREATED)
    @app.post("/v1/bug-fix/proposals", status_code=status.HTTP_201_CREATED)
    def bug_fix_proposal(request: Request, payload: BugFixRequest) -> dict[str, Any]:
        from .workflows import BugFixWorkflow, WorkflowError

        try:
            proposal = BugFixWorkflow(manager).propose(
                project_path=payload.project_path,
                patches=[item.to_file_patch() for item in payload.patches],
                analysis_evidence_ids=payload.analysis_evidence_ids,
                failure_evidence_ids=payload.failure_evidence_ids,
                symptom=payload.symptom,
                expected_behavior=payload.expected_behavior,
                non_goals=payload.non_goals,
                allowed_files=payload.allowed_files,
                verification_profile=payload.verification_profile,
            )
        except WorkflowError as exc:
            raise PatchError(exc.code, exc.message) from exc
        return _contract_payload(request, proposal.to_dict())

    @app.post("/api/v1/test-repair/proposals", status_code=status.HTTP_201_CREATED)
    @app.post("/v1/test-repair/proposals", status_code=status.HTTP_201_CREATED)
    def test_repair_proposal(request: Request, payload: TestRepairRequest) -> dict[str, Any]:
        from .test_repair import TestRepairWorkflow, WorkflowError

        try:
            proposal = TestRepairWorkflow(manager).propose(
                project_path=payload.project_path,
                patches=[item.to_file_patch() for item in payload.patches],
                analysis_evidence_ids=payload.analysis_evidence_ids,
                failure_evidence_ids=payload.failure_evidence_ids,
                symptom=payload.symptom,
                expected_behavior=payload.expected_behavior,
                non_goals=payload.non_goals,
                allowed_files=payload.allowed_files,
                verification_profile=payload.verification_profile,
            )
        except WorkflowError as exc:
            raise PatchError(exc.code, exc.message) from exc
        return _contract_payload(request, proposal.to_dict())

    @app.get("/api/v1/change-sets/{change_set_id}/delivery-report")
    @app.get("/v1/change-sets/{change_set_id}/delivery-report")
    def delivery_report(change_set_id: str) -> dict[str, Any]:
        from .delivery_decisions import DeliveryDecisionWorkflow

        return DeliveryDecisionWorkflow(manager).report(change_set_id)

    @app.post("/api/v1/change-sets/{change_set_id}/decision")
    @app.post("/v1/change-sets/{change_set_id}/decision")
    def delivery_decision(
        change_set_id: str,
        payload: DeliveryDecisionRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if idempotency_key is None:
            raise PatchError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        from .delivery_decisions import DeliveryDecisionWorkflow

        workflow = DeliveryDecisionWorkflow(manager)
        if payload.decision == "accept":
            return workflow.accept(
                change_set_id, payload.user_confirmation, idempotency_key=idempotency_key
            )
        if payload.decision != "stop":
            raise PatchError("DECISION_INVALID", "decision must be accept or stop")
        return workflow.stop(
            change_set_id, payload.user_confirmation, idempotency_key=idempotency_key
        )

    @app.get("/api/v1/change-sets/{change_set_id}")
    @app.get("/v1/change-sets/{change_set_id}")
    def get_change_set(request: Request, change_set_id: str) -> dict[str, Any]:
        return _contract_payload(request, manager.get(change_set_id))

    @app.get("/api/v1/change-sets/{change_set_id}/preview")
    @app.get("/v1/change-sets/{change_set_id}/preview")
    def get_change_set_preview(request: Request, change_set_id: str) -> dict[str, Any]:
        return _contract_payload(request, manager.get(change_set_id))

    @app.get("/api/v1/change-sets/{change_set_id}/diff")
    @app.get("/v1/change-sets/{change_set_id}/diff")
    def get_change_set_diff(change_set_id: str) -> dict[str, Any]:
        payload = manager.get(change_set_id)
        return {
            "change_set_id": change_set_id,
            "patch_hash": payload["patch_hash"],
            "scope_hash": payload["scope_hash"],
            "files": payload["files"],
            "diff": payload["diff"],
        }

    @app.get("/api/v1/runs/{run_id}")
    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        if manager.store is None:
            raise PatchError("PERSISTENCE_REQUIRED", "run state requires durable storage")
        try:
            return manager.store.get_run_state(run_id)
        except KeyError as exc:
            raise PatchError("RUN_NOT_FOUND", "Run does not exist") from exc

    @app.get("/api/v1/runs/{run_id}/events", response_class=StreamingResponse)
    @app.get("/v1/runs/{run_id}/events", response_class=StreamingResponse)
    def get_run_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> Any:
        if manager.store is None:
            raise PatchError("PERSISTENCE_REQUIRED", "run events require durable storage")
        try:
            manager.store.get_run_status(run_id)
        except KeyError as exc:
            raise PatchError("RUN_NOT_FOUND", "Run does not exist") from exc
        cursor = 0
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content=_error_payload(
                        request,
                        "REQUEST_INVALID",
                        "Last-Event-ID must be a non-negative integer",
                    ),
                )
            if cursor < 0:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={
                        "error": {
                            "code": "REQUEST_INVALID",
                            "message": "Last-Event-ID must be a non-negative integer",
                        },
                        "request_id": getattr(request.state, "request_id", _request_id(request)),
                    },
                )
        events = [
            item
            for item in manager.store.list_run_events(run_id)
            if int(item["event_id"]) > cursor
        ]

        def stream() -> Any:
            for item in events:
                event_type = str(item["event_type"])
                event = {
                    "event_id": item["event_id"],
                    "run_id": item["run_id"],
                    "correlation_id": item["correlation_id"],
                    "sequence": item["sequence"],
                    "type": event_type,
                    "created_at": item["created_at"],
                    "redacted_payload": item["payload"],
                }
                yield (
                    f"id: {item['event_id']}\n"
                    f"event: {event_type}\n"
                    f"data: {json.dumps(event, ensure_ascii=False, sort_keys=True)}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/runs/{run_id}/artifacts")
    @app.get("/v1/runs/{run_id}/artifacts")
    def get_run_artifacts(run_id: str) -> dict[str, Any]:
        if manager.store is None:
            raise PatchError("PERSISTENCE_REQUIRED", "artifact state requires durable storage")
        try:
            manager.store.get_run_status(run_id)
        except KeyError as exc:
            raise PatchError("RUN_NOT_FOUND", "Run does not exist") from exc
        return {"run_id": run_id, "artifacts": manager.store.list_artifacts(run_id)}

    @app.get("/api/v1/artifacts/{artifact_id}")
    @app.get("/v1/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        if manager.store is None:
            raise PatchError("PERSISTENCE_REQUIRED", "artifact state requires durable storage")
        try:
            return manager.store.get_artifact(artifact_id)
        except KeyError as exc:
            raise PatchError("ARTIFACT_NOT_FOUND", "Artifact does not exist") from exc

    @app.get("/api/v1/projects/{project_id}/security")
    @app.get("/v1/projects/{project_id}/security")
    def project_security(project_id: str) -> dict[str, Any]:
        return manager.project_security(project_id)

    @app.post("/api/v1/projects/{project_id}/trust")
    @app.post("/v1/projects/{project_id}/trust")
    def trust_project(project_id: str, payload: TrustProjectRequest) -> dict[str, Any]:
        return manager.trust_project(project_id, payload.confirmation)

    @app.post("/api/v1/change-sets/{change_set_id}/approve")
    @app.post("/v1/change-sets/{change_set_id}/approve")
    def approve(change_set_id: str, payload: ApprovalRequest | None = None) -> dict[str, Any]:
        return manager.approve(change_set_id, payload.capability if payload else APPLY_CAPABILITY)

    @app.post("/api/v1/change-sets/{change_set_id}/approval-requests")
    @app.post("/v1/change-sets/{change_set_id}/approval-requests")
    def request_approval(
        change_set_id: str, payload: ApprovalIntentRequest
    ) -> dict[str, Any]:
        return manager.request_approval(
            change_set_id, payload.capability, payload.user_confirmation
        )

    @app.post("/api/v1/change-sets/{change_set_id}/apply")
    @app.post("/v1/change-sets/{change_set_id}/apply")
    def apply(
        change_set_id: str,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if idempotency_key is None:
            raise PatchError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        return manager.apply(
            change_set_id, _header_token(authorization), idempotency_key=idempotency_key
        )

    @app.post("/api/v1/change-sets/{change_set_id}/verify")
    @app.post("/v1/change-sets/{change_set_id}/verify")
    def verify(
        change_set_id: str,
        payload: VerificationRequest | None = None,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if idempotency_key is None:
            raise PatchError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        return manager.verify(
            change_set_id,
            _header_token(authorization),
            profile=payload.profile if payload else VerificationProfile.PYTHON_SYNTAX,
            idempotency_key=idempotency_key,
        )

    @app.post("/api/v1/change-sets/{change_set_id}/rollback")
    @app.post("/v1/change-sets/{change_set_id}/rollback")
    def rollback(
        change_set_id: str,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if idempotency_key is None:
            raise PatchError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
        return manager.rollback(
            change_set_id, _header_token(authorization), idempotency_key=idempotency_key
        )

    @app.post("/api/v1/change-sets/{change_set_id}/revoke")
    @app.post("/v1/change-sets/{change_set_id}/revoke")
    def revoke(change_set_id: str) -> dict[str, Any]:
        return manager.revoke(change_set_id)

    return app
