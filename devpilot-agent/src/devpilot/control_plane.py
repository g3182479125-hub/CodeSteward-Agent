"""Local approval control plane for the P2B nanobot proposal loop."""

# FastAPI registers these nested functions through decorators.
# pyright: reportUnusedFunction=false

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .domain import ChangeSetStatus, FileOperation
from .patches import (
    ApplyReceipt,
    AuthorizationGrant,
    FilePatch,
    PatchError,
    PatchPlan,
    StructuredPatchService,
)
from .paths import AllowedRoots, PathBoundaryError
from .storage import ProjectStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18800
DEFAULT_TOKEN_TTL = timedelta(minutes=5)


class PatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1)
    operation: FileOperation
    content: str | None = None
    expected_sha256: str | None = None

    def to_file_patch(self) -> FilePatch:
        return FilePatch(
            relative_path=self.relative_path,
            operation=self.operation,
            content=self.content,
            expected_sha256=self.expected_sha256,
        )


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str = Field(min_length=1)
    project_id: str | None = Field(default=None, min_length=1)
    patches: list[PatchPayload] = Field(min_length=1)


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_token: str = Field(min_length=1)


@dataclass
class _PendingChangeSet:
    plan: PatchPlan
    run_id: str | None
    approval_digest: str | None = None
    approval_expires_at: datetime | None = None
    receipt: ApplyReceipt | None = None


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
        "files": _file_change_payload(plan),
        "diff": plan.diff,
    }


