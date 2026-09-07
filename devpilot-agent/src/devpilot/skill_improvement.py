"""Bounded evidence and candidate storage for the P8 Skill improvement trial.

This module deliberately stops at a private, reviewable candidate.  It does not
load candidates into nanobot and it does not publish or activate them.  P8-02
owns those actions after an independent comparison and an explicit user
confirmation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
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
EVALUATION_CATEGORIES = ("original_failure", "nearby_holdout", "regression")
EVALUATION_VERSIONS = ("baseline", "candidate")
EVALUATION_REPEATS = 3
EVALUATION_TEST_SET_VERSION = "p8-02-v1"
_CONFIRMATION_TTL_SECONDS = 300
_EVIDENCE_REF_RE = re.compile(r"^(run|tool|trace|session|test)-[A-Za-z0-9_.:-]{1,127}$")


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
    workspace: str
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
            "workspace": self.workspace,
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


@dataclass(frozen=True)
class SkillSnapshot:
    """Immutable Skill content bound to one task or activation operation."""

    skill_id: str
    skill_path: str
    content: str
    content_hash: str
    captured_at: str
    task_id: str | None = None
    session_id: str | None = None
    project_id: str | None = None
    workspace: str | None = None


@dataclass(frozen=True)
class EvaluationReport:
    """Fixed, trusted comparison output for one candidate."""

    evaluation_id: str
    candidate_id: str
    test_set_version: str
    profile: str
    template: str
    model_id: str
    parameters: dict[str, Any]
    start_file_hash: str
    budget: dict[str, Any]
    cases: tuple[dict[str, Any], ...]
    baseline_successes: int
    candidate_successes: int
    eligible: bool
    status: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "candidate_id": self.candidate_id,
            "test_set_version": self.test_set_version,
            "profile": self.profile,
            "template": self.template,
            "model_id": self.model_id,
            "parameters": dict(self.parameters),
            "start_file_hash": self.start_file_hash,
            "budget": dict(self.budget),
            "cases": [dict(item) for item in self.cases],
            "baseline_successes": self.baseline_successes,
            "candidate_successes": self.candidate_successes,
            "eligible": self.eligible,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ConfirmationRecord:
    """One-time, fully bound direct-chat confirmation."""

    confirmation_id: str
    candidate_id: str
    user_id: str
    session_id: str
    workspace: str
    project_id: str
    skill_id: str
    base_version_hash: str
    candidate_hash: str
    test_set_version: str
    evaluation_id: str
    expires_at: float
    consumed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "candidate_id": self.candidate_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "workspace": self.workspace,
            "project_id": self.project_id,
            "skill_id": self.skill_id,
            "base_version_hash": self.base_version_hash,
            "candidate_hash": self.candidate_hash,
            "test_set_version": self.test_set_version,
            "evaluation_id": self.evaluation_id,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
        }


class SkillImprovementStore:
    """Private, atomic JSON storage for P8 evidence and inactive candidates."""

    def __init__(self, data_dir: str | Path, *, workspace: str | Path | None = None):
        data_root = Path(data_dir).expanduser().resolve()
        if workspace is not None:
            workspace_root = Path(workspace).expanduser().resolve()
            skills_root = workspace_root / "skills"
            if data_root == skills_root or skills_root in data_root.parents:
                raise SkillImprovementError(
                    "private data directory cannot be under workspace skills"
                )
        self.root = data_root / "skill-improvement"
        self.feedback_file = self.root / "feedback.jsonl"
        self.candidates_dir = self.root / "candidates"
        self.evaluations_dir = self.root / "evaluations"
        self.confirmations_dir = self.root / "confirmations"
        self.snapshots_dir = self.root / "task-snapshots"
        self.activations_file = self.root / "activations.jsonl"
        self.integrity_key_file = self.root / "integrity.key"
        self.root.mkdir(parents=True, exist_ok=True)
        self.candidates_dir.mkdir(parents=True, exist_ok=True)
        self.evaluations_dir.mkdir(parents=True, exist_ok=True)
        self.confirmations_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._integrity_key = self._load_integrity_key()
        self._lock = threading.RLock()
        self._recover_activation_journal()

    @classmethod
    def from_environment(cls, workspace: str | Path | None = None) -> SkillImprovementStore:
        default = Path(workspace or ".") / ".nanobot-data"
        return cls(os.environ.get("DEVPILOT_DATA_DIR", str(default)), workspace=workspace)

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
        if source_type in {"model_inference", "external_content"} and not all(
            _EVIDENCE_REF_RE.fullmatch(item) for item in refs
        ):
            raise SkillImprovementError("verification references must be traceable IDs")
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
        if evidence.source_type in {"model_inference", "external_content"} and not all(
            _EVIDENCE_REF_RE.fullmatch(item) for item in refs
        ):
            raise SkillImprovementError("verification references must be traceable IDs")
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
        workspace: str | Path,
    ) -> SkillCandidate:
        if skill_path.replace("\\", "/") != TRIAL_SKILL_RELATIVE_PATH:
            raise SkillImprovementError("candidate path must be the bug-fix Skill")
        ids = _refs(evidence_ids)
        if not ids:
            raise SkillImprovementError("at least one verified evidence ID is required")
        evidence = [self.get_feedback(item) for item in ids]
        if any(item is None or item.processing_state != "verified" for item in evidence):
            raise SkillImprovementError("all candidate evidence must be verified")
        workspace_value = str(Path(workspace).expanduser().resolve())
        effective = self.read_effective_snapshot(workspace)
        if content_sha256(baseline_content) != effective.content_hash:
            raise SkillImprovementError("candidate baseline does not match effective Skill")
        if any(
            item is not None and item.skill_hash != effective.content_hash
            for item in evidence
        ):
            raise SkillImprovementError("candidate evidence does not match effective Skill")
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
        with self._lock:
            if len(self.list_candidates(batch_id=current_batch)) >= _MAX_CANDIDATES_PER_BATCH:
                raise SkillImprovementError("candidate batch limit is three")
            candidate = SkillCandidate(
                candidate_id=f"candidate-{uuid.uuid4()}",
                skill_id=TRIAL_SKILL_ID,
                project_id=_bounded_text(project_id, field="project_id", limit=128),
                workspace=workspace_value,
                skill_path=TRIAL_SKILL_RELATIVE_PATH,
                base_version_hash=content_sha256(baseline_content),
                candidate_content=candidate_content,
                candidate_hash=content_sha256(candidate_content),
                evidence_ids=ids,
                reason=_bounded_text(reason, field="reason"),
                expected_behavior=_bounded_text(expected_behavior, field="expected_behavior"),
                test_set_version=_bounded_text(
                    test_set_version, field="test_set_version", limit=128
                ),
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
        if not _record_id_matches(candidate_id, "candidate"):
            return None
        path = self.candidates_dir / f"{candidate_id}.json"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SkillImprovementError("candidate record is unreadable") from exc
        if not isinstance(raw, dict):
            raise SkillImprovementError("candidate record is invalid")
        candidate = SkillCandidate(
            candidate_id=str(raw["candidate_id"]),
            skill_id=str(raw["skill_id"]),
            project_id=str(raw["project_id"]),
            workspace=str(raw["workspace"]),
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
        if content_sha256(candidate.candidate_content) != candidate.candidate_hash:
            raise SkillImprovementError("candidate content hash mismatch")
        return candidate

    def list_candidates(self, *, batch_id: str | None = None) -> list[SkillCandidate]:
        candidates: list[SkillCandidate] = []
        for path in sorted(self.candidates_dir.glob("candidate-*.json")):
            candidate_id = path.stem
            candidate = self.get_candidate(candidate_id)
            if candidate is not None and (batch_id is None or candidate.batch_id == batch_id):
                candidates.append(candidate)
        return candidates

    def read_effective_snapshot(self, workspace: str | Path) -> SkillSnapshot:
        """Read the only trusted on-disk version of the trial Skill."""
        root = Path(workspace).expanduser().resolve()
        lexical_path = root / TRIAL_SKILL_RELATIVE_PATH
        if lexical_path.parent.is_symlink() or lexical_path.is_symlink():
            raise SkillImprovementError("effective bug-fix Skill cannot be a symlink")
        path = lexical_path.resolve()
        expected = root / TRIAL_SKILL_RELATIVE_PATH
        if path != expected or not path.is_file():
            raise SkillImprovementError("effective bug-fix Skill is unavailable")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillImprovementError("effective bug-fix Skill is unreadable") from exc
        return SkillSnapshot(
            skill_id=TRIAL_SKILL_ID,
            skill_path=TRIAL_SKILL_RELATIVE_PATH,
            content=content,
            content_hash=content_sha256(content),
            captured_at=_now(),
        )

    def capture_task_snapshot(
        self,
        *,
        task_id: str,
        workspace: str | Path,
        session_id: str,
        project_id: str,
    ) -> SkillSnapshot:
        """Pin the first effective version for a task; never silently refresh it."""
        task_key = content_sha256(_bounded_text(task_id, field="task_id", limit=128))
        path = self.snapshots_dir / f"{task_key}.json"
        with self._lock:
            task_value = _bounded_text(task_id, field="task_id", limit=128)
            session_value = _bounded_text(session_id, field="session_id", limit=128)
            project_value = _bounded_text(project_id, field="project_id", limit=128)
            workspace_value = str(Path(workspace).expanduser().resolve())
            if path.is_file():
                raw = self._read_object(path, "task snapshot")
                if (
                    raw.get("task_id") != task_value
                    or raw.get("session_id") != session_value
                    or raw.get("project_id") != project_value
                    or raw.get("workspace") != workspace_value
                ):
                    raise SkillImprovementError("task snapshot binding mismatch")
                return self._snapshot_from_dict(raw)
            effective = self.read_effective_snapshot(workspace)
            snapshot = SkillSnapshot(
                **self._snapshot_to_dict(effective),
                task_id=task_value,
                session_id=session_value,
                project_id=project_value,
                workspace=workspace_value,
            )
            self._atomic_write(
                path,
                {
                    "task_id": task_value,
                    "session_id": session_value,
                    "project_id": project_value,
                    "workspace": workspace_value,
                    **self._snapshot_to_dict(snapshot),
                },
            )
            return snapshot

    def evaluate_candidate(
        self,
        candidate_id: str,
        *,
        workspace: str | Path,
        model_id: str = "deterministic-synthetic",
        parameters: Mapping[str, Any] | None = None,
        budget: Mapping[str, Any] | None = None,
    ) -> EvaluationReport:
        """Run the immutable 18-case synthetic comparison.

        The case definitions and assertions live in this trusted module.  Skill
        text can affect only the candidate's behavior, never the test set or
        its pass criteria.  No model call is made by this deterministic runner.
        """
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise SkillImprovementError("candidate not found")
        if candidate.test_set_version != EVALUATION_TEST_SET_VERSION:
            raise SkillImprovementError("candidate uses an unsupported test set")
        baseline = self.read_effective_snapshot(workspace)
        if baseline.content_hash != candidate.base_version_hash:
            self._set_candidate_status(candidate, "stale")
            raise SkillImprovementError("effective Skill changed; candidate is stale")
        evaluation_id = f"evaluation-{uuid.uuid4()}"
        started = time.perf_counter()
        cases: list[dict[str, Any]] = []
        for category in EVALUATION_CATEGORIES:
            for version in EVALUATION_VERSIONS:
                content = baseline.content if version == "baseline" else candidate.candidate_content
                success, agent_trace = _run_synthetic_agent(category, content)
                for repeat in range(1, EVALUATION_REPEATS + 1):
                    cases.append(
                        {
                            "case_id": f"{category}:{version}:{repeat}",
                            "category": category,
                            "version": version,
                            "repeat": repeat,
                            "success": success,
                            "reason": (
                                "agent/tool trace satisfied fixed assertion"
                                if success
                                else "agent/tool trace failed fixed assertion"
                            ),
                            "agent_trace": list(agent_trace),
                            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                            "usage": {
                                "input_tokens": "unavailable",
                                "output_tokens": "unavailable",
                            },
                            "skill_hash": (
                                baseline.content_hash
                                if version == "baseline"
                                else candidate.candidate_hash
                            ),
                        }
                    )
        baseline_successes = sum(
            1 for item in cases if item["version"] == "baseline" and item["success"]
        )
        candidate_successes = sum(
            1 for item in cases if item["version"] == "candidate" and item["success"]
        )
        baseline_by_category = _category_successes(cases, "baseline")
        candidate_by_category = _category_successes(cases, "candidate")
        eligible = (
            baseline_successes < candidate_successes
            and all(
                candidate_by_category[category] >= baseline_by_category[category]
                for category in EVALUATION_CATEGORIES
            )
            and candidate_by_category["regression"] == EVALUATION_REPEATS
        )
        report = EvaluationReport(
            evaluation_id=evaluation_id,
            candidate_id=candidate.candidate_id,
            test_set_version=EVALUATION_TEST_SET_VERSION,
            profile="bug-fix",
            template="p8-02-fixed-synthetic-v1",
            model_id=_bounded_text(model_id, field="model_id", limit=128),
            parameters=_json_safe(parameters or {}),
            start_file_hash=baseline.content_hash,
            budget=_json_safe(budget or {"max_model_calls": 0, "max_tokens": 0}),
            cases=tuple(cases),
            baseline_successes=baseline_successes,
            candidate_successes=candidate_successes,
            eligible=eligible,
            status="eligible" if eligible else "no_verified_improvement",
            created_at=_now(),
        )
        self._atomic_write(self.evaluations_dir / f"{evaluation_id}.json", report.to_dict())
        self._set_candidate_status(
            candidate,
            "pending_confirmation" if eligible else "evaluation_failed",
            result_refs=(evaluation_id,),
        )
        return report

    def get_evaluation(self, evaluation_id: str) -> EvaluationReport | None:
        if not _record_id_matches(evaluation_id, "evaluation"):
            return None
        path = self.evaluations_dir / f"{evaluation_id}.json"
        if not path.is_file():
            return None
        raw = self._read_object(path, "evaluation")
        report = EvaluationReport(
            evaluation_id=str(raw["evaluation_id"]),
            candidate_id=str(raw["candidate_id"]),
            test_set_version=str(raw["test_set_version"]),
            profile=str(raw["profile"]),
            template=str(raw["template"]),
            model_id=str(raw["model_id"]),
            parameters=dict(raw.get("parameters") or {}),
            start_file_hash=str(raw["start_file_hash"]),
            budget=dict(raw.get("budget") or {}),
            cases=tuple(dict(item) for item in raw.get("cases", []) if isinstance(item, dict)),
            baseline_successes=int(raw["baseline_successes"]),
            candidate_successes=int(raw["candidate_successes"]),
            eligible=bool(raw["eligible"]),
            status=str(raw["status"]),
            created_at=str(raw["created_at"]),
        )
        expected_ids = {
            f"{category}:{version}:{repeat}"
            for category in EVALUATION_CATEGORIES
            for version in EVALUATION_VERSIONS
            for repeat in range(1, EVALUATION_REPEATS + 1)
        }
        if {str(item.get("case_id")) for item in report.cases} != expected_ids:
            raise SkillImprovementError("evaluation case set was changed")
        return report

    def issue_confirmation(
        self,
        candidate_id: str,
        *,
        user_id: str,
        session_id: str,
        workspace: str | Path,
        project_id: str,
    ) -> ConfirmationRecord:
        candidate = self.get_candidate(candidate_id)
        if candidate is None or candidate.status != "pending_confirmation":
            raise SkillImprovementError("candidate is not eligible for confirmation")
        if candidate.workspace != str(Path(workspace).expanduser().resolve()):
            raise SkillImprovementError("candidate workspace mismatch")
        evaluation = (
            self.get_evaluation(candidate.result_refs[-1])
            if candidate.result_refs
            else None
        )
        if evaluation is None or not evaluation.eligible:
            raise SkillImprovementError("eligible evaluation is required")
        confirmation = ConfirmationRecord(
            confirmation_id=f"confirmation-{uuid.uuid4()}",
            candidate_id=candidate.candidate_id,
            user_id=_bounded_text(user_id, field="user_id", limit=128),
            session_id=_bounded_text(session_id, field="session_id", limit=128),
            workspace=str(Path(workspace).expanduser().resolve()),
            project_id=_bounded_text(project_id, field="project_id", limit=128),
            skill_id=candidate.skill_id,
            base_version_hash=candidate.base_version_hash,
            candidate_hash=candidate.candidate_hash,
            test_set_version=evaluation.test_set_version,
            evaluation_id=evaluation.evaluation_id,
            expires_at=time.time() + _CONFIRMATION_TTL_SECONDS,
        )
        self._atomic_write(
            self.confirmations_dir / f"{confirmation.confirmation_id}.json",
            confirmation.to_dict(),
        )
        return confirmation

    def activate_candidate(
        self,
        confirmation_id: str,
        *,
        workspace: str | Path,
        user_id: str,
        session_id: str,
        project_id: str,
    ) -> SkillSnapshot:
        """Consume a bound confirmation and atomically replace the trusted Skill."""
        if not _record_id_matches(confirmation_id, "confirmation"):
            raise SkillImprovementError("confirmation not found")
        path = self.confirmations_dir / f"{confirmation_id}.json"
        with self._lock:
            confirmation = self._confirmation_from_dict(self._read_object(path, "confirmation"))
            self._check_confirmation(confirmation, user_id, session_id, workspace, project_id)
            candidate = self.get_candidate(confirmation.candidate_id)
            if (
                candidate is None
                or candidate.status != "pending_confirmation"
                or candidate.workspace != confirmation.workspace
                or candidate.candidate_hash != confirmation.candidate_hash
            ):
                raise SkillImprovementError("candidate changed after confirmation")
            current = self.read_effective_snapshot(workspace)
            if current.content_hash != confirmation.base_version_hash:
                self._set_candidate_status(candidate, "stale")
                raise SkillImprovementError("effective Skill changed after confirmation")
            skill_path = Path(workspace).expanduser().resolve() / TRIAL_SKILL_RELATIVE_PATH
            journal = {
                "phase": "prepared",
                "confirmation_id": confirmation.confirmation_id,
                "candidate_id": candidate.candidate_id,
                "workspace": str(Path(workspace).expanduser().resolve()),
                "previous_content": current.content,
                "previous_hash": current.content_hash,
                "active_hash": candidate.candidate_hash,
                "activated_at": _now(),
            }
            self._append_jsonl(self.activations_file, journal)
            self._atomic_replace_text(skill_path, candidate.candidate_content)
            updated = ConfirmationRecord(**{**confirmation.to_dict(), "consumed_at": _now()})
            self._atomic_write(path, updated.to_dict())
            active = self.read_effective_snapshot(workspace)
            self._append_jsonl(self.activations_file, {
                **journal,
                "phase": "committed",
                "active_hash": active.content_hash,
            })
            self._set_candidate_status(candidate, "active")
            return active

    def ignore_candidate(self, candidate_id: str) -> SkillCandidate:
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise SkillImprovementError("candidate not found")
        return self._set_candidate_status(candidate, "ignored")

    def restore_candidate(
        self,
        candidate_id: str,
        *,
        workspace: str | Path,
    ) -> SkillSnapshot:
        """Restore the saved stable content only if the active file is untouched."""
        candidate = self.get_candidate(candidate_id)
        if candidate is None or candidate.status != "active":
            raise SkillImprovementError("candidate is not active")
        current = self.read_effective_snapshot(workspace)
        if current.content_hash != candidate.candidate_hash:
            raise SkillImprovementError("restore stopped: effective Skill was externally modified")
        records = [
            item for item in self._read_jsonl(self.activations_file)
            if item.get("candidate_id") == candidate_id
            and item.get("workspace") == str(Path(workspace).expanduser().resolve())
            and item.get("phase") == "committed"
        ]
        if not records or not isinstance(records[-1].get("previous_content"), str):
            raise SkillImprovementError("stable version for restore is unavailable")
        skill_path = Path(workspace).expanduser().resolve() / TRIAL_SKILL_RELATIVE_PATH
        self._atomic_replace_text(skill_path, records[-1]["previous_content"])
        restored = self.read_effective_snapshot(workspace)
        self._set_candidate_status(candidate, "stale")
        return restored

    def _recover_activation_journal(self) -> None:
        """Reconcile a crash between the file swap and metadata persistence."""
        if not self.activations_file.exists():
            return
        try:
            records = self._read_jsonl(self.activations_file)
        except SkillImprovementError:
            return
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for record in records:
            candidate_id = record.get("candidate_id")
            workspace = record.get("workspace")
            if isinstance(candidate_id, str) and isinstance(workspace, str):
                latest[(candidate_id, workspace)] = record
        for (candidate_id, workspace), record in latest.items():
            if record.get("phase") not in {"prepared", "committed"}:
                continue
            candidate = self.get_candidate(candidate_id)
            if candidate is None:
                continue
            try:
                current = self.read_effective_snapshot(workspace)
            except SkillImprovementError:
                continue
            if current.content_hash == record.get("active_hash"):
                if record.get("phase") == "prepared":
                    self._append_jsonl(
                        self.activations_file, {**record, "phase": "committed"}
                    )
                confirmation_id = record.get("confirmation_id")
                if isinstance(confirmation_id, str) and _record_id_matches(
                    confirmation_id, "confirmation"
                ):
                    confirmation_path = self.confirmations_dir / f"{confirmation_id}.json"
                    if confirmation_path.is_file():
                        confirmation = self._confirmation_from_dict(
                            self._read_object(confirmation_path, "confirmation")
                        )
                        if confirmation.consumed_at is None:
                            self._atomic_write(
                                confirmation_path,
                                ConfirmationRecord(
                                    **{**confirmation.to_dict(), "consumed_at": _now()}
                                ).to_dict(),
                            )
                self._set_candidate_status(candidate, "active")
            elif current.content_hash == record.get("previous_hash"):
                self._append_jsonl(self.activations_file, {**record, "phase": "aborted"})
            else:
                self._set_candidate_status(candidate, "stale")

    def _set_candidate_status(
        self,
        candidate: SkillCandidate,
        status: str,
        *,
        result_refs: tuple[str, ...] | None = None,
    ) -> SkillCandidate:
        if status not in CANDIDATE_STATES:
            raise SkillImprovementError("invalid candidate status")
        updated = SkillCandidate(**{
            **candidate.to_dict(),
            "evidence_ids": tuple(candidate.evidence_ids),
            "result_refs": tuple(result_refs if result_refs is not None else candidate.result_refs),
            "status": status,
        })
        self._atomic_write(
            self.candidates_dir / f"{candidate.candidate_id}.json", updated.to_dict()
        )
        return updated

    @staticmethod
    def _snapshot_to_dict(snapshot: SkillSnapshot) -> dict[str, Any]:
        return {
            "skill_id": snapshot.skill_id,
            "skill_path": snapshot.skill_path,
            "content": snapshot.content,
            "content_hash": snapshot.content_hash,
            "captured_at": snapshot.captured_at,
        }

    @staticmethod
    def _snapshot_from_dict(raw: Mapping[str, Any]) -> SkillSnapshot:
        content = str(raw["content"])
        if content_sha256(content) != str(raw["content_hash"]):
            raise SkillImprovementError("task snapshot hash mismatch")
        return SkillSnapshot(
            skill_id=str(raw["skill_id"]),
            skill_path=str(raw["skill_path"]),
            content=content,
            content_hash=str(raw["content_hash"]),
            captured_at=str(raw["captured_at"]),
            task_id=raw.get("task_id") if isinstance(raw.get("task_id"), str) else None,
            session_id=raw.get("session_id") if isinstance(raw.get("session_id"), str) else None,
            project_id=raw.get("project_id") if isinstance(raw.get("project_id"), str) else None,
            workspace=raw.get("workspace") if isinstance(raw.get("workspace"), str) else None,
        )

    def _read_object(self, path: Path, label: str) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SkillImprovementError(f"{label} is unreadable") from exc
        if not isinstance(value, dict):
            raise SkillImprovementError(f"{label} is invalid")
        supplied_mac = value.pop("record_mac", None)
        if not isinstance(supplied_mac, str) or not hmac.compare_digest(
            supplied_mac, self._record_mac(value)
        ):
            raise SkillImprovementError(f"{label} integrity check failed")
        return value

    def _load_integrity_key(self) -> bytes:
        try:
            if self.integrity_key_file.is_file():
                key = self.integrity_key_file.read_bytes()
                if len(key) >= 32:
                    return key
            key = secrets.token_bytes(32)
            with self.integrity_key_file.open("xb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            return key
        except FileExistsError:
            key = self.integrity_key_file.read_bytes()
            if len(key) < 32:
                raise SkillImprovementError("integrity key is invalid") from None
            return key
        except OSError as exc:
            raise SkillImprovementError("integrity key is unavailable") from exc

    def _record_mac(self, value: Mapping[str, Any]) -> str:
        payload = json.dumps(
            {key: value[key] for key in sorted(value) if key != "record_mac"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._integrity_key, payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _confirmation_from_dict(raw: Mapping[str, Any]) -> ConfirmationRecord:
        return ConfirmationRecord(
            confirmation_id=str(raw["confirmation_id"]),
            candidate_id=str(raw["candidate_id"]),
            user_id=str(raw["user_id"]),
            session_id=str(raw["session_id"]),
            workspace=str(raw["workspace"]),
            project_id=str(raw["project_id"]),
            skill_id=str(raw["skill_id"]),
            base_version_hash=str(raw["base_version_hash"]),
            candidate_hash=str(raw["candidate_hash"]),
            test_set_version=str(raw["test_set_version"]),
            evaluation_id=str(raw["evaluation_id"]),
            expires_at=float(raw["expires_at"]),
            consumed_at=raw.get("consumed_at") if isinstance(raw.get("consumed_at"), str) else None,
        )

    @staticmethod
    def _check_confirmation(
        confirmation: ConfirmationRecord,
        user_id: str,
        session_id: str,
        workspace: str | Path,
        project_id: str,
    ) -> None:
        if confirmation.consumed_at is not None:
            raise SkillImprovementError("confirmation was already consumed")
        if confirmation.expires_at <= time.time():
            raise SkillImprovementError("confirmation expired")
        expected = (
            _bounded_text(user_id, field="user_id", limit=128),
            _bounded_text(session_id, field="session_id", limit=128),
            str(Path(workspace).expanduser().resolve()),
            _bounded_text(project_id, field="project_id", limit=128),
        )
        actual = (
            confirmation.user_id,
            confirmation.session_id,
            confirmation.workspace,
            confirmation.project_id,
        )
        if actual != expected:
            raise SkillImprovementError("confirmation scope mismatch")

    def _atomic_replace_text(self, path: Path, content: str) -> None:
        self._atomic_write_text(path, content)

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
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
                result.append(self._read_object_value(value, "JSONL record"))
        return result

    def _append_jsonl(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = _json_safe(value)
        safe["record_mac"] = self._record_mac(safe)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _atomic_write(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = _json_safe(value)
        safe["record_mac"] = self._record_mac(safe)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _read_object_value(self, value: dict[str, Any], label: str) -> dict[str, Any]:
        supplied_mac = value.pop("record_mac", None)
        if not isinstance(supplied_mac, str) or not hmac.compare_digest(
            supplied_mac, self._record_mac(value)
        ):
            raise SkillImprovementError(f"{label} integrity check failed")
        return value


def _run_synthetic_agent(category: str, content: str) -> tuple[bool, tuple[str, ...]]:
    """Run a tiny trusted Agent/Tool protocol against fixed synthetic tasks."""
    if category == "original_failure":
        if "Re-read the current file before proposing a patch." in content:
            return True, ("read_file", "recheck_current_file", "propose_patch")
        return False, ("propose_patch",)
    if category == "nearby_holdout":
        if "Read the failure evidence." in content:
            return True, ("read_failure_evidence", "propose_patch")
        return False, ("propose_patch",)
    if category == "regression":
        if "# Bug fix" in content and "Read the failure evidence." in content:
            return True, ("read_failure_evidence", "preserve_regression", "propose_patch")
        return False, ("propose_patch",)
    raise SkillImprovementError("unknown fixed evaluation category")


def _category_successes(cases: list[dict[str, Any]], version: str) -> dict[str, int]:
    return {
        category: sum(
            1
            for item in cases
            if item["version"] == version
            and item["category"] == category
            and item["success"]
        )
        for category in EVALUATION_CATEGORIES
    }


def _record_id_matches(value: str, prefix: str) -> bool:
    return bool(re.fullmatch(rf"{re.escape(prefix)}-[0-9a-f-]+", value or ""))


def _frontmatter(content: str) -> str:
    """Return exact metadata so candidate content cannot alter its trust contract."""
    if not content.startswith("---\n"):
        raise SkillImprovementError("Skill frontmatter is required")
    end = content.find("\n---", 4)
    if end < 0:
        raise SkillImprovementError("Skill frontmatter is malformed")
    return content[: end + 4]
