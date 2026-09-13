"""Immutable, FC-only artifacts for H8 remote Loop strategies.

The Host owns the runtime Loop.  A strategy is only a pure function that
receives the fixed redacted H8 projection and returns one proposed next action.
It is never imported into nanobot and it cannot receive a transcript, tool
arguments, workspace path, approval token, or other Host capability.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Protocol

from .candidate_runner import (
    CANDIDATE_LOOP_API_VERSION,
    CANDIDATE_TIMEOUT_SECONDS,
    CandidateExecutionResult,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
)

_ARTIFACT_ROOT = "remote-loop-strategies"
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


class _CandidateExecutor(Protocol):
    def trial(self, request: CandidateTrialRequest) -> CandidateExecutionResult: ...

    def execute(self, request: CandidateTrialRequest) -> CandidateExecutionResult: ...


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
        root.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(strategy_id, version, source_hash)
        self._write_immutable(root / "candidate.py", source)
        self._write_immutable(root / "manifest.json", self._encode(manifest))
        return RemoteStrategyArtifact(session_id, strategy_id, version, source_hash, root)

    def load(
        self, session_id: str, strategy_id: str, version: str, source_hash: str
    ) -> RemoteStrategyArtifact:
        """Resolve and verify a state-pinned artifact without loading its code."""
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("remote strategy source hash is invalid")
        root = self._artifact_root(session_id, strategy_id, version, source_hash)
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
        source, manifest = self._read_artifact(artifact)
        self.require_trialled(artifact)
        input_data = self._candidate_projection(projection)
        result = self._runner.execute(
            self._request(
                artifact,
                task_id=task_id,
                project_id=project_id,
                source=source,
                manifest=manifest,
                input_data=input_data,
                execution_mode="live",
            )
        )
        if result.status != "passed" or not isinstance(result.output, Mapping):
            raise ValueError("remote strategy execution did not return a decision")
        return dict(result.output)

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
    ) -> CandidateTrialRequest:
        grant = self._grant_issuer.issue(
            session_id=artifact.session_id,
            task_id=task_id,
            project_id=project_id,
            allowed_input_rules=_PROJECTION_RULES,
            expires_at_epoch=int(time()) + 180,
        )
        return CandidateTrialRequest(
            task_id=task_id,
            project_id=project_id,
            trial_id=f"remote-{execution_mode}-{uuid.uuid4().hex}",
            session_id=artifact.session_id,
            grant=grant,
            manifest=manifest,
            source=source,
            input_data=input_data,
            timeout_seconds=CANDIDATE_TIMEOUT_SECONDS,
            execution_mode=execution_mode,
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
    def _candidate_projection(projection: Mapping[str, object]) -> dict[str, object]:
        expected = {rule[0] for rule in _PROJECTION_RULES}
        if set(projection) - {"allowed_actions", *expected}:
            raise ValueError("remote strategy projection has unknown fields")
        candidate_input = {key: projection.get(key) for key in expected}
        if candidate_input.get("schema") != _PROJECTION_SCHEMA:
            raise ValueError("remote strategy projection schema is invalid")
        return candidate_input

    def _read_artifact(
        self, artifact: RemoteStrategyArtifact
    ) -> tuple[str, dict[str, object]]:
        verified = self.load(
            artifact.session_id,
            artifact.strategy_id,
            artifact.version,
            artifact.source_hash,
        )
        source = (verified.root / "candidate.py").read_text(encoding="utf-8")
        manifest = json.loads((verified.root / "manifest.json").read_text(encoding="utf-8"))
        assert isinstance(manifest, dict)
        return source, manifest

    def _artifact_root(
        self, session_id: str, strategy_id: str, version: str, source_hash: str
    ) -> Path:
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return (
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
