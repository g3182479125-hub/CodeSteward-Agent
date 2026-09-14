"""Immutable, FC-only artifacts for H8 remote Loop strategies.

The Host owns the runtime Loop.  A strategy is only a pure function that
receives the fixed redacted H8 projection and returns one proposed next action.
It is never imported into nanobot and it cannot receive a transcript, tool
arguments, workspace path, approval token, or other Host capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock
from time import time
from typing import Protocol

from .candidate_runner import (
    CANDIDATE_LOOP_API_VERSION,
    CANDIDATE_TIMEOUT_SECONDS,
    CandidateExecutionResult,
    CandidateTrialGrant,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
)

_ARTIFACT_ROOT = "remote-loop-strategies"
_DECISION_JOURNAL = "decisions"
_PROJECTION_SCHEMA = "h8-remote-driver-projection-v1"
_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_PROJECTION_RULES = (
    ("schema", "string", 64, None, None),
    ("task_ref", "string", 64, None, None),
    ("project_ref", "string", 64, None, None),
    ("step", "integer", 16, 0, 1_000_000),
    ("remaining_iterations", "integer", 16, 0, 1_000_000),
    ("completed_result_count", "integer", 16, 0, 1_000_000),
    ("pending_tool_count", "integer", 16, 0, 1_000_000),
)


def h9_fixed_evaluation_projection(fixture: Mapping[str, object]) -> dict[str, object]:
    """Return the exact host projection bound to one H9 Loop fixture."""
    case_name = fixture.get("case")
    cases = {
        "target-1": (1, 7, 0),
        "target-2": (2, 6, 1),
        "holdout-1": (3, 5, 1),
        "holdout-2": (4, 4, 2),
        "regression-1": (5, 3, 2),
        "regression-2": (6, 2, 3),
    }
    if not isinstance(case_name, str) or case_name not in cases:
        raise ValueError("H9 loop fixture is invalid")
    step, remaining_iterations, completed_results = cases[case_name]
    return {
        "schema": _PROJECTION_SCHEMA,
        "task_ref": f"h9-{case_name}",
        "project_ref": f"h9-project-{case_name}",
        "step": step,
        "remaining_iterations": remaining_iterations,
        "completed_result_count": completed_results,
        "pending_tool_count": 0,
    }


class _CandidateExecutor(Protocol):
    def trial(self, request: CandidateTrialRequest) -> CandidateExecutionResult: ...

    def execute(self, request: CandidateTrialRequest) -> CandidateExecutionResult: ...

    def cancel(self, request: CandidateTrialRequest) -> CandidateExecutionResult: ...

    def recover(self, request: CandidateTrialRequest) -> CandidateExecutionResult: ...


@dataclass(frozen=True, slots=True)
class RemoteStrategyArtifact:
    """One immutable strategy version owned by a single session."""

    session_id: str
    strategy_id: str
    version: str
    source_hash: str
    root: Path


class RemoteStrategyCatalog:
    """File-backed artifact catalog using the existing H5 FC candidate runner."""

    def __init__(
        self,
        data_dir: Path,
        *,
        runner: _CandidateExecutor,
        grant_issuer: CandidateTrialGrantIssuer,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self._runner = runner
        self._grant_issuer = grant_issuer
        self._cancel_lock = RLock()
        self._cancellations: dict[str, Event] = {}

    def define(
        self, session_id: str, strategy_id: str, version: str, source: str
    ) -> RemoteStrategyArtifact:
        """Persist an immutable source artifact; defining it never executes code."""
        if not session_id:
            raise ValueError("remote strategy session is required")
        if _ID.fullmatch(strategy_id) is None:
            raise ValueError("remote strategy identifier is invalid")
        if _VERSION.fullmatch(version) is None:
            raise ValueError("remote strategy version is invalid")
        if not isinstance(source, str) or not source:
            raise ValueError("remote strategy source is invalid")
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        root = self._artifact_root(session_id, strategy_id, version, source_hash)
        manifest = self._manifest(strategy_id, version, source_hash)
        manifest_path = root / "manifest.json"
        if manifest_path.is_file() and manifest_path.read_text(encoding="utf-8") != self._encode(
            manifest
        ):
            root = self._collision_artifact_root(
                session_id, strategy_id, version, source_hash
            )
        root.mkdir(parents=True, exist_ok=True)
        self._write_immutable(root / "candidate.py", source)
        self._write_immutable(root / "manifest.json", self._encode(manifest))
        return RemoteStrategyArtifact(session_id, strategy_id, version, source_hash, root)

    def load(
        self, session_id: str, strategy_id: str, version: str, source_hash: str
    ) -> RemoteStrategyArtifact:
        """Resolve and verify a state-pinned artifact without loading its code."""
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("remote strategy source hash is invalid")
        manifest = self._manifest(strategy_id, version, source_hash)
        root = next(
            (
                candidate
                for candidate in (
                    self._artifact_root(session_id, strategy_id, version, source_hash),
                    self._collision_artifact_root(
                        session_id, strategy_id, version, source_hash
                    ),
                    self._legacy_artifact_root(session_id, strategy_id, version, source_hash),
                )
                if self._matches_manifest(candidate, manifest)
            ),
            self._artifact_root(session_id, strategy_id, version, source_hash),
        )
        source_path = root / "candidate.py"
        manifest_path = root / "manifest.json"
        if not source_path.is_file() or not manifest_path.is_file():
            raise ValueError("remote strategy artifact hash is unavailable")
        source = source_path.read_text(encoding="utf-8")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != source_hash:
            raise ValueError("remote strategy artifact hash changed")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("remote strategy manifest is invalid") from exc
        if manifest != self._manifest(strategy_id, version, source_hash):
            raise ValueError("remote strategy manifest changed")
        return RemoteStrategyArtifact(session_id, strategy_id, version, source_hash, root)

    def load_for_scope(
        self,
        session_id: str,
        project_id: str,
        strategy_id: str,
        version: str,
        source_hash: str,
    ) -> RemoteStrategyArtifact:
        """Resolve a session artifact or its explicitly promoted project copy."""
        try:
            return self.load(session_id, strategy_id, version, source_hash)
        except ValueError as session_error:
            root = self._project_artifact_root(project_id, strategy_id, version, source_hash)
            try:
                return self._load_verified_root(
                    session_id, strategy_id, version, source_hash, root
                )
            except ValueError:
                raise session_error from None

    def promote_to_project(
        self, artifact: RemoteStrategyArtifact, *, project_id: str
    ) -> RemoteStrategyArtifact:
        """Copy only a passed immutable artifact into exact project scope."""
        verified = self.load(
            artifact.session_id,
            artifact.strategy_id,
            artifact.version,
            artifact.source_hash,
        )
        self.require_trialled(verified)
        root = self._project_artifact_root(
            project_id, artifact.strategy_id, artifact.version, artifact.source_hash
        )
        root.mkdir(parents=True, exist_ok=True)
        for name in ("candidate.py", "manifest.json", "trial.json"):
            source = verified.root / name
            if not source.is_file():
                raise ValueError("remote strategy promotion receipt is incomplete")
            self._write_immutable(root / name, source.read_text(encoding="utf-8"))
        return RemoteStrategyArtifact(
            artifact.session_id,
            artifact.strategy_id,
            artifact.version,
            artifact.source_hash,
            root,
        )

    def trial(
        self, artifact: RemoteStrategyArtifact, *, task_id: str, project_id: str
    ) -> CandidateExecutionResult:
        """Run the fixed H8 fixture through FC and persist a hash-bound receipt."""
        source, manifest = self._read_artifact(artifact)
        input_data = self._fixed_projection()
        result = self._runner.trial(
            self._request(
                artifact,
                task_id=task_id,
                project_id=project_id,
                source=source,
                manifest=manifest,
                input_data=input_data,
                execution_mode="trial",
            )
        )
        if result.status == "passed" and result.source_hash == artifact.source_hash:
            self._write_immutable(
                artifact.root / "trial.json",
                self._encode(
                    {
                        "source_hash": artifact.source_hash,
                        "input_hash": result.input_hash,
                        "status": "passed",
                    }
                ),
            )
        return result

    def decide(
        self,
        artifact: RemoteStrategyArtifact,
        *,
        session_id: str,
        task_id: str,
        project_id: str,
        projection: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Ask FC for a pure decision after verifying the exact passed artifact."""
        if artifact.session_id != session_id:
            raise ValueError("remote strategy is not visible in this session")
        request = self.decision_request(
            artifact,
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            projection=projection,
        )
        return self.execute_decision(request)

    def decision_request(
        self,
        artifact: RemoteStrategyArtifact,
        *,
        session_id: str,
        task_id: str,
        project_id: str,
        projection: Mapping[str, object],
    ) -> CandidateTrialRequest:
        """Build one cancellable, FC-only request for a redacted decision."""
        if artifact.session_id != session_id:
            raise ValueError("remote strategy is not visible in this session")
        source, manifest = self._read_artifact(artifact)
        self.require_trialled(artifact)
        cancelled = Event()
        request = self._request(
            artifact,
            task_id=task_id,
            project_id=project_id,
            source=source,
            manifest=manifest,
            input_data=self._candidate_projection(projection),
            execution_mode="live",
            cancel_requested=cancelled.is_set,
        )
        with self._cancel_lock:
            self._write_decision_journal(artifact, request, status="prepared")
            self._cancellations[request.trial_id] = cancelled
        return request

    def evaluation_request(
        self,
        artifact: RemoteStrategyArtifact,
        *,
        session_id: str,
        task_id: str,
        project_id: str,
        projection: Mapping[str, object],
        trial_id: str,
    ) -> CandidateTrialRequest:
        """Build one H9 fixed-suite request without creating a live journal."""
        if artifact.session_id != session_id:
            raise ValueError("remote strategy is not visible in this session")
        if not re.fullmatch(r"remote-eval-[0-9a-f]{32}", trial_id):
            raise ValueError("remote strategy evaluation identifier is invalid")
        source, manifest = self._read_artifact(artifact)
        self.require_trialled(artifact)
        return self._request(
            artifact,
            task_id=task_id,
            project_id=project_id,
            source=source,
            manifest=manifest,
            input_data=self._candidate_projection(projection),
            execution_mode="live",
            trial_id=trial_id,
        )

    def execute_decision(self, request: CandidateTrialRequest) -> Mapping[str, object]:
        """Execute a previously built request; the host may cancel the same request."""
        artifact = self._artifact_for_request(request)
        try:
            result = self._runner.execute(request)
        except Exception:
            self._write_decision_journal(artifact, request, status="unknown")
            raise
        finally:
            with self._cancel_lock:
                self._cancellations.pop(request.trial_id, None)
        self._write_decision_journal(artifact, request, status=result.status, result=result)
        if result.status != "passed" or not isinstance(result.output, Mapping):
            raise ValueError("remote strategy execution did not return a decision")
        return dict(result.output)

    def cancel_decision(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        """Forward cancellation through the Candidate runner to FC's run receipt."""
        artifact = self._artifact_for_request(request)
        with self._cancel_lock:
            cancelled = self._cancellations.get(request.trial_id)
            if cancelled is not None:
                cancelled.set()
        try:
            result = self._runner.cancel(request)
            self._write_decision_journal(artifact, request, status=result.status, result=result)
            return result
        except Exception:
            self._write_decision_journal(artifact, request, status="unknown")
            raise
        finally:
            with self._cancel_lock:
                self._cancellations.pop(request.trial_id, None)

    def load_decision_request(
        self,
        session_id: str,
        strategy_id: str,
        version: str,
        source_hash: str,
        trial_id: str,
    ) -> CandidateTrialRequest:
        """Rehydrate one exact remote request after a host process restart."""
        journal_path = self._decision_path(
            RemoteStrategyArtifact(session_id, strategy_id, version, source_hash, self.data_dir),
            trial_id,
        )
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("remote strategy decision journal is unavailable") from exc
        if (
            not isinstance(journal, Mapping)
            or journal.get("schema") != "h8-remote-decision-v1"
            or any(
                journal.get(key) != expected
                for key, expected in (
                    ("session_id", session_id),
                    ("strategy_id", strategy_id),
                    ("version", version),
                    ("source_hash", source_hash),
                    ("trial_id", trial_id),
                )
            )
        ):
            raise ValueError("remote strategy decision journal scope changed")
        project_id = journal.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("remote strategy decision project scope is invalid")
        artifact = self.load_for_scope(
            session_id, project_id, strategy_id, version, source_hash
        )
        raw_input = journal.get("input_data")
        raw_grant = journal.get("grant")
        expected_input_fields = {rule[0] for rule in _PROJECTION_RULES}
        if (
            journal.get("execution_mode") != "live"
            or not isinstance(raw_input, Mapping)
            or set(raw_input) != expected_input_fields
            or not isinstance(raw_grant, Mapping)
        ):
            raise ValueError("remote strategy decision journal is invalid")
        input_data = self._candidate_projection(raw_input)
        try:
            rules = tuple(
                tuple(rule) for rule in raw_grant["allowed_input_rules"]
            )
            grant = CandidateTrialGrant(
                grant_id=str(raw_grant["grant_id"]),
                session_id=str(raw_grant["session_id"]),
                task_id=str(raw_grant["task_id"]),
                project_id=str(raw_grant["project_id"]),
                allowed_input_fields=tuple(str(item) for item in raw_grant["allowed_input_fields"]),
                allowed_input_rules=rules,
                expires_at_epoch=(
                    int(raw_grant["expires_at_epoch"])
                    if raw_grant.get("expires_at_epoch") is not None
                    else None
                ),
                signature="",
            )
            grant = self._grant_issuer.resign(grant)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("remote strategy decision grant is invalid") from exc
        if (
            grant.session_id != session_id
            or grant.task_id != str(journal["task_id"])
            or grant.project_id != str(journal["project_id"])
            or tuple(grant.allowed_input_rules) != _PROJECTION_RULES
            or not self._grant_issuer.verify(grant)
        ):
            raise ValueError("remote strategy decision grant scope changed")
        source, manifest = self._read_artifact(artifact)
        cancelled = Event()
        request = self._request(
            artifact,
            task_id=str(journal["task_id"]),
            project_id=str(journal["project_id"]),
            source=source,
            manifest=manifest,
            input_data=input_data,
            execution_mode="live",
            trial_id=trial_id,
            grant=grant,
            cancel_requested=cancelled.is_set,
        )
        with self._cancel_lock:
            self._cancellations[trial_id] = cancelled
        return request

    def recover_decision(
        self,
        session_id: str,
        strategy_id: str,
        version: str,
        source_hash: str,
        trial_id: str,
    ) -> CandidateExecutionResult:
        """Query the exact persisted candidate operation without replaying it."""
        request = self.load_decision_request(
            session_id, strategy_id, version, source_hash, trial_id
        )
        artifact = self._artifact_for_request(request)
        try:
            result = self._runner.recover(request)
            self._write_decision_journal(artifact, request, status=result.status, result=result)
            return result
        except Exception:
            self._write_decision_journal(artifact, request, status="unknown")
            raise
        finally:
            with self._cancel_lock:
                self._cancellations.pop(trial_id, None)

    def require_trialled(self, artifact: RemoteStrategyArtifact) -> None:
        """Reject selection when no passed FC receipt binds this exact source hash."""
        receipt_path = artifact.root / "trial.json"
        if not receipt_path.is_file():
            raise ValueError("remote strategy requires a successful trial")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("remote strategy trial receipt is invalid") from exc
        if receipt.get("status") != "passed" or receipt.get("source_hash") != artifact.source_hash:
            raise ValueError("remote strategy trial receipt does not match artifact hash")

    def _request(
        self,
        artifact: RemoteStrategyArtifact,
        *,
        task_id: str,
        project_id: str,
        source: str,
        manifest: Mapping[str, object],
        input_data: Mapping[str, object],
        execution_mode: str,
        trial_id: str | None = None,
        grant: CandidateTrialGrant | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> CandidateTrialRequest:
        grant = grant or self._grant_issuer.issue(
            session_id=artifact.session_id,
            task_id=task_id,
            project_id=project_id,
            allowed_input_rules=_PROJECTION_RULES,
            expires_at_epoch=int(time()) + 180,
        )
        return CandidateTrialRequest(
            task_id=task_id,
            project_id=project_id,
            trial_id=trial_id or f"remote-{execution_mode}-{uuid.uuid4().hex}",
            session_id=artifact.session_id,
            grant=grant,
            manifest=manifest,
            source=source,
            input_data=input_data,
            timeout_seconds=CANDIDATE_TIMEOUT_SECONDS,
            execution_mode=execution_mode,
            cancel_requested=cancel_requested,
        )

    def _artifact_for_request(self, request: CandidateTrialRequest) -> RemoteStrategyArtifact:
        strategy_id = request.manifest.get("plugin_id")
        version = request.manifest.get("version")
        if not isinstance(strategy_id, str) or not isinstance(version, str):
            raise ValueError("remote strategy request manifest is invalid")
        return self.load_for_scope(
            request.session_id,
            request.project_id,
            strategy_id,
            version,
            hashlib.sha256(request.source.encode("utf-8")).hexdigest(),
        )

    def _write_decision_journal(
        self,
        artifact: RemoteStrategyArtifact,
        request: CandidateTrialRequest,
        *,
        status: str,
        result: CandidateExecutionResult | None = None,
    ) -> None:
        path = self._decision_path(artifact, request.trial_id)
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("remote strategy decision journal is invalid") from exc
            if not isinstance(current, Mapping) or any(
                current.get(key) != expected
                for key, expected in (
                    ("session_id", request.session_id),
                    ("task_id", request.task_id),
                    ("project_id", request.project_id),
                    ("trial_id", request.trial_id),
                )
            ):
                raise ValueError("remote strategy decision journal scope changed")
            payload = dict(current)
        else:
            grant = request.grant
            if not isinstance(grant, CandidateTrialGrant):
                raise ValueError("remote strategy decision grant is unavailable")
            payload = {
                "schema": "h8-remote-decision-v1",
                "session_id": request.session_id,
                "task_id": request.task_id,
                "project_id": request.project_id,
                "strategy_id": artifact.strategy_id,
                "version": artifact.version,
                "source_hash": artifact.source_hash,
                "trial_id": request.trial_id,
                "execution_mode": request.execution_mode,
                "input_data": dict(request.input_data),
                "grant": {
                    "grant_id": grant.grant_id,
                    "session_id": grant.session_id,
                    "task_id": grant.task_id,
                    "project_id": grant.project_id,
                    "allowed_input_fields": list(grant.allowed_input_fields),
                    "allowed_input_rules": [list(rule) for rule in grant.allowed_input_rules],
                    "expires_at_epoch": grant.expires_at_epoch,
                },
            }
        payload["status"] = status
        if result is not None:
            payload["result"] = result.to_dict()
            payload["operation_id"] = result.operation_id
            payload["run_id"] = result.run_id
        self._atomic_write(path, self._encode(payload))

    @staticmethod
    def _atomic_write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(value, encoding="utf-8", newline="\n")
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _decision_path(self, artifact: RemoteStrategyArtifact, trial_id: str) -> Path:
        if re.fullmatch(r"remote-live-[0-9a-f]{32}", trial_id) is None:
            raise ValueError("remote strategy trial identifier is invalid")
        # Keep the mutable journal outside the deeply nested immutable artifact
        # path. This also avoids Windows MAX_PATH failures for long hashes.
        session_digest = hashlib.sha256(artifact.session_id.encode("utf-8")).hexdigest()[:16]
        return self._filesystem_path(
            self.data_dir
            / _ARTIFACT_ROOT
            / _DECISION_JOURNAL
            / session_digest
            / f"{trial_id}.json"
        )

    @staticmethod
    def _manifest(strategy_id: str, version: str, source_hash: str) -> dict[str, object]:
        return {
            "plugin_id": strategy_id,
            "version": version,
            "content_hash": source_hash,
            "kind": "loop",
            "api_version": CANDIDATE_LOOP_API_VERSION,
            "scope": "session",
            "entry": "candidate.py",
            "requires": [],
            "provides": ["loop:remote-strategy"],
            "requested_capabilities": [],
            "execution_mode": "fc_proxy",
        }

    @staticmethod
    def _fixed_projection() -> dict[str, object]:
        return {
            "schema": _PROJECTION_SCHEMA,
            "task_ref": "trial-task",
            "project_ref": "trial-project",
            "step": 0,
            "remaining_iterations": 3,
            "completed_result_count": 0,
            "pending_tool_count": 0,
        }

    @staticmethod
    def fixed_evaluation_projection(
        fixture: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return one host-owned, case-bound projection for H9 comparisons."""
        projection = RemoteStrategyCatalog._fixed_projection()
        if fixture is None:
            return projection
        return h9_fixed_evaluation_projection(fixture)

    @staticmethod
    def _candidate_projection(projection: Mapping[str, object]) -> dict[str, object]:
        expected = {rule[0] for rule in _PROJECTION_RULES}
        if set(projection) - {"allowed_actions", *expected}:
            raise ValueError("remote strategy projection has unknown fields")
        candidate_input = {key: projection.get(key) for key in expected}
        if candidate_input.get("schema") != _PROJECTION_SCHEMA:
            raise ValueError("remote strategy projection schema is invalid")
        return candidate_input

    def _load_verified_root(
        self,
        session_id: str,
        strategy_id: str,
        version: str,
        source_hash: str,
        root: Path,
    ) -> RemoteStrategyArtifact:
        source_path = root / "candidate.py"
        manifest_path = root / "manifest.json"
        if not source_path.is_file() or not manifest_path.is_file():
            raise ValueError("remote strategy artifact hash is unavailable")
        source = source_path.read_text(encoding="utf-8")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != source_hash:
            raise ValueError("remote strategy artifact hash changed")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("remote strategy manifest is invalid") from exc
        if manifest != self._manifest(strategy_id, version, source_hash):
            raise ValueError("remote strategy manifest changed")
        return RemoteStrategyArtifact(session_id, strategy_id, version, source_hash, root)

    def _read_artifact(
        self, artifact: RemoteStrategyArtifact
    ) -> tuple[str, dict[str, object]]:
        verified = self._load_verified_root(
            artifact.session_id,
            artifact.strategy_id,
            artifact.version,
            artifact.source_hash,
            artifact.root,
        )
        source = (verified.root / "candidate.py").read_text(encoding="utf-8")
        manifest = json.loads((verified.root / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("remote strategy manifest is invalid")
        return source, manifest

    def _artifact_root(
        self, session_id: str, strategy_id: str, version: str, source_hash: str
    ) -> Path:
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        artifact_digest = self._artifact_digest(strategy_id, version, source_hash)
        return self._filesystem_path(
            self.data_dir
            / _ARTIFACT_ROOT
            / "artifacts"
            / session_digest
            / artifact_digest[:24]
        )

    def _project_artifact_root(
        self, project_id: str, strategy_id: str, version: str, source_hash: str
    ) -> Path:
        project_digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
        artifact_digest = self._artifact_digest(strategy_id, version, source_hash)
        return self._filesystem_path(
            self.data_dir
            / _ARTIFACT_ROOT
            / "projects"
            / project_digest
            / artifact_digest[:24]
        )

    def _collision_artifact_root(
        self, session_id: str, strategy_id: str, version: str, source_hash: str
    ) -> Path:
        """Use a full digest only if another artifact occupies the compact key."""
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return self._filesystem_path(
            self.data_dir
            / _ARTIFACT_ROOT
            / "artifacts"
            / session_digest
            / self._artifact_digest(strategy_id, version, source_hash)
        )

    @staticmethod
    def _artifact_digest(strategy_id: str, version: str, source_hash: str) -> str:
        return hashlib.sha256(f"{strategy_id}\0{version}\0{source_hash}".encode()).hexdigest()

    @staticmethod
    def _filesystem_path(path: Path) -> Path:
        """Permit H8's immutable artifacts below deep Windows workspaces.

        The catalog accepts a user-configured data directory, so shortening
        only our child names cannot guarantee a sub-260-character absolute
        path.  Win32's extended-length syntax retains the exact same location
        while allowing pathlib's file operations to create and verify it.
        Other platforms keep their ordinary paths unchanged.
        """
        if os.name != "nt":
            return path
        value = str(path)
        if value.startswith("\\\\?\\"):
            return path
        return Path("\\\\?\\" + value)

    @staticmethod
    def _matches_manifest(root: Path, expected: Mapping[str, object]) -> bool:
        candidate_path = root / "candidate.py"
        manifest_path = root / "manifest.json"
        if not candidate_path.is_file() or not manifest_path.is_file():
            return False
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8")) == expected
        except (OSError, json.JSONDecodeError):
            return False

    def _legacy_artifact_root(
        self, session_id: str, strategy_id: str, version: str, source_hash: str
    ) -> Path:
        """Locate the H8 pre-compaction layout without migrating immutable files."""
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return self._filesystem_path(
            self.data_dir
            / _ARTIFACT_ROOT
            / "session"
            / session_digest
            / strategy_id
            / version
            / source_hash
        )

    @staticmethod
    def _encode(value: Mapping[str, object]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _write_immutable(path: Path, value: str) -> None:
        if path.exists():
            if path.read_text(encoding="utf-8") != value:
                raise ValueError("remote strategy artifact conflicts with immutable version")
            return
        path.write_text(value, encoding="utf-8", newline="\n")
