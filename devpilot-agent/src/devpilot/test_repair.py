"""Approval-gated repair workflow for saved Python and Node test failures."""

# The shared workflow module intentionally keeps these compatibility helpers private.
# This module is the sibling workflow adapter that consumes those stable internals.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError

from .artifacts import ArtifactService, ArtifactWrite
from .control_plane import ControlPlaneManager
from .execution import FakeExecutionBackend
from .patches import FilePatch, PatchError
from .paths import PathBoundaryError
from .storage import ProjectStore
from .user_intent import confirmation_matches
from .verification import VerificationProfile
from .workflows import (
    _NON_DELIVERY_VERIFY_CODES,
    _STOP_CONDITIONS,
    BugFixWorkflow,
    WorkflowError,
    _freeze,
    _jsonable,
)  # pyright: ignore[reportPrivateUsage]

MAX_REPAIR_ATTEMPTS = 3
_SUPPORTED_PROFILES = frozenset({VerificationProfile.PYTHON_RUN, VerificationProfile.NODE_SYNTAX})
# Test-repair proposals may include an explicitly listed regression test even
# when the saved failure was observed in production code.  This keeps the
# repair workflow usable for strengthening the reproducer without turning the
# whole trusted project into an unbounded write scope.
_TEST_SUPPORT_PREFIX = "tests/"
_TEST_SUPPORT_FILES = frozenset({"pytest.ini"})


@dataclass(frozen=True, slots=True)
class TestRepairProposal:
    """Immutable caller-facing handle for one exact repair attempt."""

    change_set_id: str
    project_id: str
    run_id: str | None
    parent_change_set_id: str | None
    root_change_set_id: str
    attempt: int
    max_attempts: int
    patch_hash: str
    scope_hash: str
    verification_profile: VerificationProfile
    analysis_evidence_ids: tuple[str, ...]
    failure_evidence_ids: tuple[str, ...]
    failure_evidence_hash: str
    failure_fingerprints: tuple[str, ...]
    symptom: str
    expected_behavior: str
    non_goals: tuple[str, ...]
    allowed_files: tuple[str, ...]
    preview: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": "test-repair",
            "change_set_id": self.change_set_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "parent_change_set_id": self.parent_change_set_id,
            "root_change_set_id": self.root_change_set_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            # The confirmation tuple binds plan_hash to the immutable patch
            # hash for this workflow, just as the preview contract does.
            "plan_hash": self.patch_hash,
            "patch_hash": self.patch_hash,
            "scope_hash": self.scope_hash,
            "verification_profile": self.verification_profile.value,
            "analysis_evidence_ids": list(self.analysis_evidence_ids),
            "failure_evidence_ids": list(self.failure_evidence_ids),
            "failure_evidence_hash": self.failure_evidence_hash,
            "failure_fingerprints": list(self.failure_fingerprints),
            "symptom": self.symptom,
            "expected_behavior": self.expected_behavior,
            "non_goals": list(self.non_goals),
            "allowed_files": list(self.allowed_files),
            "status": "awaiting_user_approval",
            "requires_user_confirmation": True,
            "preview": _jsonable(self.preview),
        }


