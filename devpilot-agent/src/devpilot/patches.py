"""Controlled structured file patches for the P2A delivery boundary.

This module deliberately has no CLI, FastAPI route, or nanobot Tool entry point.
The only write path exposed here requires a test-only authorization grant so the
P2A implementation can be exercised without opening a production write port.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from .domain import ChangeSet, ChangeSetStatus, FileChange, FileOperation
from .paths import AllowedRoots
from .storage import ProjectStore

MAX_CHANGESET_FILES = 12
MAX_PATCH_BYTES = 80 * 1024
MAX_PATCH_LINES = 1_200
MAX_FILE_BYTES = 512 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PARTS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "dist", "build", ".venv", "__pycache__"}
)
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_SECRET_NAME = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|"
    r"(?:.*[._-])?(?:secret|credential|password|api[_-]?key|access[_-]?token|auth[_-]?token)"
    r"(?:[._-].*)?)$"
)
_SECRET_SUFFIX = re.compile(r"(?i)\.(?:pem|key|crt|cer|p12|pfx|jks)$")


class PatchError(ValueError):
    """Stable, user-safe error for a rejected patch operation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class FilePatch:
    """One complete text-file replacement described by a relative path."""

    relative_path: str
    operation: FileOperation
    content: str | None = None
    expected_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    """Scoped operator authorization; never exposed to the model or Tool API."""

    __test__ = False

    change_set_id: str
    project_id: str
    patch_hash: str
    scope_hash: str
    capability: str = "r1"
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


# Backward-compatible fixture name retained for the P2A test suite.
TestAuthorizationGrant = AuthorizationGrant


@dataclass(frozen=True, slots=True)
class PatchFileRecord:
    relative_path: str
    operation: FileOperation
    expected_sha256: str | None
    content_hash: str
    before_size: int
    after_sha256: str | None


@dataclass(frozen=True, slots=True)
class PatchPlan:
    project_id: str
    project_path: Path
    patches: tuple[FilePatch, ...]
    change_set: ChangeSet
    scope_hash: str
    patch_hash: str
    diff: str
    records: tuple[PatchFileRecord, ...]
    snapshots: tuple[_Snapshot, ...] = field(repr=False)
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Snapshot:
    relative_path: str
    existed: bool
    content: bytes | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class ApplyReceipt:
    change_set_id: str
    project_id: str
    project_path: Path
    before: tuple[PatchFileRecord, ...]
    after: tuple[PatchFileRecord, ...]
    snapshots: tuple[_Snapshot, ...] = field(repr=False)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative_parts(raw_path: str) -> tuple[str, ...]:
    if not raw_path or "\x00" in raw_path:
        raise PatchError("PATH_INVALID", "relative_path is empty or contains NUL")
    posix = PurePosixPath(raw_path.replace("\\", "/"))
    windows = PureWindowsPath(raw_path)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise PatchError("PATH_NOT_RELATIVE", "absolute paths are not accepted")
    parts = tuple(part for part in posix.parts if part not in {"."})
    if not parts or ".." in parts:
        raise PatchError("PATH_OUTSIDE_PROJECT", "path traversal is not accepted")
    for part in parts:
        if part in {"", "."}:
            raise PatchError("PATH_INVALID", "path contains an empty component")
        if part != part.rstrip(" .") or ":" in part:
            raise PatchError(
                "WINDOWS_PATH_ALIAS", "trailing dots, spaces, and NTFS streams are not accepted"
            )
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
            raise PatchError("WINDOWS_PATH_ALIAS", "reserved Windows device names are not accepted")
    return parts


def _forbidden_path(parts: tuple[str, ...]) -> bool:
    for part in parts:
        if part.casefold() in _FORBIDDEN_PARTS:
            return True
        if _SECRET_NAME.fullmatch(part) or _SECRET_SUFFIX.search(part):
            return True
    return False


def _read_text_bytes(path: Path) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise PatchError("FILE_READ_FAILED", "unable to read the target file") from exc
    if b"\x00" in content:
        raise PatchError("BINARY_FILE_FORBIDDEN", "binary files are not supported")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError("BINARY_FILE_FORBIDDEN", "non-UTF-8 files are not supported") from exc
    if len(content) > MAX_FILE_BYTES:
        raise PatchError("FILE_LIMIT_EXCEEDED", "file exceeds the 512 KiB limit")
    return content


