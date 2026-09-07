"""Bounded evidence and candidate storage for the P8 Skill improvement trial.

This module deliberately stops at a private, reviewable candidate.  It does not
load candidates into nanobot and it does not publish or activate them.  P8-02
owns those actions after an independent comparison and an explicit user
confirmation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redaction import redact_sensitive_payload

TRIAL_SKILL_ID = "bug-fix"
TRIAL_SKILL_RELATIVE_PATH = "skills/bug-fix/SKILL.md"
FEEDBACK_SOURCE_TYPES = frozenset(
    {"user_feedback", "tool_result", "model_inference", "external_content"}
)
FEEDBACK_STATES = frozenset({"verified", "pending_verification", "rejected"})
CANDIDATE_STATES = frozenset(
    {
        "pending_evaluation",
        "evaluation_failed",
        "pending_confirmation",
        "active",
        "ignored",
        "stale",
    }
)
_MAX_FEEDBACK_TEXT = 4_000
_MAX_EVIDENCE_REFS = 20
_MAX_CANDIDATES_PER_BATCH = 3


class SkillImprovementError(ValueError):
    """Raised when feedback or a Skill candidate violates the P8 contract."""


def content_sha256(content: str | bytes) -> str:
    """Return the hash of the exact UTF-8/byte content used by the contract."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_text(value: object, *, field: str, limit: int = _MAX_FEEDBACK_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillImprovementError(f"{field} must be a non-empty string")
    sanitized = redact_sensitive_payload(value.strip())
    if not isinstance(sanitized, str):  # pragma: no cover - defensive typing
        raise SkillImprovementError(f"{field} must be text")
    return sanitized[:limit]


def _refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item not in result:
            result.append(item.strip()[:128])
    return tuple(result[:_MAX_EVIDENCE_REFS])


def _json_safe(value: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = redact_sensitive_payload(dict(value))
    if not isinstance(sanitized, dict):  # pragma: no cover - defensive typing
        raise SkillImprovementError("record must be a JSON object")
    return sanitized


@dataclass(frozen=True)
class FeedbackEvidence:
    evidence_id: str
    task_id: str
    project_id: str
    source_type: str
    occurred_at: str
    skill_id: str
    skill_hash: str
    correction: str
    actual_result: str
    evidence_refs: tuple[str, ...]
    processing_state: str
    batch_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "source_type": self.source_type,
            "occurred_at": self.occurred_at,
            "skill_id": self.skill_id,
            "skill_hash": self.skill_hash,
            "correction": self.correction,
            "actual_result": self.actual_result,
            "evidence_refs": list(self.evidence_refs),
            "processing_state": self.processing_state,
            "batch_id": self.batch_id,
        }


@dataclass(frozen=True)
class SkillCandidate:
    candidate_id: str
    skill_id: str
    project_id: str
    skill_path: str
    base_version_hash: str
    candidate_content: str
    candidate_hash: str
    evidence_ids: tuple[str, ...]
    reason: str
    expected_behavior: str
    test_set_version: str
    status: str
    budget: dict[str, Any]
    result_refs: tuple[str, ...]
    created_at: str
    batch_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "skill_id": self.skill_id,
            "project_id": self.project_id,
            "skill_path": self.skill_path,
            "base_version_hash": self.base_version_hash,
            "candidate_content": self.candidate_content,
            "candidate_hash": self.candidate_hash,
            "evidence_ids": list(self.evidence_ids),
            "reason": self.reason,
            "expected_behavior": self.expected_behavior,
            "test_set_version": self.test_set_version,
            "status": self.status,
            "budget": dict(self.budget),
            "result_refs": list(self.result_refs),
            "created_at": self.created_at,
            "batch_id": self.batch_id,
        }