@dataclass(frozen=True, slots=True)
class TestRepairResult:
    """Bounded result of one explicitly approved repair attempt."""

    change_set_id: str
    status: str
    apply: dict[str, Any]
    verification: dict[str, Any]
    artifact: ArtifactWrite

    def to_dict(self) -> dict[str, Any]:
        result = {
            "workflow": "test-repair",
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
        if "failure_fingerprint" in self.verification:
            result["failure_fingerprint"] = self.verification["failure_fingerprint"]
        return result


class TestRepairWorkflow:
    """Turn saved test-check evidence into a bounded repair attempt chain."""

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
                "test repair requires a server-configured Artifact root",
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
    ) -> TestRepairProposal:
        cls(manager, artifacts_root=artifacts_root)
        preview = manager.get(change_set_id)
        raw = preview.get("test_repair_contract")
        if not isinstance(raw, Mapping):
            raise WorkflowError(
                "TEST_REPAIR_NOT_FOUND", "ChangeSet is not a persisted test-repair proposal"
            )
        contract = cast(Mapping[str, Any], raw)
        if contract.get("workflow") != "test-repair":
            raise WorkflowError(
                "TEST_REPAIR_NOT_FOUND", "ChangeSet is not a persisted test-repair proposal"
            )
        try:
            profile = VerificationProfile(str(contract["verification_profile"]))
            if profile not in _SUPPORTED_PROFILES:
                raise ValueError("unsupported profile")
            return TestRepairProposal(
                change_set_id=str(preview["change_set_id"]),
                project_id=str(preview["project_id"]),
                run_id=str(preview["run_id"]) if preview.get("run_id") else None,
                parent_change_set_id=(
                    str(contract["parent_change_set_id"])
                    if contract.get("parent_change_set_id")
                    else None
                ),
                root_change_set_id=str(contract["root_change_set_id"]),
                attempt=int(contract["attempt"]),
                max_attempts=int(contract["max_attempts"]),
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
                failure_fingerprints=tuple(
                    str(item) for item in cast(list[Any], contract["failure_fingerprints"])
                ),
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
                "TEST_REPAIR_PERSISTENCE_INVALID", "test-repair contract is incomplete"
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
        verification_profile: VerificationProfile = VerificationProfile.PYTHON_RUN,
    ) -> TestRepairProposal:
        return self._propose(
            project_path=project_path,
            patches=patches,
            analysis_evidence_ids=analysis_evidence_ids,
            failure_evidence_ids=failure_evidence_ids,
            symptom=symptom,
            expected_behavior=expected_behavior,
            non_goals=non_goals,
            allowed_files=allowed_files,
            verification_profile=verification_profile,
            parent_change_set_id=None,
            root_change_set_id=None,
            attempt=1,
            failure_fingerprints=(),
        )

    def next_attempt(
        self,
        previous: TestRepairProposal,
        *,
        patches: Iterable[FilePatch],
        failure_evidence_ids: Iterable[str],
        failure_fingerprint: str,
        analysis_evidence_ids: Iterable[str] | None = None,
    ) -> TestRepairProposal:
        if previous.attempt >= MAX_REPAIR_ATTEMPTS:
            raise WorkflowError(
                "TEST_REPAIR_BUDGET_EXCEEDED",
                "test repair cannot create a fourth attempt",
            )
        fingerprint = str(failure_fingerprint).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise WorkflowError(
                "TEST_REPAIR_FAILURE_FINGERPRINT_INVALID",
                "failure fingerprint must be a SHA-256 value",
            )
        event_fingerprint = self._last_failure_fingerprint(previous)
        if event_fingerprint != fingerprint:
            raise WorkflowError(
                "TEST_REPAIR_FAILURE_FINGERPRINT_INVALID",
                "failure fingerprint is not bound to the previous verification result",
            )
        if fingerprint in previous.failure_fingerprints:
            raise WorkflowError(
                "TEST_REPAIR_REPEAT_FAILURE",
                "the same verification failure repeated; repair has stopped",
            )
        return self._propose(
            project_path=self._project_path(previous),
            patches=patches,
            analysis_evidence_ids=analysis_evidence_ids or previous.analysis_evidence_ids,
            failure_evidence_ids=failure_evidence_ids,
            symptom=previous.symptom,
            expected_behavior=previous.expected_behavior,
            non_goals=previous.non_goals,
            allowed_files=previous.allowed_files,
            verification_profile=previous.verification_profile,
            parent_change_set_id=previous.change_set_id,
            root_change_set_id=previous.root_change_set_id,
            attempt=previous.attempt + 1,
            failure_fingerprints=(*previous.failure_fingerprints, fingerprint),
        )

    def apply(
        self,
        proposal: TestRepairProposal,
        *,
        user_confirmation: str,
        approval_token: str,
    ) -> dict[str, Any]:
        if not confirmation_matches(user_confirmation, "apply"):
            raise WorkflowError(
                "USER_CONFIRMATION_REQUIRED",
                "the exact test-repair proposal requires explicit user confirmation",
            )
        if not approval_token:
            raise WorkflowError("APPROVAL_TOKEN_REQUIRED", "an external Apply approval is required")
        return self.manager.apply(
            proposal.change_set_id,
            approval_token,
            idempotency_key=f"test-repair-apply-{proposal.change_set_id}",
        )

    def verify(
        self,
        proposal: TestRepairProposal,
        *,
        approval_token: str,
        apply_result: dict[str, Any] | None = None,
    ) -> TestRepairResult:
        if not approval_token:
            raise WorkflowError(
                "APPROVAL_TOKEN_REQUIRED", "an external Verification approval is required"
            )
        store = self.manager.store
        if store is None or proposal.run_id is None:
            raise WorkflowError(
                "WORKFLOW_ARTIFACT_UNAVAILABLE",
                "test repair requires durable store and run-backed Artifact output",
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
            self._append_failure(
                proposal,
                store,
                "EXECUTION_BACKEND_UNAVAILABLE",
                failure_artifact,
                verification={"status": "execution_blocked"},
            )
            raise WorkflowError(
                "AUTONOMOUS_REPAIR_BLOCKED",
                "real execution remains blocked until the P6 Sandbox Gate passes",
            )
        try:
            verification = self.manager.verify(
                proposal.change_set_id,
                approval_token,
                profile=proposal.verification_profile,
                idempotency_key=f"test-repair-verify-{proposal.change_set_id}",
                finalize_run=False,
            )
        except PatchError as exc:
            if exc.code in _NON_DELIVERY_VERIFY_CODES:
                raise
            failure = {
                "status": "verification_failed",
                "profile": proposal.verification_profile.value,
                "backend": "not-run",
                "exit_code": None,
                "error_code": exc.code,
            }
            failure_artifact = self._record_failure_artifact(proposal, failure, store)
            self._append_failure(proposal, store, exc.code, failure_artifact, verification=failure)
            self.manager.finalize_run(proposal.change_set_id, "failed", {"code": exc.code})
            raise

        successful = verification.get("exit_code") == 0
        result_verification = dict(verification)
        if not successful:
            fingerprint = self._failure_fingerprint(verification)
            result_verification["failure_fingerprint"] = fingerprint
        try:
            artifact = self._write_result_artifact(
                proposal,
                apply_result or {"change_set_id": proposal.change_set_id, "status": "applied"},
                result_verification,
                successful,
                store,
            )
        except WorkflowError as exc:
            failure_artifact = {"artifact_id": None, "status": "artifact_write_failed"}
            self._append_failure(
                proposal,
                store,
                exc.code,
                failure_artifact,
                verification=result_verification,
            )
            self.manager.finalize_run(
                proposal.change_set_id,
                "failed",
                {"change_set_id": proposal.change_set_id, "code": exc.code},
            )
            raise
        event_type = "test_repair.completed" if successful else "test_repair.failed"
        payload: dict[str, Any] = {
            "workflow": "test-repair",
            "change_set_id": proposal.change_set_id,
            "status": verification.get("status", "unknown"),
            "exit_code": verification.get("exit_code"),
            "artifact_id": artifact[0],
            "artifact": artifact[1],
            "verification_profile": proposal.verification_profile.value,
            "backend": verification.get("backend", "unknown"),
            "failure_fingerprint": result_verification.get("failure_fingerprint"),
        }
        payload.update(self._bound_hashes(proposal))
        store.append_event(proposal.run_id, event_type, payload)
        self.manager.finalize_run(
            proposal.change_set_id,
            "completed" if successful else "failed",
            {
                "change_set_id": proposal.change_set_id,
                "status": verification.get("status"),
                "artifact_id": artifact[0],
            },
        )
        return TestRepairResult(
            change_set_id=proposal.change_set_id,
            status=str(verification.get("status", "unknown")),
            apply=apply_result or {"change_set_id": proposal.change_set_id, "status": "applied"},
            verification=result_verification,
            artifact=artifact[2],
        )

    def _propose(
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
        verification_profile: VerificationProfile,
        parent_change_set_id: str | None,
        root_change_set_id: str | None,
        attempt: int,
        failure_fingerprints: tuple[str, ...],
    ) -> TestRepairProposal:
        store = self.manager.store
        if store is None:
            raise WorkflowError(
                "WORKFLOW_STORE_REQUIRED", "test repair requires a durable ProjectStore"
            )
        if attempt < 1 or attempt > MAX_REPAIR_ATTEMPTS:
            raise WorkflowError(
                "TEST_REPAIR_BUDGET_EXCEEDED",
                "test repair attempt is outside the three-round budget",
            )
        try:
            profile = VerificationProfile(verification_profile)
        except ValueError as exc:
            raise WorkflowError(
                "VERIFICATION_PROFILE_UNSUPPORTED",
                "verification profile is not allowlisted for test repair",
            ) from exc
        if profile not in _SUPPORTED_PROFILES:
            raise WorkflowError(
                "VERIFICATION_PROFILE_UNSUPPORTED",
                "test repair only supports Python run and Node syntax profiles",
            )
        analysis_ids = self._identifiers(analysis_evidence_ids, "analysis evidence")
        failure_ids = self._identifiers(failure_evidence_ids, "failure evidence")
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
        analysis_facts = BugFixWorkflow._analysis_facts(  # pyright: ignore[reportPrivateUsage]
            store, project_id, analysis_ids
        )
        failure_facts, target_files, before_hashes = self._test_failure_facts(
            store, project_id, failure_ids, clean_symptom, clean_expected
        )
        patch_values = tuple(patches)
        if not patch_values:
            raise WorkflowError("TEST_REPAIR_INPUT_INVALID", "at least one patch is required")
        allowed_keys = {self._path_key(item) for item in clean_allowed}
        target_keys = {self._path_key(item) for item in target_files}
        current_hashes = dict(before_hashes)
        for relative_path in clean_allowed:
            key = self._path_key(relative_path)
            if key in current_hashes or not self._is_test_support_path(relative_path):
                continue
            candidate = project / relative_path
            if candidate.is_file():
                current_hashes[key] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        seen: set[str] = set()
        for patch in patch_values:
            path = self._path(patch.relative_path, "patch path")
            key = self._path_key(path)
            if key in seen or key not in allowed_keys or (
                key not in target_keys and not self._is_test_support_path(path)
            ):
                raise WorkflowError(
                    "TEST_REPAIR_SCOPE_INVALID",
                    "every patch must be unique and within saved failure scope or tests/",
                )
            seen.add(key)
            if patch.operation.value in {
                "update",
                "delete",
            } and patch.expected_sha256 != current_hashes.get(key):
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                    "patch expected_sha256 must match the saved or current file hash",
                )
        preview = self.manager.preview(
            project_path=project, project_id=None, patches=list(patch_values)
        )
        run_id = self.manager.get_run_id(str(preview["change_set_id"]))
        evidence_hash = self._hash({"analysis": analysis_facts, "failure": failure_facts})
        stop_conditions = [
            *_STOP_CONDITIONS,
            "test_repair_attempt_budget",
            "repeated_failure",
            "failure_evidence_changed",
        ]
        contract_payload = {
            "plan_version": 1,
            "workflow": "test-repair",
            "patch_hash": preview["patch_hash"],
            "scope_hash": preview["scope_hash"],
            "evidence_hash": evidence_hash,
            "failure_evidence_hash": self._hash(failure_facts),
            "attempt": attempt,
            "max_attempts": MAX_REPAIR_ATTEMPTS,
            "parent_change_set_id": parent_change_set_id,
            "root_change_set_id": root_change_set_id or str(preview["change_set_id"]),
            "failure_fingerprints": list(failure_fingerprints),
            "symptom": clean_symptom,
            "expected_behavior": clean_expected,
            "non_goals": list(clean_non_goals),
            "allowed_files": list(clean_allowed),
            "check_ids": [profile.value],
            "stop_conditions": stop_conditions,
        }
        contract_hash = self._hash(contract_payload)
        store.update_change_plan_contract(
            str(preview["plan_id"]),
            check_ids=[profile.value],
            stop_conditions=stop_conditions,
            evidence_hash=evidence_hash,
            contract_hash=contract_hash,
        )
        payload = {
            "workflow": "test-repair",
            "change_set_id": preview["change_set_id"],
            "parent_change_set_id": parent_change_set_id,
            "root_change_set_id": root_change_set_id or str(preview["change_set_id"]),
            "attempt": attempt,
            "max_attempts": MAX_REPAIR_ATTEMPTS,
            "analysis_evidence_ids": list(analysis_ids),
            "failure_evidence_ids": list(failure_ids),
            "analysis_evidence_hash": self._hash(analysis_facts),
            "failure_evidence_hash": self._hash(failure_facts),
            "evidence_hash": evidence_hash,
            "failure_fingerprints": list(failure_fingerprints),
            "symptom": clean_symptom,
            "expected_behavior": clean_expected,
            "non_goals": list(clean_non_goals),
            "allowed_files": list(clean_allowed),
            "verification_profile": profile.value,
            "check_ids": [profile.value],
            "stop_conditions": stop_conditions,
            "contract_hash": contract_hash,
        }
        if run_id:
            store.append_event(run_id, "test_repair.proposed", payload)
        preview.update({**payload, "failure_evidence_hash": self._hash(failure_facts)})
        return TestRepairProposal(
            change_set_id=str(preview["change_set_id"]),
            project_id=str(preview["project_id"]),
            run_id=run_id,
            parent_change_set_id=parent_change_set_id,
            root_change_set_id=root_change_set_id or str(preview["change_set_id"]),
            attempt=attempt,
            max_attempts=MAX_REPAIR_ATTEMPTS,
            patch_hash=str(preview["patch_hash"]),
            scope_hash=str(preview["scope_hash"]),
            verification_profile=profile,
            analysis_evidence_ids=analysis_ids,
            failure_evidence_ids=failure_ids,
            failure_evidence_hash=self._hash(failure_facts),
            failure_fingerprints=failure_fingerprints,
            symptom=clean_symptom,
            expected_behavior=clean_expected,
            non_goals=clean_non_goals,
            allowed_files=clean_allowed,
            preview=cast(Mapping[str, Any], _freeze(preview)),
        )

    @staticmethod
    def _project_path(proposal: TestRepairProposal) -> Path:
        raw = proposal.preview.get("project_path")
        if not isinstance(raw, str) or not raw:
            raise WorkflowError(
                "TEST_REPAIR_PERSISTENCE_INVALID", "proposal project path is missing"
            )
        return Path(raw)

    def _test_failure_facts(
        self,
        store: ProjectStore,
        project_id: str,
        evidence_ids: tuple[str, ...],
        symptom: str,
        expected: str,
    ) -> tuple[list[dict[str, Any]], tuple[str, ...], dict[str, str]]:
        facts: list[dict[str, Any]] = []
        targets: set[str] = set()
        hashes: dict[str, str] = {}
        for evidence_id in evidence_ids:
            try:
                valid = (
                    store.run_belongs_to_project(project_id=project_id, run_id=evidence_id)
                    and store.get_run_kind(evidence_id) == "test-check"
                )
                status = store.get_run_status(evidence_id)
            except KeyError as exc:
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID", "failure evidence Run does not exist"
                ) from exc
            if not valid or status != "failed":
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                    "failure evidence must be a failed test-check Run",
                )
            failures = [
                item
                for item in store.list_run_events(evidence_id)
                if item["event_type"] == "test_check.failed"
            ]
            if len(failures) != 1 or not isinstance(failures[0].get("payload"), dict):
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                    "failure evidence must contain exactly one test_check.failed fact",
                )
            payload = cast(dict[str, Any], failures[0]["payload"])
            if (
                payload.get("producer") != "devpilot.test-check"
                or payload.get("schema_version") != 1
                or payload.get("project_id") != project_id
                or payload.get("status") != "failed"
            ):
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                    "test failure fact provenance, project or status is invalid",
                )
            if payload.get("symptom") != symptom or payload.get("expected_behavior") != expected:
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                    "proposal text must match saved test failure",
                )
            raw_targets = payload.get("target_files")
            raw_hashes = payload.get("before_hashes")
            if not isinstance(raw_targets, list) or not isinstance(raw_hashes, dict):
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                    "test failure fact must contain target_files and before_hashes",
                )
            normalized_targets = tuple(
                self._path_key(self._path(str(item), "target file"))
                for item in cast(list[Any], raw_targets)
            )
            if not normalized_targets or len(set(normalized_targets)) != len(normalized_targets):
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID", "test failure targets must be unique"
                )
            normalized_hashes: dict[str, str] = {}
            for raw_path, raw_hash in cast(dict[Any, Any], raw_hashes).items():
                path = self._path_key(self._path(str(raw_path), "before hash path"))
                if not re.fullmatch(r"[0-9a-f]{64}", str(raw_hash)):
                    raise WorkflowError(
                        "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                        "test failure before hashes are invalid",
                    )
                normalized_hashes[path] = str(raw_hash)
            if any(path not in normalized_hashes for path in normalized_targets):
                raise WorkflowError(
                    "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                    "every test failure target needs a before hash",
                )
            for path, value in normalized_hashes.items():
                if path in hashes and hashes[path] != value:
                    raise WorkflowError(
                        "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
                        "test failure evidence contains conflicting hashes",
                    )
                hashes[path] = value
            targets.update(normalized_targets)
            facts.append(
                {
                    "run_id": evidence_id,
                    "event": {"event_type": "test_check.failed", "payload": payload},
                }
            )
        return facts, tuple(sorted(targets)), hashes

    def _last_failure_fingerprint(self, proposal: TestRepairProposal) -> str | None:
        if proposal.run_id is None or self.manager.store is None:
            return None
        events = [
            item
            for item in self.manager.store.list_run_events(proposal.run_id)
            if item["event_type"] == "test_repair.failed"
        ]
        if not events:
            return None
        value = events[-1]["payload"].get("failure_fingerprint")
        return str(value) if value else None

    def _write_result_artifact(
        self,
        proposal: TestRepairProposal,
        applied: dict[str, Any],
        verification: dict[str, Any],
        successful: bool,
        store: ProjectStore,
    ) -> tuple[str, dict[str, Any], ArtifactWrite]:
        artifact_id = str(uuid.uuid4())
        try:
            report = self._render_report(proposal, applied, verification, successful, artifact_id)
            write = ArtifactService(self.artifacts_root).write_markdown(
                project_id=proposal.project_id,
                run_id=cast(str, proposal.run_id),
                name="test-repair.md" if successful else "test-repair-failure.md",
                content=report,
            )
            relative = write.path.relative_to(self.artifacts_root).as_posix()
            kind = "test-repair" if successful else "test-repair-failure"
            store.record_artifact(
                artifact_id=artifact_id,
                project_id=proposal.project_id,
                run_id=cast(str, proposal.run_id),
                relative_path=relative,
                sha256=write.sha256,
                size_bytes=write.size_bytes,
                kind=kind,
            )
        except (OSError, ValueError, SQLAlchemyError) as exc:
            raise WorkflowError(
                "WORKFLOW_ARTIFACT_WRITE_FAILED", "test-repair Artifact could not be recorded"
            ) from exc
        return (
            artifact_id,
            {
                "artifact_id": artifact_id,
                "path": relative,
                "sha256": write.sha256,
                "size_bytes": write.size_bytes,
                "indexed": True,
            },
            write,
        )

    def _record_failure_artifact(
        self, proposal: TestRepairProposal, failure: dict[str, Any], store: ProjectStore
    ) -> dict[str, Any]:
        try:
            artifact_id, info, _ = self._write_result_artifact(
                proposal, {"status": "not-applied"}, failure, False, store
            )
            return {**info, "artifact_id": artifact_id}
        except WorkflowError:
            return {"artifact_id": None, "status": "artifact_write_failed"}

    def _append_failure(
        self,
        proposal: TestRepairProposal,
        store: ProjectStore,
        code: str,
        artifact: dict[str, Any],
        *,
        verification: dict[str, Any],
    ) -> None:
        if proposal.run_id is None:
            return
        payload = {
            "code": code,
            "workflow": "test-repair",
            "artifact": artifact,
            "verification_profile": proposal.verification_profile.value,
            "backend": verification.get("backend", "unknown"),
            "failure_fingerprint": verification.get("failure_fingerprint"),
        }
        payload.update(self._bound_hashes(proposal))
        try:
            store.append_event(proposal.run_id, "test_repair.failed", payload)
        except (OSError, ValueError, SQLAlchemyError):
            pass

    @staticmethod
    def _render_report(
        proposal: TestRepairProposal,
        applied: dict[str, Any],
        verification: dict[str, Any],
        successful: bool,
        artifact_id: str,
    ) -> str:
        files = cast(list[object], proposal.preview.get("files", []))
        lines: list[str] = []
        for raw in files:
            if isinstance(raw, Mapping):
                item = cast(Mapping[str, Any], raw)
                relative_path = item.get("relative_path", "<unknown>")
                operation = item.get("operation", "unknown")
                before_hash = item.get("expected_sha256") or "<none>"
                after_hash = item.get("after_sha256") or "<none>"
                lines.append(
                    f"- `{relative_path}` ({operation}); before=`{before_hash}`; "
                    f"after=`{after_hash}`"
                )
        verification_profile = verification.get("profile", proposal.verification_profile.value)
        return "\n".join(
            [
                "# Test-repair delivery report",
                "",
                "- Workflow: `test-repair`",
                f"- ChangeSet: `{proposal.change_set_id}`",
                f"- Parent ChangeSet: `{proposal.parent_change_set_id or '<none>'}`",
                f"- Attempt: `{proposal.attempt}/{proposal.max_attempts}`",
                f"- Project: `{proposal.project_id}`",
                f"- Patch hash: `{proposal.patch_hash}`",
                f"- Scope hash: `{proposal.scope_hash}`",
                f"- Failure evidence hash: `{proposal.failure_evidence_hash}`",
                f"- Artifact ID: `{artifact_id}`",
                f"- Apply status: `{applied.get('status', 'unknown')}`",
                f"- Verification status: `{verification.get('status', 'unknown')}`",
                f"- Delivery result: `{'success' if successful else 'failed'}`",
                f"- Verification profile: `{verification_profile}`",
                f"- Verification backend: `{verification.get('backend', 'unknown')}`",
                f"- Exit code: `{verification.get('exit_code', 'unknown')}`",
                f"- Failure fingerprint: `{verification.get('failure_fingerprint', '<none>')}`",
                "",
                "## Changed files",
                *(lines or ["- 未提供文件元数据"]),
                "",
                "- stdout/stderr omitted by policy.",
                "",
            ]
        )

    @staticmethod
    def _bound_hashes(proposal: TestRepairProposal) -> dict[str, Any]:
        before = [str(item) for item in proposal.preview.get("before_manifest", [])]
        after = [str(item) for item in proposal.preview.get("after_manifest", [])]

        def digest(value: list[str]) -> str:
            return hashlib.sha256(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()

        return {
            "before_manifest": before,
            "after_manifest": after,
            "before_manifest_hash": digest(before),
            "after_manifest_hash": digest(after),
        }

    @staticmethod
    def _failure_fingerprint(verification: Mapping[str, Any]) -> str:
        return TestRepairWorkflow._hash(
            {
                "profile": verification.get("profile"),
                "status": verification.get("status"),
                "backend": verification.get("backend"),
                "exit_code": verification.get("exit_code"),
                "stdout": verification.get("stdout", ""),
                "stderr": verification.get("stderr", ""),
            }
        )

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    @staticmethod
    def _identifiers(values: Iterable[str], label: str) -> tuple[str, ...]:
        result = tuple(str(item).strip() for item in values if str(item).strip())
        if not result or len(set(result)) != len(result) or any(len(item) > 128 for item in result):
            raise WorkflowError(
                "TEST_REPAIR_INPUT_INVALID", f"{label} ids must be unique and non-empty"
            )
        return result

    @staticmethod
    def _text(value: str, field: str) -> str:
        result = str(value).strip()
        if not result or len(result) > 4000:
            raise WorkflowError("TEST_REPAIR_INPUT_INVALID", f"{field} must be 1-4000 characters")
        return result

    @classmethod
    def _texts(cls, values: Iterable[str], field: str) -> tuple[str, ...]:
        result = tuple(cls._text(str(item), field) for item in values)
        if len(result) > 32 or len(set(result)) != len(result):
            raise WorkflowError("TEST_REPAIR_INPUT_INVALID", f"{field} must contain unique values")
        return result

    @staticmethod
    def _path(value: str, field: str) -> str:
        result = str(value).replace("\\", "/").strip()
        parts = tuple(item for item in result.split("/") if item)
        if (
            not result
            or result.startswith("/")
            or any(item in {".", ".."} or ":" in item for item in parts)
        ):
            raise WorkflowError(
                "TEST_REPAIR_SCOPE_INVALID", f"{field} must be a safe relative path"
            )
        return "/".join(parts)

    @staticmethod
    def _path_key(value: str) -> str:
        return value.casefold() if os.name == "nt" else value

    @staticmethod
    def _is_test_support_path(value: str) -> bool:
        normalized = value.replace("\\", "/").casefold()
        return normalized.startswith(_TEST_SUPPORT_PREFIX) or normalized in _TEST_SUPPORT_FILES

    @classmethod
    def _paths(cls, values: Iterable[str], field: str) -> tuple[str, ...]:
        result = tuple(cls._path(str(item), field) for item in values)
        if (
            not result
            or len(result) > 32
            or len({cls._path_key(item) for item in result}) != len(result)
        ):
            raise WorkflowError("TEST_REPAIR_SCOPE_INVALID", f"{field} must contain unique paths")
        return result