def _canonical_patch(patch: FilePatch) -> dict[str, str | None]:
    return {
        "relative_path": patch.relative_path,
        "operation": FileOperation(patch.operation).value,
        "expected_sha256": patch.expected_sha256,
        "content": patch.content,
    }


class StructuredPatchService:
    """Plan and apply bounded text replacements under an allowed project root."""

    def __init__(
        self,
        *,
        allowed_roots: AllowedRoots,
        store: ProjectStore | None = None,
        replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    ) -> None:
        self.allowed_roots = allowed_roots
        self.store = store
        self._replace = replace
        self._consumed_grants: set[str] = set()
        self._locked_projects: set[Path] = set()
        self._mutex = threading.RLock()
        if self.store is not None:
            for project_id, change_set_id in self.store.list_applying_change_sets():
                self.store.lock_project(
                    project_id, f"uncertain recovery for ChangeSet {change_set_id}"
                )

    def plan(
        self,
        project_path: str | Path,
        *,
        project_id: str = "project-test",
        task_id: str = "task-test",
        plan_id: str = "plan-test",
        run_id: str | None = None,
        patches: Iterable[FilePatch],
    ) -> PatchPlan:
        project = self.allowed_roots.require_project(project_path)
        patch_list = tuple(patches)
        if not patch_list:
            raise PatchError("PATCH_EMPTY", "at least one file patch is required")
        if len(patch_list) > MAX_CHANGESET_FILES:
            raise PatchError("PATCH_LIMIT_EXCEEDED", "a ChangeSet may contain at most 12 files")

        records: list[PatchFileRecord] = []
        snapshots: list[_Snapshot] = []
        seen: set[str] = set()
        diff_parts: list[str] = []
        patch_bytes = 0
        patch_lines = 0

        for patch in patch_list:
            try:
                operation = FileOperation(patch.operation)
            except ValueError as exc:
                raise PatchError(
                    "PATCH_OPERATION_INVALID", "operation must be create, update, or delete"
                ) from exc
            parts = _safe_relative_parts(patch.relative_path)
            relative_path = "/".join(parts)
            identity = relative_path.casefold()
            if identity in seen:
                raise PatchError("PATCH_DUPLICATE_PATH", "a path may occur only once")
            if any(
                identity.startswith(f"{existing}/") or existing.startswith(f"{identity}/")
                for existing in seen
            ):
                raise PatchError(
                    "PATCH_OVERLAPPING_PATH",
                    "a ChangeSet cannot patch both a path and one of its descendants",
                )
            seen.add(identity)
            if _forbidden_path(parts):
                raise PatchError("FORBIDDEN_FILE", "sensitive or generated paths are not writable")

            target = self._target(project, parts)
            exists = target.exists()
            if exists and target.is_dir():
                raise PatchError("TARGET_NOT_FILE", "target path is a directory")
            if target.is_symlink():
                raise PatchError("SYMLINK_FORBIDDEN", "symbolic-link targets are not writable")

            before = _read_text_bytes(target) if exists else None
            before_hash = sha256_bytes(before) if before is not None else None
            before_size = len(before) if before is not None else 0
            expected = patch.expected_sha256
            if expected is not None and not _SHA256.fullmatch(expected):
                raise PatchError("HASH_INVALID", "expected_sha256 must be a lowercase SHA-256")

            if operation is FileOperation.CREATE:
                if exists:
                    raise PatchError("TARGET_EXISTS", "create requires a missing target")
                if expected is not None:
                    raise PatchError("HASH_INVALID", "create expected_sha256 must be null")
            elif operation is FileOperation.UPDATE:
                self._require_expected_hash(expected, before_hash)
            elif operation is FileOperation.DELETE:
                self._require_expected_hash(expected, before_hash)
                if patch.content is not None:
                    raise PatchError("PATCH_CONTENT_INVALID", "delete cannot contain content")

            after = (
                None if operation is FileOperation.DELETE else self._encode_content(patch.content)
            )
            if operation is not FileOperation.DELETE and after is None:
                raise PatchError("PATCH_CONTENT_REQUIRED", "create and update require content")
            after_hash = sha256_bytes(after) if after is not None else None
            content_hash = sha256_bytes(after or b"")
            patch_bytes += len(after or b"")
            patch_lines += self._changed_lines(before or b"", after or b"")
            if patch_bytes > MAX_PATCH_BYTES or patch_lines > MAX_PATCH_LINES:
                raise PatchError(
                    "PATCH_LIMIT_EXCEEDED", "ChangeSet patch size or line limit exceeded"
                )

            records.append(
                PatchFileRecord(
                    relative_path=relative_path,
                    operation=operation,
                    expected_sha256=expected,
                    content_hash=content_hash,
                    before_size=before_size,
                    after_sha256=after_hash,
                )
            )
            snapshots.append(
                _Snapshot(
                    relative_path=relative_path,
                    existed=before is not None,
                    content=before,
                    sha256=before_hash,
                )
            )
            diff_parts.append(self._diff(relative_path, before or b"", after or b""))

        canonical = [
            _canonical_patch(patch) | {"relative_path": record.relative_path}
            for patch, record in zip(patch_list, records, strict=True)
        ]
        patch_hash = sha256_bytes(
            json.dumps(
                canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
        scope_hash = sha256_bytes(json.dumps(sorted(seen), separators=(",", ":")).encode())
        file_changes = tuple(
            FileChange(
                relative_path=record.relative_path,
                operation=record.operation,
                content_hash=record.content_hash,
                expected_sha256=record.expected_sha256,
                before_size=record.before_size,
                after_sha256=record.after_sha256,
            )
            for record in records
        )
        change_set = ChangeSet(
            task_id=task_id,
            plan_id=plan_id,
            patch_hash=patch_hash,
            file_changes=file_changes,
            before_manifest=tuple(
                f"{item.relative_path}:{item.expected_sha256 or ''}" for item in records
            ),
            after_manifest=tuple(
                f"{item.relative_path}:{item.after_sha256 or ''}" for item in records
            ),
            rollback_manifest=tuple(
                f"{item.relative_path}:{item.expected_sha256 or ''}" for item in records
            ),
        )
        plan = PatchPlan(
            project_id=project_id,
            project_path=project,
            patches=tuple(
                FilePatch(
                    relative_path=record.relative_path,
                    operation=record.operation,
                    content=patch.content,
                    expected_sha256=record.expected_sha256,
                )
                for patch, record in zip(patch_list, records, strict=True)
            ),
            change_set=change_set,
            scope_hash=scope_hash,
            patch_hash=patch_hash,
            diff="".join(diff_parts),
            records=tuple(records),
            snapshots=tuple(snapshots),
            run_id=run_id,
        )
        self._persist_plan(plan)
        return plan

    def apply(self, plan: PatchPlan, grant: AuthorizationGrant | None) -> ApplyReceipt:
        with self._mutex:
            project = plan.project_path
            if self.store is not None and self.store.is_project_locked(plan.project_id):
                raise PatchError("PROJECT_LOCKED", "project is locked after an uncertain failure")
            if project in self._locked_projects:
                raise PatchError("PROJECT_LOCKED", "project is locked after an uncertain failure")
            self._consume_grant(plan, grant)
            if plan.change_set.status not in {
                ChangeSetStatus.PLANNED,
                ChangeSetStatus.APPROVAL_PENDING,
            }:
                raise PatchError(
                    "CHANGESET_NOT_APPLICABLE", "ChangeSet is not awaiting application"
                )
            if plan.change_set.status is ChangeSetStatus.PLANNED:
                plan.change_set.transition_to(ChangeSetStatus.APPROVAL_PENDING)
            plan.change_set.transition_to(ChangeSetStatus.APPROVED)
            plan.change_set.transition_to(ChangeSetStatus.APPLYING)
            self._persist_status(plan, ChangeSetStatus.APPLYING)
            try:
                snapshots = self._recheck_preconditions(plan)
            except PatchError:
                plan.change_set.transition_to(ChangeSetStatus.APPLY_FAILED)
                self._persist_status(plan, ChangeSetStatus.APPLY_FAILED)
                raise
            temp_root = Path(tempfile.mkdtemp(prefix=".devpilot-patch-", dir=project.parent))
            applied: list[_Snapshot] = []
            try:
                self._stage(plan, temp_root)
                for patch in plan.patches:
                    target = self._target(project, _safe_relative_parts(patch.relative_path))
                    if patch.operation is FileOperation.DELETE:
                        target.unlink()
                    else:
                        staged = temp_root / patch.relative_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        self._replace(staged, target)
                    applied.append(
                        next(
                            item for item in snapshots if item.relative_path == patch.relative_path
                        )
                    )
                plan.change_set.transition_to(ChangeSetStatus.APPLIED)
                self._persist_status(plan, ChangeSetStatus.APPLIED)
                receipt = ApplyReceipt(
                    change_set_id=plan.change_set.id,
                    project_id=plan.project_id,
                    project_path=project,
                    before=plan.records,
                    after=plan.records,
                    snapshots=tuple(snapshots),
                )
                return receipt
            except PatchError as exc:
                if plan.change_set.status is ChangeSetStatus.APPLYING:
                    plan.change_set.transition_to(ChangeSetStatus.APPLY_FAILED)
                self._persist_status(plan, ChangeSetStatus.APPLY_FAILED, exc.code)
                raise
            except (OSError, RuntimeError) as exc:
                try:
                    self._restore(project, tuple(applied), plan)
                except PatchError as rollback_error:
                    if plan.change_set.status is ChangeSetStatus.APPLYING:
                        plan.change_set.transition_to(ChangeSetStatus.APPLY_FAILED)
                    self._locked_projects.add(project)
                    if self.store is not None:
                        self.store.lock_project(
                            plan.project_id, "rollback failed after apply error"
                        )
                    raise rollback_error from exc
                plan.change_set.transition_to(ChangeSetStatus.APPLY_FAILED)
                self._persist_status(plan, ChangeSetStatus.APPLY_FAILED, "APPLY_FAILED")
                raise PatchError(
                    "APPLY_FAILED", "patch application failed and was rolled back"
                ) from exc
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)

    def rollback(self, receipt: ApplyReceipt, plan: PatchPlan) -> None:
        with self._mutex:
            if receipt.change_set_id != plan.change_set.id or receipt.project_id != plan.project_id:
                raise PatchError("ROLLBACK_SCOPE_MISMATCH", "receipt does not match the ChangeSet")
            self._restore(receipt.project_path, receipt.snapshots, plan)
            plan.change_set.transition_to(ChangeSetStatus.REVERTED)
            self._persist_status(plan, ChangeSetStatus.REVERTED)

    def _persist_plan(self, plan: PatchPlan) -> None:
        if self.store is None:
            return
        patches_json = json.dumps(
            [
                {
                    "relative_path": patch.relative_path,
                    "operation": patch.operation.value,
                    "content": patch.content,
                    "expected_sha256": patch.expected_sha256,
                }
                for patch in plan.patches
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        snapshots_json = json.dumps(
            [
                {
                    "relative_path": item.relative_path,
                    "existed": item.existed,
                    "content": item.content.decode("utf-8") if item.content is not None else None,
                    "sha256": item.sha256,
                }
                for item in plan.snapshots
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.store.persist_patch_plan(
            change_set_id=plan.change_set.id,
            task_id=plan.change_set.task_id,
            plan_id=plan.change_set.plan_id,
            project_id=plan.project_id,
            project_path=str(plan.project_path),
            patch_hash=plan.patch_hash,
            scope_hash=plan.scope_hash,
            status=plan.change_set.status.value,
            before_manifest=json.dumps(plan.change_set.before_manifest),
            after_manifest=json.dumps(plan.change_set.after_manifest),
            rollback_manifest=json.dumps(plan.change_set.rollback_manifest),
            patches_json=patches_json,
            snapshots_json=snapshots_json,
            file_changes=[
                {
                    "relative_path": item.relative_path,
                    "operation": item.operation.value,
                    "expected_sha256": item.expected_sha256,
                    "content_hash": item.content_hash,
                    "before_size": item.before_size,
                    "after_sha256": item.after_sha256,
                }
                for item in plan.records
            ],
            run_id=plan.run_id,
        )

    def _persist_status(
        self, plan: PatchPlan, status: ChangeSetStatus, failure_reason: str | None = None
    ) -> None:
        if self.store is not None:
            self.store.update_change_set_status(
                plan.change_set.id, status.value, failure_reason=failure_reason
            )

    def _target(self, project: Path, parts: tuple[str, ...]) -> Path:
        current = project
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise PatchError(
                    "SYMLINK_FORBIDDEN", "symbolic-link path components are not writable"
                )
        target = project.joinpath(*parts)
        resolved = target.resolve(strict=False)
        try:
            resolved.relative_to(project)
        except ValueError as exc:
            raise PatchError("PATH_OUTSIDE_PROJECT", "resolved path escapes the project") from exc
        return target

    @staticmethod
    def _encode_content(content: str | None) -> bytes | None:
        if content is None:
            return None
        encoded = content.encode("utf-8")
        if b"\x00" in encoded:
            raise PatchError("BINARY_FILE_FORBIDDEN", "binary content is not supported")
        if len(encoded) > MAX_FILE_BYTES:
            raise PatchError("FILE_LIMIT_EXCEEDED", "file exceeds the 512 KiB limit")
        return encoded

    @staticmethod
    def _require_expected_hash(expected: str | None, actual: str | None) -> None:
        if expected is None:
            raise PatchError("PRECONDITION_REQUIRED", "update and delete require expected_sha256")
        if actual is None or expected != actual:
            raise PatchError("PRECONDITION_HASH_MISMATCH", "target changed since planning")

    @staticmethod
    def _changed_lines(before: bytes, after: bytes) -> int:
        before_lines = before.decode("utf-8").splitlines()
        after_lines = after.decode("utf-8").splitlines()
        diff = difflib.ndiff(before_lines, after_lines)
        return sum(1 for line in diff if line.startswith("+ ") or line.startswith("- "))

    @staticmethod
    def _diff(relative_path: str, before: bytes, after: bytes) -> str:
        old = before.decode("utf-8").splitlines(keepends=True)
        new = after.decode("utf-8").splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                old, new, fromfile=f"a/{relative_path}", tofile=f"b/{relative_path}"
            )
        )

    def _consume_grant(self, plan: PatchPlan, grant: AuthorizationGrant | None) -> None:
        if grant is None:
            raise PatchError("AUTHORIZATION_REQUIRED", "a test authorization grant is required")
        if grant.id in self._consumed_grants:
            raise PatchError("AUTHORIZATION_REPLAYED", "authorization grant was already consumed")
        if grant.capability != "r1":
            raise PatchError("AUTHORIZATION_CAPABILITY_INVALID", "grant is not an R1 grant")
        expires_at = grant.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise PatchError("AUTHORIZATION_EXPIRED", "authorization grant has expired")
        if (
            grant.change_set_id != plan.change_set.id
            or grant.project_id != plan.project_id
            or grant.patch_hash != plan.patch_hash
            or grant.scope_hash != plan.scope_hash
        ):
            raise PatchError(
                "AUTHORIZATION_SCOPE_MISMATCH", "grant does not match the planned ChangeSet"
            )
        self._consumed_grants.add(grant.id)

    def _recheck_preconditions(self, plan: PatchPlan) -> tuple[_Snapshot, ...]:
        snapshots: list[_Snapshot] = []
        for patch, record in zip(plan.patches, plan.records, strict=True):
            target = self._target(plan.project_path, _safe_relative_parts(patch.relative_path))
            exists = target.exists()
            before = _read_text_bytes(target) if exists else None
            actual = sha256_bytes(before) if before is not None else None
            if patch.operation is FileOperation.CREATE and exists:
                raise PatchError(
                    "PRECONDITION_HASH_MISMATCH", "create target appeared after planning"
                )
            if patch.operation is not FileOperation.CREATE and actual != record.expected_sha256:
                raise PatchError("PRECONDITION_HASH_MISMATCH", "target changed since planning")
            snapshots.append(_Snapshot(record.relative_path, before is not None, before, actual))
        return tuple(snapshots)

    @staticmethod
    def _stage(plan: PatchPlan, temp_root: Path) -> None:
        for patch in plan.patches:
            if patch.operation is FileOperation.DELETE:
                continue
            content = StructuredPatchService._encode_content(patch.content)
            if content is None:
                raise PatchError("PATCH_CONTENT_REQUIRED", "staged patch content is missing")
            staged = temp_root / patch.relative_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)

    def _restore(self, project: Path, snapshots: tuple[_Snapshot, ...], plan: PatchPlan) -> None:
        for snapshot in snapshots:
            target = self._target(project, _safe_relative_parts(snapshot.relative_path))
            current = _read_text_bytes(target) if target.exists() else None
            expected_after = next(
                record.after_sha256
                for record in plan.records
                if record.relative_path == snapshot.relative_path
            )
            current_hash = sha256_bytes(current) if current is not None else None
            if current_hash != expected_after:
                raise PatchError(
                    "ROLLBACK_CONFLICT", "target changed after apply; rollback stopped"
                )
        temp_root = Path(tempfile.mkdtemp(prefix=".devpilot-rollback-", dir=project.parent))
        try:
            for snapshot in snapshots:
                target = self._target(project, _safe_relative_parts(snapshot.relative_path))
                if snapshot.content is None:
                    if target.exists():
                        target.unlink()
                else:
                    staged = temp_root / snapshot.relative_path
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    staged.write_bytes(snapshot.content)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._replace(staged, target)
        except (OSError, RuntimeError) as exc:
            raise PatchError("ROLLBACK_FAILED", "rollback could not restore all files") from exc
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