class SkillImprovementStore:
    """Private, atomic JSON storage for P8 evidence and inactive candidates."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).expanduser().resolve() / "skill-improvement"
        self.feedback_file = self.root / "feedback.jsonl"
        self.candidates_dir = self.root / "candidates"
        self.root.mkdir(parents=True, exist_ok=True)
        self.candidates_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_environment(cls, workspace: str | Path | None = None) -> SkillImprovementStore:
        default = Path(workspace or ".") / ".nanobot-data"
        return cls(os.environ.get("DEVPILOT_DATA_DIR", str(default)))

    @property
    def storage_root(self) -> Path:
        """Return the private root; it is intentionally not a SkillsLoader root."""
        return self.root

    def record_feedback(
        self,
        *,
        task_id: str,
        project_id: str,
        source_type: str,
        skill_id: str,
        skill_hash: str,
        correction: str,
        actual_result: str,
        evidence_refs: object = (),
        batch_id: str | None = None,
    ) -> FeedbackEvidence:
        if source_type not in FEEDBACK_SOURCE_TYPES:
            raise SkillImprovementError("unsupported feedback source type")
        if skill_id != TRIAL_SKILL_ID:
            raise SkillImprovementError("P8 trial only accepts the bug-fix Skill")
        if len(skill_hash) != 64 or any(c not in "0123456789abcdef" for c in skill_hash.lower()):
            raise SkillImprovementError("skill_hash must be a SHA-256 digest")
        refs = _refs(evidence_refs)
        if source_type == "model_inference" and len(refs) < 2:
            raise SkillImprovementError(
                "model inference requires two independent evidence references"
            )
        state = (
            "verified"
            if source_type in {"user_feedback", "tool_result"}
            else "pending_verification"
        )
        evidence = FeedbackEvidence(
            evidence_id=f"feedback-{uuid.uuid4()}",
            task_id=_bounded_text(task_id, field="task_id", limit=128),
            project_id=_bounded_text(project_id, field="project_id", limit=128),
            source_type=source_type,
            occurred_at=_now(),
            skill_id=skill_id,
            skill_hash=skill_hash.lower(),
            correction=_bounded_text(correction, field="correction"),
            actual_result=_bounded_text(actual_result, field="actual_result"),
            evidence_refs=refs,
            processing_state=state,
            batch_id=batch_id,
        )
        self._append_jsonl(self.feedback_file, evidence.to_dict())
        return evidence

    def verify_feedback(
        self, evidence_id: str, *, independent_refs: object = ()
    ) -> FeedbackEvidence:
        evidence = self.get_feedback(evidence_id)
        if evidence is None:
            raise SkillImprovementError("feedback evidence not found")
        refs = tuple(dict.fromkeys((*evidence.evidence_refs, *_refs(independent_refs))))
        if evidence.source_type in {"model_inference", "external_content"} and len(refs) < 2:
            raise SkillImprovementError("independent verification evidence is required")
        updated = FeedbackEvidence(
            **{
                **evidence.to_dict(),
                "evidence_refs": refs,
                "processing_state": "verified",
            }
        )
        self._append_jsonl(self.feedback_file, updated.to_dict())
        return updated

    def get_feedback(self, evidence_id: str) -> FeedbackEvidence | None:
        latest: dict[str, Any] | None = None
        if not self.feedback_file.exists():
            return None
        for record in self._read_jsonl(self.feedback_file):
            if record.get("evidence_id") == evidence_id:
                latest = record
        if latest is None:
            return None
        return FeedbackEvidence(
            evidence_id=str(latest["evidence_id"]),
            task_id=str(latest["task_id"]),
            project_id=str(latest["project_id"]),
            source_type=str(latest["source_type"]),
            occurred_at=str(latest["occurred_at"]),
            skill_id=str(latest["skill_id"]),
            skill_hash=str(latest["skill_hash"]),
            correction=str(latest["correction"]),
            actual_result=str(latest["actual_result"]),
            evidence_refs=_refs(latest.get("evidence_refs")),
            processing_state=str(latest["processing_state"]),
            batch_id=latest.get("batch_id") if isinstance(latest.get("batch_id"), str) else None,
        )

    def create_candidate(
        self,
        *,
        project_id: str,
        skill_path: str,
        baseline_content: str,
        candidate_content: str,
        evidence_ids: object,
        reason: str,
        expected_behavior: str,
        test_set_version: str,
        batch_id: str | None = None,
        budget: Mapping[str, Any] | None = None,
    ) -> SkillCandidate:
        if skill_path.replace("\\", "/") != TRIAL_SKILL_RELATIVE_PATH:
            raise SkillImprovementError("candidate path must be the bug-fix Skill")
        ids = _refs(evidence_ids)
        if not ids:
            raise SkillImprovementError("at least one verified evidence ID is required")
        evidence = [self.get_feedback(item) for item in ids]
        if any(item is None or item.processing_state != "verified" for item in evidence):
            raise SkillImprovementError("all candidate evidence must be verified")
        projects = {item.project_id for item in evidence if item is not None}
        if projects != {project_id}:
            raise SkillImprovementError("candidate evidence must belong to one project")
        if content_sha256(baseline_content) == content_sha256(candidate_content):
            raise SkillImprovementError("candidate content must differ from its baseline")
        if _frontmatter(candidate_content) != _frontmatter(baseline_content):
            raise SkillImprovementError(
                "candidate may change Skill guidance, not protected metadata"
            )
        if redact_sensitive_payload(candidate_content) != candidate_content:
            raise SkillImprovementError("candidate contains sensitive material")
        max_chars = max(2_000, len(baseline_content))
        if len(candidate_content) > max_chars:
            raise SkillImprovementError("candidate content exceeds the bounded Skill size")
        current_batch = batch_id or f"batch-{uuid.uuid4()}"
        if len(self.list_candidates(batch_id=current_batch)) >= _MAX_CANDIDATES_PER_BATCH:
            raise SkillImprovementError("candidate batch limit is three")
        candidate = SkillCandidate(
            candidate_id=f"candidate-{uuid.uuid4()}",
            skill_id=TRIAL_SKILL_ID,
            project_id=_bounded_text(project_id, field="project_id", limit=128),
            skill_path=TRIAL_SKILL_RELATIVE_PATH,
            base_version_hash=content_sha256(baseline_content),
            candidate_content=candidate_content,
            candidate_hash=content_sha256(candidate_content),
            evidence_ids=ids,
            reason=_bounded_text(reason, field="reason"),
            expected_behavior=_bounded_text(expected_behavior, field="expected_behavior"),
            test_set_version=_bounded_text(test_set_version, field="test_set_version", limit=128),
            status="pending_evaluation",
            budget=_json_safe(budget or {"max_model_calls": 0, "max_tokens": 0}),
            result_refs=(),
            created_at=_now(),
            batch_id=current_batch,
        )
        self._atomic_write(
            self.candidates_dir / f"{candidate.candidate_id}.json", candidate.to_dict()
        )
        return candidate

    def get_candidate(self, candidate_id: str) -> SkillCandidate | None:
        path = self.candidates_dir / f"{candidate_id}.json"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SkillImprovementError("candidate record is unreadable") from exc
        if not isinstance(raw, dict):
            raise SkillImprovementError("candidate record is invalid")
        return SkillCandidate(
            candidate_id=str(raw["candidate_id"]),
            skill_id=str(raw["skill_id"]),
            project_id=str(raw["project_id"]),
            skill_path=str(raw["skill_path"]),
            base_version_hash=str(raw["base_version_hash"]),
            candidate_content=str(raw["candidate_content"]),
            candidate_hash=str(raw["candidate_hash"]),
            evidence_ids=_refs(raw.get("evidence_ids")),
            reason=str(raw["reason"]),
            expected_behavior=str(raw["expected_behavior"]),
            test_set_version=str(raw["test_set_version"]),
            status=str(raw["status"]),
            budget=dict(raw.get("budget") or {}),
            result_refs=_refs(raw.get("result_refs")),
            created_at=str(raw["created_at"]),
            batch_id=str(raw["batch_id"]),
        )

    def list_candidates(self, *, batch_id: str | None = None) -> list[SkillCandidate]:
        candidates: list[SkillCandidate] = []
        for path in sorted(self.candidates_dir.glob("candidate-*.json")):
            candidate_id = path.stem
            candidate = self.get_candidate(candidate_id)
            if candidate is not None and (batch_id is None or candidate.batch_id == batch_id):
                candidates.append(candidate)
        return candidates

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SkillImprovementError("feedback storage is unreadable") from exc
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError as exc:
                raise SkillImprovementError("feedback storage contains invalid JSON") from exc
            if isinstance(value, dict):
                result.append(value)
        return result

    @staticmethod
    def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(
                json.dumps(_json_safe(value), ensure_ascii=False, indent=2, sort_keys=True)
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _frontmatter(content: str) -> str:
    """Return exact metadata so candidate content cannot alter its trust contract."""
    if not content.startswith("---\n"):
        raise SkillImprovementError("Skill frontmatter is required")
    end = content.find("\n---", 4)
    if end < 0:
        raise SkillImprovementError("Skill frontmatter is malformed")
    return content[: end + 4]
