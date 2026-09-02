"""Durable localhost control plane for the explicit P2B approval loop."""

# FastAPI consumes decorated functions through route registration.
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: VerificationProfile = VerificationProfile.PYTHON_SYNTAX


class FeatureDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str = Field(min_length=1)
    patches: list[PatchPayload] = Field(min_length=1, max_length=12)
    evidence_ids: list[str] = Field(min_length=1, max_length=32)
    verification_profile: VerificationProfile = VerificationProfile.PYTHON_SYNTAX


class _PendingChangeSet:
    def __init__(self, plan: PatchPlan, run_id: str | None = None) -> None:
        self.plan = plan
        self.run_id = run_id
        self.receipt: ApplyReceipt | None = None
        self.memory_approvals: dict[str, tuple[str, datetime]] = {}


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
        "project_id": plan.project_id,
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
            resolved_project_id = self.store.ensure_project(str(project))
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
            return plan_payload(self._require(change_set_id))

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

    def approve(self, change_set_id: str, capability: str = APPLY_CAPABILITY) -> dict[str, Any]:
        if capability not in {APPLY_CAPABILITY, ROLLBACK_CAPABILITY, VERIFY_CAPABILITY}:
            raise PatchError("CAPABILITY_INVALID", "unsupported approval capability")
        with self._mutex:
            entry = self._require(change_set_id)
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
                self.store.create_approval(
                    approval_id=approval_id,
                    change_set_id=change_set_id,
                    plan_hash=entry.plan.patch_hash,
                    scope_hash=entry.plan.scope_hash,
                    patch_hash=entry.plan.patch_hash,
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
            if not isinstance(self.execution_backend, FakeExecutionBackend):
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
            entry.plan.change_set.transition_to(ChangeSetStatus.VERIFYING)
            self._persist_status(entry)
            try:
                result = self.execution_backend.run(request)
            except ExecutionError as exc:
                entry.plan.change_set.transition_to(ChangeSetStatus.VERIFICATION_FAILED)
                self._persist_status(entry, failure_reason=exc.code)
                self._event(entry, "changeset.verification_failed", {"code": exc.code})
                if finalize_run:
                    self._finish_run(entry, "failed", {"code": exc.code})
                raise PatchError("VERIFICATION_FAILED", "sandbox verification failed") from exc
            except Exception as exc:
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
            replay = self._check_idempotency(
                idempotency_key, "apply", change_set_id, approval_token
            )
            if replay is not None:
                return replay
            approval = self._consume_approval(entry, approval_token, APPLY_CAPABILITY)
            self._reserve_idempotency(idempotency_key, "apply", change_set_id, approval_token)
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
                self._event(entry, "changeset.apply_failed", {"code": exc.code})
                self._finish_run(entry, "failed", {"code": exc.code})
                raise
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
            replay = self._check_idempotency(
                idempotency_key, "rollback", change_set_id, approval_token
            )
            if replay is not None:
                return replay
            self._consume_approval(entry, approval_token, ROLLBACK_CAPABILITY)
            self._reserve_idempotency(idempotency_key, "rollback", change_set_id, approval_token)
            receipt = entry.receipt or ApplyReceipt(
                change_set_id=entry.plan.change_set.id,
                project_id=entry.plan.project_id,
                project_path=entry.plan.project_path,
                before=entry.plan.records,
                after=entry.plan.records,
                snapshots=entry.plan.snapshots,
            )
            self.patch_service.rollback(receipt, entry.plan)
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
        if (
            raw is None
            or not raw["project_path"]
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
        store.require_schema_head("0005_p4_run_link")
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
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    app.state.manager = manager

    @app.middleware("http")
    async def require_loopback(request: Request, call_next: Any) -> Any:
        client = request.client.host if request.client is not None else None
        if client not in {"127.0.0.1", "::1", "localhost"}:
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "PATH_DENIED", "message": "local control plane only"}},
            )
        return await call_next(request)

    @app.exception_handler(PatchError)
    async def patch_error_handler(_: Request, exc: PatchError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "devpilot-control-plane"}

    @app.post("/v1/change-sets/preview", status_code=status.HTTP_201_CREATED)
    def preview(payload: PreviewRequest) -> dict[str, Any]:
        return manager.preview(
            project_path=payload.project_path,
            project_id=payload.project_id,
            patches=[item.to_file_patch() for item in payload.patches],
        )

    @app.post("/v1/feature-delivery/proposals", status_code=status.HTTP_201_CREATED)
    def feature_delivery_proposal(payload: FeatureDeliveryRequest) -> dict[str, Any]:
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
        return proposal.to_dict()

    @app.get("/v1/change-sets/{change_set_id}")
    def get_change_set(change_set_id: str) -> dict[str, Any]:
        return manager.get(change_set_id)

    @app.post("/v1/change-sets/{change_set_id}/approve")
    def approve(change_set_id: str, payload: ApprovalRequest | None = None) -> dict[str, Any]:
        return manager.approve(change_set_id, payload.capability if payload else APPLY_CAPABILITY)

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

    @app.post("/v1/change-sets/{change_set_id}/revoke")
    def revoke(change_set_id: str) -> dict[str, Any]:
        return manager.revoke(change_set_id)

    return app