class ControlPlaneManager:
    """Own active proposals and keep write operations behind operator approval."""

    def __init__(
        self,
        *,
        allowed_roots: AllowedRoots,
        store: ProjectStore | None = None,
        token_ttl: timedelta = DEFAULT_TOKEN_TTL,
    ) -> None:
        self.store = store
        self.token_ttl = token_ttl
        self.patch_service = StructuredPatchService(allowed_roots=allowed_roots)
        self._entries: dict[str, _PendingChangeSet] = {}
        self._mutex = threading.RLock()

    def preview(
        self,
        *,
        project_path: str | Path,
        project_id: str | None,
        patches: list[FilePatch],
    ) -> dict[str, Any]:
        try:
            project = self.patch_service.allowed_roots.require_project(project_path)
        except PathBoundaryError as exc:
            raise PatchError("PROJECT_PATH_REJECTED", "project is outside allowed roots") from exc

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
            patches=patches,
        )
        entry = _PendingChangeSet(plan=plan, run_id=run_id)
        with self._mutex:
            self._entries[plan.change_set.id] = entry
        self._event(entry, "changeset.previewed", {"files": len(plan.records)})
        return plan_payload(entry)

    def get(self, change_set_id: str) -> dict[str, Any]:
        with self._mutex:
            entry = self._require(change_set_id)
            return plan_payload(entry)

    def approve(self, change_set_id: str) -> dict[str, Any]:
        with self._mutex:
            entry = self._require(change_set_id)
            if entry.receipt is not None:
                raise PatchError("CHANGESET_ALREADY_APPLIED", "ChangeSet has already been applied")
            if entry.approval_digest is not None:
                raise PatchError(
                    "APPROVAL_ALREADY_ISSUED", "ChangeSet already has an active approval"
                )
            entry.plan.change_set.transition_to(ChangeSetStatus.APPROVAL_PENDING)
            token = secrets.token_urlsafe(32)
            entry.approval_digest = _digest(token)
            entry.approval_expires_at = _now() + self.token_ttl
            self._event(entry, "changeset.approval_issued", {})
            return {
                "change_set_id": change_set_id,
                "status": entry.plan.change_set.status.value,
                "expires_at": entry.approval_expires_at.isoformat(),
                "approval_token": token,
            }

    def apply(self, change_set_id: str, approval_token: str) -> dict[str, Any]:
        with self._mutex:
            entry = self._require(change_set_id)
            if entry.approval_digest is None or entry.approval_expires_at is None:
                raise PatchError("APPROVAL_REQUIRED", "an active operator approval is required")
            if _now() >= entry.approval_expires_at:
                entry.approval_digest = None
                raise PatchError("APPROVAL_EXPIRED", "operator approval has expired")
            if not secrets.compare_digest(entry.approval_digest, _digest(approval_token)):
                raise PatchError("APPROVAL_INVALID", "operator approval is invalid")

            grant = AuthorizationGrant(
                change_set_id=entry.plan.change_set.id,
                project_id=entry.plan.project_id,
                patch_hash=entry.plan.patch_hash,
                scope_hash=entry.plan.scope_hash,
                capability="r1",
                expires_at=entry.approval_expires_at,
            )
            entry.approval_digest = None
            try:
                receipt = self.patch_service.apply(entry.plan, grant)
            except PatchError as exc:
                self._event(entry, "changeset.apply_failed", {"code": exc.code})
                self._finish_run(entry, "failed", {"code": exc.code})
                raise
            entry.receipt = receipt
            self._event(entry, "changeset.applied", {"files": len(entry.plan.records)})
            self._finish_run(entry, "completed", {"change_set_id": change_set_id})
            return {
                "change_set_id": change_set_id,
                "status": entry.plan.change_set.status.value,
                "patch_hash": entry.plan.patch_hash,
                "scope_hash": entry.plan.scope_hash,
                "files": _file_change_payload(entry.plan),
            }

    def rollback(self, change_set_id: str) -> dict[str, Any]:
        with self._mutex:
            entry = self._require(change_set_id)
            if entry.receipt is None:
                raise PatchError("ROLLBACK_UNAVAILABLE", "ChangeSet has no applied receipt")
            try:
                self.patch_service.rollback(entry.receipt, entry.plan)
            except PatchError as exc:
                self._event(entry, "changeset.rollback_failed", {"code": exc.code})
                raise
            self._event(entry, "changeset.reverted", {})
            return {"change_set_id": change_set_id, "status": entry.plan.change_set.status.value}

    def _require(self, change_set_id: str) -> _PendingChangeSet:
        entry = self._entries.get(change_set_id)
        if entry is None:
            raise PatchError(
                "CHANGESET_NOT_FOUND", "ChangeSet does not exist in this control-plane session"
            )
        return entry

    def _event(self, entry: _PendingChangeSet, event_type: str, payload: dict[str, Any]) -> None:
        if self.store is not None and entry.run_id is not None:
            self.store.append_event(entry.run_id, event_type, payload)

    def _finish_run(self, entry: _PendingChangeSet, result: str, payload: dict[str, Any]) -> None:
        if self.store is not None and entry.run_id is not None:
            self.store.finish_run(entry.run_id, result, payload)


def create_app(
    *,
    manager: ControlPlaneManager | None = None,
    allowed_roots: AllowedRoots | None = None,
    data_dir: str | Path | None = None,
) -> FastAPI:
    """Create the localhost control-plane ASGI application."""

    if manager is None:
        store = ProjectStore(
            (Path(data_dir) if data_dir is not None else _configured_data_dir()) / "devpilot.db"
        )
        store.initialize()
        manager = ControlPlaneManager(
            allowed_roots=allowed_roots or _configured_roots(),
            store=store,
        )

    app = FastAPI(title="DevPilot Local Control Plane", version="0.1.0")
    app.state.manager = manager

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

    @app.get("/v1/change-sets/{change_set_id}")
    def get_change_set(change_set_id: str) -> dict[str, Any]:
        return manager.get(change_set_id)

    @app.post("/v1/change-sets/{change_set_id}/approve")
    def approve(change_set_id: str) -> dict[str, Any]:
        return manager.approve(change_set_id)

    @app.post("/v1/change-sets/{change_set_id}/apply")
    def apply(change_set_id: str, payload: ApplyRequest) -> dict[str, Any]:
        return manager.apply(change_set_id, payload.approval_token)

    @app.post("/v1/change-sets/{change_set_id}/rollback")
    def rollback(change_set_id: str) -> dict[str, Any]:
        return manager.rollback(change_set_id)

    return app
