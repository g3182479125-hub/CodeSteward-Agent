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
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redaction import redact_sensitive_payload
from .replay_workspace import ReplayabilityError, capture_git_snapshot

TRIAL_SKILL_ID = "bug-fix"
TRIAL_SKILL_RELATIVE_PATH = "skills/bug-fix/SKILL.md"
IMPROVEMENT_CASE_STATES = frozenset(
    {"eligible", "claimed", "not_replayable", "evaluated", "ignored"}
)
REPLAY_TRIAL_STATUSES = frozenset(
    {"passed", "failed", "timeout", "cancelled", "infrastructure_error"}
)
FEEDBACK_SOURCE_TYPES = frozenset(
    {
        "user_feedback",
        "tool_result",
        "trusted_verification",
        "trusted_failure_evidence",
        "model_inference",
        "external_content",
    }
)
FEEDBACK_STATES = frozenset(
    {"verified", "pending_verification", "rejected", "deleted"}
)
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
BATCH_STATES = frozenset(
    {
        "idle",
        "running",
        "no_new_evidence",
        "candidate_created",
        "failed",
        "budget_exhausted",
        "disabled",
    }
)
_MAX_FEEDBACK_TEXT = 4_000
_MAX_EVIDENCE_REFS = 20
_MAX_CANDIDATES_PER_BATCH = 3
_MAX_SCHEDULED_BATCH_EVIDENCE = 20
_MAX_SCHEDULED_MODEL_CALLS = 2
_MAX_SCHEDULED_TOKENS = 20_000
_MAX_SCHEDULED_DURATION_SECONDS = 600
_SCHEDULED_LEASE_SECONDS = 660
_MAX_PROCESSED_EVIDENCE_IDS = 1_024
EVALUATION_CATEGORIES = ("original_failure", "nearby_holdout", "regression")
EVALUATION_VERSIONS = ("baseline", "candidate")
EVALUATION_REPEATS = 3
EVALUATION_TEST_SET_VERSION = "p8-02-v1"
_CONFIRMATION_TTL_SECONDS = 300
_EVIDENCE_REF_RE = re.compile(
    r"^(run|tool|trace|session|test|failure|feedback|case|trial|snapshot|verification)-[A-Za-z0-9_.:-]{1,127}$"
    r"|^local:[A-Za-z0-9_.:-]{1,127}$"
)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TRUSTED_VERIFICATION_REF_RE = re.compile(
    r"^verification-[A-Za-z0-9_.-]{1,127}:[A-Za-z0-9_.-]{1,127}$"
)
_TRUSTED_FAILURE_REF_RE = re.compile(r"^failure-[A-Za-z0-9_.-]{1,127}$")


def _trusted_verification_parts(ref: str) -> tuple[str, str] | None:
    if not _TRUSTED_VERIFICATION_REF_RE.fullmatch(ref):
        return None
    run_ref, change_set_id = ref.removeprefix("verification-").split(":", 1)
    return run_ref, change_set_id


def _traceable_ref(value: str, *, origin_run_id: str | None = None) -> bool:
    return bool(
        _EVIDENCE_REF_RE.fullmatch(value)
        or (origin_run_id is not None and value == origin_run_id)
    )


@dataclass(frozen=True)
class SkillImprovementTarget:
    """Developer-owned mapping from a task profile to its improveable Skill."""

    skill_id: str
    profile_name: str
    profile_version: str
    allowed_trigger_types: frozenset[str]
    grader_kind: str

    @property
    def profile_binding(self) -> str:
        return f"{self.profile_name}@{self.profile_version}"

    @property
    def skill_path(self) -> str:
        return f"skills/{self.skill_id}/SKILL.md"


SKILL_IMPROVEMENT_TARGETS = {
    "repo-analysis": SkillImprovementTarget(
        skill_id="repo-analysis",
        profile_name="repo-analysis",
        profile_version="1",
        allowed_trigger_types=frozenset({"user_confirmed_missing_fact"}),
        grader_kind="analysis_fact",
    ),
    "feature-delivery": SkillImprovementTarget(
        skill_id="feature-delivery",
        profile_name="feature-delivery",
        profile_version="2",
        allowed_trigger_types=frozenset({"trusted_verification_failed"}),
        grader_kind="delivery_verification",
    ),
    "bug-fix": SkillImprovementTarget(
        skill_id="bug-fix",
        profile_name="bug-fix",
        profile_version="1",
        allowed_trigger_types=frozenset({"trusted_verification_failed"}),
        grader_kind="bug_fix_verification",
    ),
}


def skill_improvement_target(skill_id: str) -> SkillImprovementTarget:
    try:
        return SKILL_IMPROVEMENT_TARGETS[skill_id]
    except KeyError as exc:
        raise SkillImprovementError(f"unsupported Skill improvement target: {skill_id}") from exc


def integrity_key_path_for_data_dir(data_dir: str | Path) -> Path:
    """Return the independent user-owned integrity-key path for a data root."""
    data_root = Path(data_dir).expanduser().resolve()
    configured = os.environ.get("DEVPILOT_INTEGRITY_KEY_FILE")
    return Path(configured).expanduser().resolve() if configured else (
        data_root.parent / f".{data_root.name}.integrity.key"
    )


class SkillImprovementError(ValueError):
    """Raised when feedback or a Skill candidate violates the P8 contract."""


def content_sha256(content: str | bytes) -> str:
    """Return the hash of the exact UTF-8/byte content used by the contract."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def scheduled_batch_id(
    workspace: str | Path,
    project_id: str,
    user_id: str,
    skill_id: str = TRIAL_SKILL_ID,
) -> str:
    """Return the stable identity of a user's configured Skill batch."""
    scope = "\0".join(
        (
            str(Path(workspace).expanduser().resolve()),
            project_id,
            user_id,
            skill_id,
        )
    )
    return f"batch-{content_sha256(scope)[:24]}"


def task_snapshot_ref(task_id: str) -> str:
    """Return the stable private record ID for one task snapshot."""
    return f"snapshot-{content_sha256(task_id)[:32]}"


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
    workspace: str
    user_id: str
    session_id: str
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
            "workspace": self.workspace,
            "user_id": self.user_id,
            "session_id": self.session_id,
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
class ImprovementCase:
    """Trusted, immutable unit selected by future Skill-improvement batches."""

    case_id: str
    skill_id: str
    profile_binding: str
    origin_run_id: str
    task_id: str
    project_id: str
    workspace: str
    user_id: str
    session_id: str
    base_skill_hash: str
    trigger_type: str
    trigger_summary: str
    expected_behavior: str
    evidence_refs: tuple[str, ...]
    replay_snapshot_ref: str | None
    assertion_spec: dict[str, object]
    status: str
    selected_batch_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "skill_id": self.skill_id,
            "profile_binding": self.profile_binding,
            "origin_run_id": self.origin_run_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "workspace": self.workspace,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "base_skill_hash": self.base_skill_hash,
            "trigger_type": self.trigger_type,
            "trigger_summary": self.trigger_summary,
            "expected_behavior": self.expected_behavior,
            "evidence_refs": list(self.evidence_refs),
            "replay_snapshot_ref": self.replay_snapshot_ref,
            "assertion_spec": dict(self.assertion_spec),
            "status": self.status,
            "selected_batch_id": self.selected_batch_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ReplayTrial:
    """Append-only trusted evaluation result for one ImprovementCase."""

    trial_id: str
    case_id: str
    skill_id: str
    skill_hash: str
    version: str
    model_id: str
    parameters: dict[str, object]
    snapshot_ref: str
    started_at: str
    finished_at: str
    token_count: int
    duration_seconds: float
    tool_trace_ref: str
    final_output_ref: str
    assertion_result: dict[str, object]
    status: str
    candidate_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "case_id": self.case_id,
            "skill_id": self.skill_id,
            "skill_hash": self.skill_hash,
            "version": self.version,
            "model_id": self.model_id,
            "parameters": dict(self.parameters),
            "snapshot_ref": self.snapshot_ref,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "token_count": self.token_count,
            "duration_seconds": self.duration_seconds,
            "tool_trace_ref": self.tool_trace_ref,
            "final_output_ref": self.final_output_ref,
            "assertion_result": dict(self.assertion_result),
            "status": self.status,
            "candidate_id": self.candidate_id,
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
    user_id: str = ""
    session_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    case_ids: tuple[str, ...] = ()

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
            "user_id": self.user_id,
            "session_ids": list(self.session_ids),
            "task_ids": list(self.task_ids),
            "case_ids": list(self.case_ids),
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
    repository_root: str | None = None
    repository_identity: str | None = None
    start_commit_sha: str | None = None
    working_tree_clean: bool | None = None
    allowed_file_scope: tuple[str, ...] = ()
    verification_profile: str | None = None
    replayable: bool | None = None
    replayability_reason: str | None = None
    replay_metadata_pending: bool = False
    source_evidence_refs: tuple[str, ...] = ()
    start_replayable: bool | None = None
    start_replayability_reason: str | None = None


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
    case_id: str | None = None
    baseline_trial_id: str | None = None
    candidate_trial_id: str | None = None

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
            "case_id": self.case_id,
            "baseline_trial_id": self.baseline_trial_id,
            "candidate_trial_id": self.candidate_trial_id,
        }


@dataclass(frozen=True)
class FixedSkillScenario:
    """Immutable fixture and assertion used by the P8 comparison runner."""

    category: str
    fixture_id: str
    source_text: str
    failure_evidence: str
    expected_action: str


FIXED_SKILL_SCENARIOS = (
    FixedSkillScenario(
        category="original_failure",
        fixture_id="discount.py",
        source_text="def discount(price, percent):\n    return price - price * percent / 10\n",
        failure_evidence="10 percent must reduce a 100 price by 10, not 100.",
        expected_action="read_current_file_before_proposing_patch",
    ),
    FixedSkillScenario(
        category="nearby_holdout",
        fixture_id="parser.py",
        source_text="def parse(value):\n    return value.strip()\n",
        failure_evidence="The parser must preserve the caller's explicit failure evidence.",
        expected_action="read_failure_evidence",
    ),
    FixedSkillScenario(
        category="regression",
        fixture_id="bug-fix-contract.md",
        source_text="# Bug fix\nRead the failure evidence.\n",
        failure_evidence="The bug-fix contract itself must remain protected.",
        expected_action="preserve_regression_contract",
    ),
)


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


@dataclass(frozen=True)
class ScheduledSkillBatch:
    """Persistent policy and cursor for one native-Cron Skill batch."""

    batch_id: str
    job_id: str
    skill_id: str
    workspace: str
    project_id: str
    user_id: str
    session_key: str
    enabled: bool
    read_scope: str
    max_model_calls: int
    max_tokens: int
    max_duration_seconds: int
    notification_mode: str
    cursor_at: str | None
    processed_evidence_ids: tuple[str, ...]
    claimed_evidence_ids: tuple[str, ...]
    status: str
    lease_owner: str | None
    lease_expires_at: float | None
    run_count: int
    candidate_attempts: int
    token_count: int
    consecutive_failures: int
    candidate_id: str | None
    last_run_at: str | None
    last_error: str | None
    last_notified_error: str | None
    last_candidate_ids: tuple[str, ...]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "job_id": self.job_id,
            "skill_id": self.skill_id,
            "workspace": self.workspace,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "session_key": self.session_key,
            "enabled": self.enabled,
            "read_scope": self.read_scope,
            "max_model_calls": self.max_model_calls,
            "max_tokens": self.max_tokens,
            "max_duration_seconds": self.max_duration_seconds,
            "notification_mode": self.notification_mode,
            "cursor_at": self.cursor_at,
            "processed_evidence_ids": list(self.processed_evidence_ids),
            "claimed_evidence_ids": list(self.claimed_evidence_ids),
            "status": self.status,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "run_count": self.run_count,
            "candidate_attempts": self.candidate_attempts,
            "token_count": self.token_count,
            "consecutive_failures": self.consecutive_failures,
            "candidate_id": self.candidate_id,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "last_notified_error": self.last_notified_error,
            "last_candidate_ids": list(self.last_candidate_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ScheduledBatchClaim:
    """Result of the cross-process preflight claim."""

    status: str
    batch: ScheduledSkillBatch
    evidence: tuple[FeedbackEvidence, ...] = ()
    lease_owner: str | None = None
    deadline_at: float | None = None


def build_scheduled_batch_prompt(
    claim: ScheduledBatchClaim,
    *,
    baseline_content: str,
) -> str:
    """Build the bounded, read-only prompt used by a scheduled agent turn."""
    if claim.status != "claimed" or not claim.lease_owner or not claim.deadline_at:
        raise SkillImprovementError("scheduled batch prompt requires an active claim")
    evidence_lines: list[str] = []
    for item in claim.evidence:
        evidence_lines.append(
            "- "
            + item.evidence_id
            + ": correction="
            + item.correction[:1_200]
            + "; actual_result="
            + item.actual_result[:1_200]
        )
    return "\n".join(
        [
            "Run one controlled DevFlow Agent Skill-improvement batch.",
            "This is analysis and candidate generation only: never edit the project, "
            "run commands, evaluate, enable, activate, apply, or restore a Skill.",
            f"Target Skill: {claim.batch.skill_id}",
            f"Project ID: {claim.batch.project_id}",
            f"Batch ID: {claim.batch.batch_id}",
            "Use only the verified evidence below; do not invent additional evidence.",
            *evidence_lines,
            "",
            "Active Skill baseline (copy its protected frontmatter exactly):",
            "```markdown",
            baseline_content,
            "```",
            "",
            "Call devpilot_skill_candidate exactly once with all claimed evidence IDs "
            "and test_set_version='p8-02-v1'. If the tool rejects the candidate for a "
            "format or contract error, correct it once and retry; then stop.",
            "The candidate must remain pending_evaluation. Report the candidate ID and "
            "do not perform any other tool action.",
        ]
    )


class SkillImprovementStore:
    """Private, integrity-protected SQLite storage for P8 evidence and candidates."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        workspace: str | Path | None = None,
    ):
        data_root = Path(data_dir).expanduser().resolve()
        if workspace is not None:
            workspace_root = Path(workspace).expanduser().resolve()
            skills_root = workspace_root / "skills"
            if data_root == skills_root or skills_root in data_root.parents:
                raise SkillImprovementError(
                    "private data directory cannot be under workspace skills"
                )
        # P8 uses the existing DevPilot SQLite data root. Candidate records
        # live in a dedicated table, not a second JSON database.
        self.root = data_root
        self.database_path = data_root / "devpilot.db"
        self.workspace = (
            Path(workspace).expanduser().resolve() if workspace is not None else None
        )
        if self.workspace is not None and (
            data_root == self.workspace or self.workspace in data_root.parents
        ):
            raise SkillImprovementError(
                "private Skill improvement data cannot be stored inside the workspace"
            )
        self.integrity_key_file = integrity_key_path_for_data_dir(data_root)
        if self.integrity_key_file == data_root or data_root in self.integrity_key_file.parents:
            raise SkillImprovementError("integrity key must be outside the private data root")
        if self.workspace is not None and (
            self.integrity_key_file == self.workspace
            or self.workspace in self.integrity_key_file.parents
        ):
            raise SkillImprovementError("integrity key cannot be stored inside the workspace")
        # These paths are logical record namespaces retained for diagnostics;
        # no P8 payload is written to them.
        self.feedback_file = self.root / "feedback.jsonl"
        self.candidates_dir = self.root / "candidates"
        self.batches_dir = self.root / "batches"
        self.evaluations_dir = self.root / "evaluations"
        self.confirmations_dir = self.root / "confirmations"
        self.snapshots_dir = self.root / "task-snapshots"
        self.cases_dir = self.root / "improvement-cases"
        self.replay_trials_dir = self.root / "replay-trials"
        self.activations_file = self.root / "activations.jsonl"
        self.process_lock_file = self.root / "skill-improvement.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        self._initialize_sqlite_table()
        self._integrity_key = self._load_integrity_key()
        self._lock = threading.RLock()
        self._recovery_blocked = False
        self._recover_activation_journal()

    def _initialize_sqlite_table(self) -> None:
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            has_alembic = connection.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'alembic_version'"
            ).fetchone()
            version_rows = (
                connection.execute("select version_num from alembic_version").fetchall()
                if has_alembic
                else []
            )
            if has_alembic and not version_rows:
                raise SkillImprovementError("database schema version is unavailable")
            if not has_alembic:
                raise SkillImprovementError(
                    "database schema must be initialized by Alembic before P8 use"
                )
            compatible_heads = {
                "0011_p8_skill_improvement_records",
                "0012_h3_plugin_runtime_state",
                "0013_h8_loop_handoff_state",
                "0014_h8_requested_driver_state",
                "0015_h8_switch_generation",
                "0016_h9_harness_selection_records",
            }
            if len(version_rows) != 1:
                raise SkillImprovementError(
                    "database schema must have exactly one supported Alembic head"
                )
            version = str(version_rows[0][0])
            if version not in compatible_heads:
                raise SkillImprovementError(
                    "database schema head is unsupported; expected a supported linear P8 head"
                )
            has_records_table = connection.execute(
                "select 1 from sqlite_master "
                "where type = 'table' and name = 'skill_improvement_records'"
            ).fetchone()
            if has_records_table is None:
                raise SkillImprovementError(
                    "database schema is incomplete; rerun Alembic migration 0011"
                )
            return

    def _record_rows(self, record_type: str, record_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            "select record_id, payload_json, record_mac from skill_improvement_records "
            "where record_type = ?"
        )
        params: list[str] = [record_type]
        if record_id is not None:
            query += " and record_id = ?"
            params.append(record_id)
        query += " order by id"
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            rows = connection.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for _row_id, payload_json, record_mac in rows:
            value = json.loads(payload_json)
            if not isinstance(value, dict) or not hmac.compare_digest(
                str(record_mac), self._record_mac(value)
            ):
                raise SkillImprovementError(f"{record_type} integrity check failed")
            result.append(value)
        return result

    def _record_exists(self, record_type: str, record_id: str) -> bool:
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            return connection.execute(
                "select 1 from skill_improvement_records "
                "where record_type = ? and record_id = ? limit 1",
                (record_type, record_id),
            ).fetchone() is not None

    def _project_is_trusted(self, project_id: str, workspace: str | Path) -> bool:
        """Require the same durable project registration used by delivery."""

        try:
            from .paths import AllowedRoots, allowed_root_id, repository_fingerprint

            with sqlite3.connect(self.database_path, timeout=30) as connection:
                row = connection.execute(
                    "select real_path, trust_state, repo_fingerprint, allowed_root_id "
                    "from projects where id = ?",
                    (project_id,),
                ).fetchone()
        except (ImportError, OSError, RuntimeError, ValueError, sqlite3.Error):
            return False
        if row is None:
            return False
        real_path, trust_state, fingerprint, root_id = row
        project = Path(workspace).expanduser().resolve()
        configured = os.environ.get("DEVPILOT_ALLOWED_ROOTS", "")
        roots = AllowedRoots.from_paths(
            tuple(item for item in configured.split(os.pathsep) if item.strip())
            or (project,)
        )
        expected_root_id = allowed_root_id(roots.root_for(project))
        return (
            str(Path(real_path).expanduser().resolve()) == str(project)
            and trust_state == "trusted"
            and isinstance(fingerprint, str)
            and fingerprint == repository_fingerprint(project)
            and isinstance(root_id, str)
            and root_id == expected_root_id
        )

    def _has_trusted_verification_fact(
        self,
        *,
        verification_ref: str,
        origin_run_id: str,
        change_set_id: str,
        project_id: str,
        skill_id: str,
        task_id: str | None = None,
        workspace: str | None = None,
    ) -> bool:
        """Check the server-owned Run/Event/ChangeSet facts behind a receipt."""
        if verification_ref != f"verification-{origin_run_id}:{change_set_id}":
            return False
        try:
            with sqlite3.connect(self.database_path, timeout=30) as connection:
                run = connection.execute(
                    """
                    select r.project_id, r.status, cs.task_id, cs.project_path,
                           p.real_path, cs.status, cs.project_id
                    from runs r
                    left join change_sets cs on cs.id = ? and cs.run_id = r.id
                    left join projects p on p.id = r.project_id
                    where r.id = ?
                    """,
                    (change_set_id, origin_run_id),
                ).fetchone()
                if (
                    run is None
                    or run[0] != project_id
                    or run[1] != "failed"
                    or run[5] != "verification_failed"
                    or run[6] != project_id
                    or (task_id is not None and run[2] != task_id)
                    or (
                        workspace is not None
                        and (
                            run[3] is None
                            and run[4] is None
                            or str(Path(run[3] or run[4]).expanduser().resolve()) != workspace
                        )
                    )
                ):
                    return False
                events = connection.execute(
                    "select event_type, payload_json from run_events where run_id = ?",
                    (origin_run_id,),
                ).fetchall()
                event_types = {str(row[0]) for row in events}
                if "changeset.applied" not in event_types:
                    return False
                if "changeset.verification_failed" not in event_types:
                    return False
                applied_events = [
                    json.loads(str(row[1]))
                    for row in events
                    if row[0] == "changeset.applied"
                ]
                verification_events = [
                    json.loads(str(row[1]))
                    for row in events
                    if row[0] == "changeset.verification_failed"
                ]
                if not any(
                    isinstance(payload, dict)
                    and payload.get("change_set_id") == change_set_id
                    for payload in applied_events
                ):
                    return False
                if not any(
                    isinstance(payload, dict)
                    and payload.get("change_set_id") == change_set_id
                    and payload.get("failure_kind") == "semantic"
                    for payload in verification_events
                ):
                    return False
                approval = connection.execute(
                    """
                    select 1 from approvals
                    where change_set_id = ? and capability = 'r1.apply'
                      and status = 'consumed' and consumed_at is not null
                    limit 1
                    """,
                    (change_set_id,),
                ).fetchone()
                approved_apply = approval is not None or any(
                    payload.get("execution_mode") == "full_access"
                    for payload in applied_events
                    if isinstance(payload, dict)
                )
                if not approved_apply:
                    return False
                proposed = {
                    "feature-delivery": "feature_delivery.proposed",
                    "bug-fix": "bug_fix.proposed",
                }.get(skill_id)
                skill_bound_verification = any(
                    isinstance(payload, dict) and payload.get("skill_id") == skill_id
                    for payload in verification_events
                )
                if (
                    proposed is not None
                    and proposed not in event_types
                    and not skill_bound_verification
                ):
                    return False
                return True
        except sqlite3.Error:
            return False

    def _has_trusted_failure_fact(
        self,
        *,
        failure_ref: str,
        project_id: str,
        expected_behavior: str | None = None,
        origin_run_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        workspace: str | None = None,
    ) -> bool:
        if not _TRUSTED_FAILURE_REF_RE.fullmatch(failure_ref):
            return False
        run_id = failure_ref.removeprefix("failure-")
        try:
            with sqlite3.connect(self.database_path, timeout=30) as connection:
                row = connection.execute(
                    """
                    select r.project_id, r.kind, r.status, p.real_path, e.event_type, e.payload_json
                    from runs r join run_events e on e.run_id = r.id
                    join projects p on p.id = r.project_id
                    where r.id = ? and r.project_id = ?
                    """,
                    (run_id, project_id),
                ).fetchall()
                trusted = any(
                    kind in {"bug-check", "test-check"}
                    and status == "failed"
                    and (
                        workspace is None
                        or str(Path(real_path).expanduser().resolve()) == workspace
                    )
                    and event_type in {"bug_check.failed", "test_check.failed"}
                    and (
                        expected_behavior is None
                        or json.loads(str(payload)).get("expected_behavior")
                        == expected_behavior
                    )
                    for _project, kind, status, real_path, event_type, payload in row
                )
                if not trusted:
                    return False
                if any(value is not None for value in (task_id, session_id, user_id)):
                    bound = connection.execute(
                        "select payload_json from run_events "
                        "where run_id = ? and event_type = 'run.context.bound'",
                        (run_id,),
                    ).fetchall()
                    if not any(
                        isinstance(decoded := json.loads(str(payload)), dict)
                        and decoded.get("task_id") == task_id
                        and decoded.get("session_id") == session_id
                        and decoded.get("user_id") == user_id
                        and (workspace is None or decoded.get("workspace") == workspace)
                        for (payload,) in bound
                    ):
                        return False
                if origin_run_id is None:
                    return True
                proposed_events = connection.execute(
                    "select event_type, payload_json from run_events where run_id = ?",
                    (origin_run_id,),
                ).fetchall()
                return any(
                    event_type == "bug_fix.proposed"
                    and isinstance(decoded := json.loads(str(payload_json)), dict)
                    and (
                        failure_ref in decoded.get("failure_evidence_ids", [])
                        or failure_ref.removeprefix("failure-")
                        in decoded.get("failure_evidence_ids", [])
                    )
                    for event_type, payload_json in proposed_events
                )
        except sqlite3.Error:
            return False

    def _has_repo_analysis_fact(
        self,
        *,
        origin_run_id: str,
        project_id: str,
        workspace: str,
        expected_behavior: str,
        task_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> bool:
        """Validate a repo-analysis fact against the server-owned Run events."""
        try:
            with sqlite3.connect(self.database_path, timeout=30) as connection:
                run = connection.execute(
                    """
                    select r.project_id, r.kind, p.real_path
                    from runs r join projects p on p.id = r.project_id
                    where r.id = ?
                    """,
                    (origin_run_id,),
                ).fetchone()
                if (
                    run is None
                    or run[0] != project_id
                    or run[1] != "repo-analysis"
                    or str(Path(run[2]).expanduser().resolve()) != workspace
                ):
                    return False
                events = connection.execute(
                    "select event_type, payload_json from run_events where run_id = ?",
                    (origin_run_id,),
                ).fetchall()
                if any(value is not None for value in (task_id, session_id, user_id)):
                    bound = [
                        json.loads(str(payload))
                        for event_type, payload in events
                        if event_type == "run.context.bound"
                    ]
                    if not any(
                        isinstance(payload, dict)
                        and payload.get("task_id") == task_id
                        and payload.get("session_id") == session_id
                        and payload.get("user_id") == user_id
                        and payload.get("workspace") == workspace
                        for payload in bound
                    ):
                        return False
                allowed = {
                    "tool.completed",
                    "evidence.inspect.completed",
                    "evidence.search.completed",
                    "evidence.read.completed",
                }
                decoded_events = [
                    (event_type, json.loads(str(payload)))
                    for event_type, payload in events
                ]
                if any(
                    ref != origin_run_id
                    and not ref.startswith("local:")
                    and not any(
                        isinstance(payload, dict)
                        and ref in payload.get("evidence_refs", [])
                        for _event_type, payload in decoded_events
                    )
                    for ref in evidence_refs
                ):
                    return False
                return any(
                    event_type in allowed and expected_behavior in str(payload)
                    for event_type, payload in events
                )
        except sqlite3.Error:
            return False

    @staticmethod
    def _namespace(path: Path) -> tuple[str, str | None]:
        if path.name == "feedback.jsonl":
            return "feedback", None
        if path.name == "activations.jsonl":
            return "activation", None
        namespace = path.parent.name
        return {
            "candidates": "candidate",
            "batches": "batch",
            "evaluations": "evaluation",
            "confirmations": "confirmation",
            "task-snapshots": "snapshot",
            "improvement-cases": "improvement_case",
            "replay-trials": "replay_trial",
        }.get(namespace, "generic"), path.stem

    @classmethod
    def from_environment(cls, workspace: str | Path | None = None) -> SkillImprovementStore:
        # Keep the service-owned records outside the agent-visible workspace.
        # An explicit DEVPILOT_DATA_DIR is still accepted only when it passes
        # the constructor's workspace-boundary check.
        default = Path.home() / ".nanobot-skill-improvement"
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
        workspace: str | Path,
        user_id: str,
        session_id: str,
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
        workspace_value = str(Path(workspace).expanduser().resolve())
        if self.workspace is not None and workspace_value != str(self.workspace):
            raise SkillImprovementError("feedback workspace does not match the store")
        skill_improvement_target(skill_id)
        if len(skill_hash) != 64 or any(c not in "0123456789abcdef" for c in skill_hash.lower()):
            raise SkillImprovementError("skill_hash must be a SHA-256 digest")
        refs = _refs(evidence_refs)
        if source_type == "model_inference" and len(refs) < 2:
            raise SkillImprovementError(
                "model inference requires two independent evidence references"
            )
        if source_type in {
            "model_inference",
            "external_content",
            "trusted_verification",
            "trusted_failure_evidence",
        } and not all(_EVIDENCE_REF_RE.fullmatch(item) for item in refs):
            raise SkillImprovementError("verification references must be traceable IDs")
        if source_type == "trusted_verification" and not any(
            _TRUSTED_VERIFICATION_REF_RE.fullmatch(item) for item in refs
        ):
            raise SkillImprovementError(
                "trusted verification requires a server verification reference"
            )
        if source_type == "trusted_verification":
            if not any(
                self._has_trusted_verification_fact(
                    verification_ref=item,
                    origin_run_id=_trusted_verification_parts(item)[0],
                    change_set_id=_trusted_verification_parts(item)[1],
                    project_id=project_id,
                    skill_id=skill_id,
                    task_id=_bounded_text(task_id, field="task_id", limit=128),
                    workspace=workspace_value,
                )
                for item in refs
                if _trusted_verification_parts(item) is not None
            ):
                raise SkillImprovementError(
                "trusted verification reference is not a server-owned fact"
            )
        if source_type == "trusted_failure_evidence" and not any(
            self._has_trusted_failure_fact(failure_ref=item, project_id=project_id)
            for item in refs
        ):
            raise SkillImprovementError("failure reference is not a server-owned fact")
        state = (
            "verified"
            if source_type in {
                "user_feedback",
                "tool_result",
                "trusted_verification",
                "trusted_failure_evidence",
            }
            else "pending_verification"
        )
        evidence = FeedbackEvidence(
            evidence_id=f"feedback-{uuid.uuid4()}",
            task_id=_bounded_text(task_id, field="task_id", limit=128),
            project_id=_bounded_text(project_id, field="project_id", limit=128),
            workspace=workspace_value,
            user_id=_bounded_text(user_id, field="user_id", limit=128),
            session_id=_bounded_text(session_id, field="session_id", limit=128),
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
        if evidence.processing_state == "deleted":
            raise SkillImprovementError("deleted feedback cannot be verified")
        refs = tuple(dict.fromkeys((*evidence.evidence_refs, *_refs(independent_refs))))
        if evidence.source_type in {"model_inference", "external_content"} and len(refs) < 2:
            raise SkillImprovementError("independent verification evidence is required")
        if evidence.source_type in {
            "model_inference",
            "external_content",
            "trusted_verification",
            "trusted_failure_evidence",
        } and not all(_EVIDENCE_REF_RE.fullmatch(item) for item in refs):
            raise SkillImprovementError("verification references must be traceable IDs")
        if evidence.source_type == "trusted_verification" and not any(
            _TRUSTED_VERIFICATION_REF_RE.fullmatch(item) for item in refs
        ):
            raise SkillImprovementError(
                "trusted verification requires a server verification reference"
            )
        if evidence.source_type == "trusted_failure_evidence" and not any(
            _TRUSTED_FAILURE_REF_RE.fullmatch(item) for item in refs
        ):
            raise SkillImprovementError("failure evidence requires a server failure reference")
        updated = FeedbackEvidence(
            **{
                **evidence.to_dict(),
                "evidence_refs": refs,
                "processing_state": "verified",
            }
        )
        self._append_jsonl(self.feedback_file, updated.to_dict())
        return updated

    def delete_feedback(
        self,
        evidence_id: str,
        *,
        workspace: str | Path,
        user_id: str,
        project_id: str,
    ) -> FeedbackEvidence:
        """Tombstone feedback and invalidate candidates that used it."""
        workspace_value = str(Path(workspace).expanduser().resolve())
        with self._lock, self._process_lock():
            evidence = self.get_feedback(evidence_id)
            if evidence is None:
                raise SkillImprovementError("feedback evidence not found")
            if (
                evidence.workspace != workspace_value
                or evidence.user_id != _bounded_text(user_id, field="user_id", limit=128)
                or evidence.project_id != _bounded_text(project_id, field="project_id", limit=128)
            ):
                raise SkillImprovementError("feedback deletion scope mismatch")
            deleted = FeedbackEvidence(
                **{
                    **evidence.to_dict(),
                    "correction": "[deleted]",
                    "actual_result": "[deleted]",
                    "evidence_refs": (),
                    "processing_state": "deleted",
                }
            )
            # Remove every historical payload before retaining a minimal,
            # redacted tombstone.  A logical append-only tombstone alone
            # would leave the user's original correction/result/reference in
            # the SQLite file.
            self._delete_record_history("feedback", evidence_id)
            self._append_jsonl(self.feedback_file, deleted.to_dict())
            for candidate in self.list_candidates(workspace=workspace):
                linked_case = any(
                    evidence_id in case.evidence_refs
                    for case in self.list_improvement_cases(
                        workspace=workspace_value,
                        project_id=evidence.project_id,
                        user_id=evidence.user_id,
                    )
                    if case.case_id in candidate.case_ids
                )
                if evidence_id not in candidate.evidence_ids and not linked_case:
                    continue
                invalidated_content = "[deleted candidate content]"
                self._atomic_write(
                    self.candidates_dir / f"{candidate.candidate_id}.json",
                    {
                        **candidate.to_dict(),
                        "candidate_content": invalidated_content,
                        "candidate_hash": content_sha256(invalidated_content),
                        "reason": "[deleted evidence]",
                        "expected_behavior": "[deleted evidence]",
                        "status": "stale",
                        "result_refs": (),
                    },
                )
            for case in self.list_improvement_cases(
                workspace=workspace_value,
                project_id=evidence.project_id,
                user_id=evidence.user_id,
            ):
                if evidence_id not in case.evidence_refs:
                    continue
                self._atomic_write(
                    self.cases_dir / f"{case.case_id}.json",
                    {
                        **case.to_dict(),
                        "evidence_refs": [],
                        "replay_snapshot_ref": None,
                        "selected_batch_id": None,
                        "status": "not_replayable",
                    },
                )
            return deleted

    def get_feedback(
        self,
        evidence_id: str,
        *,
        workspace: str | Path | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> FeedbackEvidence | None:
        latest: dict[str, Any] | None = None
        for record in self._read_jsonl(self.feedback_file):
            if record.get("evidence_id") == evidence_id:
                latest = record
        if latest is None:
            return None
        evidence = FeedbackEvidence(
            evidence_id=str(latest["evidence_id"]),
            task_id=str(latest["task_id"]),
            project_id=str(latest["project_id"]),
            workspace=str(latest["workspace"]),
            user_id=str(latest["user_id"]),
            session_id=str(latest["session_id"]),
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
        if workspace is not None and evidence.workspace != str(
            Path(workspace).expanduser().resolve()
        ):
            return None
        if user_id is not None and evidence.user_id != _bounded_text(
            user_id, field="user_id", limit=128
        ):
            return None
        if project_id is not None and evidence.project_id != _bounded_text(
            project_id, field="project_id", limit=128
        ):
            return None
        return evidence

    def list_feedback(
        self,
        *,
        workspace: str | Path | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        skill_id: str | None = None,
        processing_state: str | None = None,
    ) -> list[FeedbackEvidence]:
        """Return the latest version of each evidence record in scope."""
        latest_ids = {
            value.get("evidence_id")
            for value in self._read_jsonl(self.feedback_file)
            if isinstance(value.get("evidence_id"), str)
        }
        records: list[FeedbackEvidence] = []
        for evidence_id in sorted(latest_ids):
            evidence = self.get_feedback(
                evidence_id,
                workspace=workspace,
                user_id=user_id,
                project_id=project_id,
            )
            if evidence is None:
                continue
            if skill_id is not None and evidence.skill_id != skill_id:
                continue
            if processing_state is not None and evidence.processing_state != processing_state:
                continue
            records.append(evidence)
        return sorted(records, key=lambda item: (item.occurred_at, item.evidence_id))

    def create_improvement_case(
        self,
        *,
        skill_id: str,
        profile_binding: str,
        origin_run_id: str,
        task_id: str,
        project_id: str,
        workspace: str | Path,
        user_id: str,
        session_id: str,
        base_skill_hash: str,
        trigger_type: str,
        trigger_summary: str,
        expected_behavior: str,
        evidence_refs: object = (),
        replay_snapshot_ref: str | None = None,
        assertion_spec: Mapping[str, object] | None = None,
        status: str = "eligible",
        selected_batch_id: str | None = None,
    ) -> ImprovementCase:
        """Persist one server-validated case without invoking a model."""
        target = skill_improvement_target(skill_id)
        if profile_binding != target.profile_binding:
            raise SkillImprovementError("improvement case profile binding does not match target")
        if trigger_type not in target.allowed_trigger_types:
            raise SkillImprovementError("trigger type is not allowed for this Skill target")
        if status not in IMPROVEMENT_CASE_STATES:
            raise SkillImprovementError("invalid improvement case status")
        if not _is_sha256(base_skill_hash):
            raise SkillImprovementError("base_skill_hash must be a SHA-256 digest")
        workspace_value = str(Path(workspace).expanduser().resolve())
        if self.workspace is not None and workspace_value != str(self.workspace):
            raise SkillImprovementError("improvement case workspace does not match the store")
        effective = self.read_effective_snapshot(workspace, skill_id=skill_id)
        base_hash = base_skill_hash.lower()
        if effective.content_hash != base_hash:
            raise SkillImprovementError("improvement case baseline does not match effective Skill")
        origin_value = _bounded_text(origin_run_id, field="origin_run_id", limit=128)
        if not _RUN_ID_RE.fullmatch(origin_value):
            raise SkillImprovementError("origin_run_id must be a run ID")
        task_value = _bounded_text(task_id, field="task_id", limit=128)
        project_value = _bounded_text(project_id, field="project_id", limit=128)
        user_value = _bounded_text(user_id, field="user_id", limit=128)
        session_value = _bounded_text(session_id, field="session_id", limit=128)
        refs = _refs(evidence_refs)
        if not refs:
            raise SkillImprovementError("at least one evidence reference is required")
        if not all(_traceable_ref(ref, origin_run_id=origin_value) for ref in refs):
            raise SkillImprovementError("evidence references must be traceable IDs")
        matched_evidence: list[FeedbackEvidence] = []
        for ref in refs:
            matches = [
                item
                for item in self.list_feedback(
                    workspace=workspace_value,
                    user_id=user_value,
                    project_id=project_value,
                )
                if item.evidence_id == ref or ref in item.evidence_refs
            ]
            if not matches:
                raise SkillImprovementError(f"evidence reference is unavailable: {ref}")
            matched_evidence.extend(matches)
        unique_evidence = {
            item.evidence_id: item for item in matched_evidence
        }
        if any(item.processing_state != "verified" for item in unique_evidence.values()):
            raise SkillImprovementError("improvement case evidence must be verified")
        allowed_sources = (
            {"user_feedback"}
            if trigger_type == "user_confirmed_missing_fact"
            else (
                {"trusted_verification"}
                if skill_id == "feature-delivery"
                else {"trusted_verification", "trusted_failure_evidence"}
            )
        )
        if any(item.source_type not in allowed_sources for item in unique_evidence.values()):
            raise SkillImprovementError(
                "improvement case evidence source is not trusted for this trigger"
            )
        if trigger_type == "trusted_verification_failed" and not any(
            any(
                _trusted_verification_parts(ref) is not None
                and _trusted_verification_parts(ref)[0] == origin_value
                and self._has_trusted_verification_fact(
                    verification_ref=ref,
                    origin_run_id=_trusted_verification_parts(ref)[0],
                    change_set_id=_trusted_verification_parts(ref)[1],
                    project_id=project_value,
                    skill_id=skill_id,
                    task_id=task_value,
                    workspace=workspace_value,
                )
                for ref in item.evidence_refs
            )
            for item in unique_evidence.values()
        ):
            raise SkillImprovementError(
                "trusted verification case requires a server verification reference"
            )
        if any(
            item.skill_id != skill_id
            or item.skill_hash != base_hash
            or item.task_id != task_value
            or item.session_id != session_value
            or item.workspace != workspace_value
            or item.project_id != project_value
            or item.user_id != user_value
            for item in unique_evidence.values()
        ):
            raise SkillImprovementError("improvement case evidence binding mismatch")
        if skill_id == "bug-fix" and not any(
            item.source_type == "trusted_failure_evidence"
            and any(
                self._has_trusted_failure_fact(
                    failure_ref=ref,
                    project_id=project_value,
                    origin_run_id=origin_value,
                    task_id=task_value,
                    session_id=session_value,
                    user_id=user_value,
                    workspace=workspace_value,
                )
                for ref in item.evidence_refs
            )
            for item in unique_evidence.values()
        ):
            raise SkillImprovementError("bug-fix case requires original failure evidence")
        origin_ref = f"run-{origin_value}"
        if not any(
            origin_value == item.evidence_id
            or origin_value in item.evidence_refs
            or origin_ref in item.evidence_refs
            for item in unique_evidence.values()
        ):
            raise SkillImprovementError("origin run is not present in the verified evidence")
        refs = tuple(dict.fromkeys((*refs, *sorted(unique_evidence))))
        summary = _bounded_text(trigger_summary, field="trigger_summary")
        expected = _bounded_text(expected_behavior, field="expected_behavior")
        snapshot_ref = (
            _bounded_text(replay_snapshot_ref, field="replay_snapshot_ref", limit=256)
            if replay_snapshot_ref is not None
            else None
        )
        if status == "eligible" and snapshot_ref is None:
            raise SkillImprovementError("eligible improvement cases require a replay snapshot")
        if snapshot_ref is not None:
            snapshot = self.get_task_snapshot(
                snapshot_ref,
                workspace=workspace_value,
                project_id=project_value,
                session_id=session_value,
                skill_id=skill_id,
            )
            if snapshot is None or snapshot.task_id != task_value:
                raise SkillImprovementError("improvement case replay snapshot binding mismatch")
            if snapshot.content_hash != base_hash:
                raise SkillImprovementError("improvement case snapshot does not match baseline")
            if snapshot.replayable is not True:
                status = "not_replayable"
        assertion = _json_safe(assertion_spec or {})
        identity = json.dumps(
            {
                "skill_id": skill_id,
                "profile_binding": profile_binding,
                "origin_run_id": origin_value,
                "task_id": task_value,
                "project_id": project_value,
                "workspace": workspace_value,
                "user_id": user_value,
                "session_id": session_value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        case_id = f"case-{content_sha256(identity)[:32]}"
        with self._lock, self._process_lock():
            locked_effective = self.read_effective_snapshot(workspace, skill_id=skill_id)
            if locked_effective.content_hash != base_hash:
                raise SkillImprovementError("improvement case baseline changed during creation")
            locked_evidence = {
                item.evidence_id: item
                for item in self.list_feedback(
                    workspace=workspace_value,
                    user_id=user_value,
                    project_id=project_value,
                    skill_id=skill_id,
                    processing_state="verified",
                )
            }
            if any(
                evidence_id not in locked_evidence
                or locked_evidence[evidence_id].skill_hash != base_hash
                or locked_evidence[evidence_id].task_id != task_value
                or locked_evidence[evidence_id].session_id != session_value
                or locked_evidence[evidence_id].source_type not in allowed_sources
                or not (
                    origin_value == locked_evidence[evidence_id].evidence_id
                    or origin_value in locked_evidence[evidence_id].evidence_refs
                    or f"run-{origin_value}" in locked_evidence[evidence_id].evidence_refs
                )
                for evidence_id in unique_evidence
            ):
                raise SkillImprovementError("improvement case evidence changed during creation")
            if trigger_type == "trusted_verification_failed" and not any(
                _trusted_verification_parts(ref) is not None
                and _trusted_verification_parts(ref)[0] == origin_value
                and self._has_trusted_verification_fact(
                    verification_ref=ref,
                    origin_run_id=_trusted_verification_parts(ref)[0],
                    change_set_id=_trusted_verification_parts(ref)[1],
                    project_id=project_value,
                    skill_id=skill_id,
                    task_id=task_value,
                    workspace=workspace_value,
                )
                for item in locked_evidence.values()
                for ref in item.evidence_refs
                if _TRUSTED_VERIFICATION_REF_RE.fullmatch(ref)
            ):
                raise SkillImprovementError(
                    "trusted verification changed during case creation"
                )
            if skill_id == "bug-fix" and not any(
                item.source_type == "trusted_failure_evidence"
                and any(
                    self._has_trusted_failure_fact(
                        failure_ref=ref,
                        project_id=project_value,
                        origin_run_id=origin_value,
                        task_id=task_value,
                        session_id=session_value,
                        user_id=user_value,
                        workspace=workspace_value,
                    )
                    for ref in item.evidence_refs
                )
                for item in locked_evidence.values()
            ):
                raise SkillImprovementError(
                    "original failure evidence changed during case creation"
                )
            if snapshot_ref is not None:
                locked_snapshot = self._get_task_snapshot_unlocked(
                    snapshot_ref,
                    workspace=workspace_value,
                    project_id=project_value,
                    session_id=session_value,
                    skill_id=skill_id,
                )
                if (
                    locked_snapshot is None
                    or locked_snapshot.task_id != task_value
                    or locked_snapshot.content_hash != base_hash
                ):
                    raise SkillImprovementError(
                        "improvement case snapshot changed during creation"
                    )
            if self._record_exists("improvement_case", case_id):
                existing = self.get_improvement_case(case_id)
                if existing is None:
                    raise SkillImprovementError("improvement case record is unavailable")
                if (
                    existing.base_skill_hash != base_hash
                    or existing.trigger_type != trigger_type
                    or existing.trigger_summary != summary
                    or existing.expected_behavior != expected
                    or existing.evidence_refs != refs
                    or existing.replay_snapshot_ref != snapshot_ref
                    or existing.assertion_spec != assertion
                ):
                    raise SkillImprovementError(
                        "improvement case conflicts with existing origin_run_id"
                    )
                return existing
            case = ImprovementCase(
                case_id=case_id,
                skill_id=skill_id,
                profile_binding=profile_binding,
                origin_run_id=origin_value,
                task_id=task_value,
                project_id=project_value,
                workspace=workspace_value,
                user_id=user_value,
                session_id=session_value,
                base_skill_hash=base_hash,
                trigger_type=trigger_type,
                trigger_summary=summary,
                expected_behavior=expected,
                evidence_refs=refs,
                replay_snapshot_ref=snapshot_ref,
                assertion_spec=assertion,
                status=status,
                selected_batch_id=(
                    _bounded_text(selected_batch_id, field="selected_batch_id", limit=128)
                    if selected_batch_id is not None
                    else None
                ),
                created_at=_now(),
            )
            self._atomic_write(self.cases_dir / f"{case.case_id}.json", case.to_dict())
            return case

    def get_improvement_case(
        self,
        case_id: str,
        *,
        workspace: str | Path | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> ImprovementCase | None:
        if not _record_id_matches(case_id, "case"):
            return None
        path = self.cases_dir / f"{case_id}.json"
        if not self._record_exists("improvement_case", case_id):
            return None
        raw = self._read_object(path, "improvement case")
        case = _improvement_case_from_dict(raw)
        if workspace is not None and case.workspace != str(Path(workspace).expanduser().resolve()):
            return None
        if project_id is not None and case.project_id != _bounded_text(
            project_id, field="project_id", limit=128
        ):
            return None
        if user_id is not None and case.user_id != _bounded_text(
            user_id, field="user_id", limit=128
        ):
            return None
        return case

    def list_improvement_cases(
        self,
        *,
        workspace: str | Path | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        skill_id: str | None = None,
        status: str | None = None,
    ) -> list[ImprovementCase]:
        workspace_value = (
            str(Path(workspace).expanduser().resolve()) if workspace is not None else None
        )
        cases: list[ImprovementCase] = []
        for raw in self._record_rows("improvement_case"):
            case = _improvement_case_from_dict(raw)
            if workspace_value is not None and case.workspace != workspace_value:
                continue
            if project_id is not None and case.project_id != project_id:
                continue
            if user_id is not None and case.user_id != user_id:
                continue
            if skill_id is not None and case.skill_id != skill_id:
                continue
            if status == "eligible" and not self._case_is_replayable(case):
                continue
            if status is not None and case.status != status:
                continue
            cases.append(case)
        return sorted(cases, key=lambda item: (item.created_at, item.case_id))

    def _case_is_replayable(self, case: ImprovementCase) -> bool:
        """Require a current, explicit replay proof for every execution gate."""

        if case.status in {"not_replayable", "ignored"} or case.replay_snapshot_ref is None:
            return False
        snapshot = self.get_task_snapshot(
            case.replay_snapshot_ref,
            workspace=case.workspace,
            project_id=case.project_id,
            session_id=case.session_id,
            skill_id=case.skill_id,
        )
        return snapshot is not None and snapshot.replayable is True

    def _evidence_has_non_replayable_case(
        self,
        evidence_id: str,
        *,
        workspace: str,
        project_id: str,
        user_id: str,
        skill_id: str,
    ) -> bool:
        return any(
            not self._case_is_replayable(case) and evidence_id in case.evidence_refs
            for case in self.list_improvement_cases(
                workspace=workspace,
                project_id=project_id,
                user_id=user_id,
                skill_id=skill_id,
            )
        )

    def record_replay_trial(
        self,
        *,
        case_id: str,
        skill_id: str,
        skill_hash: str,
        version: str,
        model_id: str,
        parameters: Mapping[str, object] | None,
        snapshot_ref: str,
        started_at: str,
        finished_at: str,
        token_count: int,
        duration_seconds: float,
        tool_trace_ref: str,
        final_output_ref: str,
        assertion_result: Mapping[str, object] | None,
        status: str,
        trial_id: str | None = None,
        candidate_id: str | None = None,
    ) -> ReplayTrial:
        """Append one immutable replay result bound to an existing case."""
        case = self.get_improvement_case(case_id)
        if case is None:
            raise SkillImprovementError("replay trial case was not found")
        if case.status in {"not_replayable", "ignored"}:
            raise SkillImprovementError("replay trial case is no longer replayable")
        if case.skill_id != skill_id:
            raise SkillImprovementError("replay trial Skill does not match its case")
        if not _is_sha256(skill_hash):
            raise SkillImprovementError("skill_hash must be a SHA-256 digest")
        if version not in {"baseline", "candidate"}:
            raise SkillImprovementError("replay trial version must be baseline or candidate")
        if status not in REPLAY_TRIAL_STATUSES:
            raise SkillImprovementError("invalid replay trial status")
        if token_count < 0:
            raise SkillImprovementError("token_count must be a non-negative integer")
        if duration_seconds < 0:
            raise SkillImprovementError("duration_seconds must be non-negative")
        if case.replay_snapshot_ref is None or snapshot_ref != case.replay_snapshot_ref:
            raise SkillImprovementError("replay trial snapshot does not match its case")
        snapshot = self.get_task_snapshot(
            snapshot_ref,
            workspace=case.workspace,
            project_id=case.project_id,
            session_id=case.session_id,
            skill_id=skill_id,
        )
        if snapshot is None or snapshot.task_id != case.task_id:
            raise SkillImprovementError("replay trial snapshot binding mismatch")
        if snapshot.content_hash != case.base_skill_hash:
            raise SkillImprovementError("replay trial snapshot hash mismatch")
        if snapshot.replayable is not True:
            raise SkillImprovementError("replay trial snapshot is not replayable")
        if version == "baseline" and skill_hash.lower() != case.base_skill_hash:
            raise SkillImprovementError("baseline trial hash does not match its case")
        if version == "candidate":
            if candidate_id is None or not _record_id_matches(candidate_id, "candidate"):
                raise SkillImprovementError("candidate replay trials require a candidate ID")
            candidate = self.get_candidate(
                candidate_id,
                workspace=case.workspace,
                user_id=case.user_id,
                project_id=case.project_id,
            )
            if (
                candidate is None
                or case_id not in candidate.case_ids
                or candidate.skill_id != skill_id
                or candidate.candidate_hash != skill_hash.lower()
            ):
                raise SkillImprovementError("candidate trial is not bound to its case")
        assertion = _json_safe(assertion_result or {})
        target = skill_improvement_target(skill_id)
        if status in {"passed", "failed"} and (
            assertion.get("grader_kind") != target.grader_kind
            or not isinstance(assertion.get("passed"), bool)
            or (status == "passed") != assertion["passed"]
        ):
            raise SkillImprovementError("replay trial assertion is not bound to its grader")
        trial_value = (
            _bounded_text(trial_id, field="trial_id", limit=128)
            if trial_id is not None
            else f"trial-{uuid.uuid4()}"
        )
        if not _record_id_matches(trial_value, "trial"):
            raise SkillImprovementError("trial_id must be a traceable trial ID")
        trial = ReplayTrial(
            trial_id=trial_value,
            case_id=case_id,
            skill_id=skill_id,
            skill_hash=skill_hash.lower(),
            version=version,
            model_id=_bounded_text(model_id, field="model_id", limit=128),
            parameters=_json_safe(parameters or {}),
            snapshot_ref=_bounded_text(snapshot_ref, field="snapshot_ref", limit=256),
            started_at=_bounded_text(started_at, field="started_at", limit=64),
            finished_at=_bounded_text(finished_at, field="finished_at", limit=64),
            token_count=token_count,
            duration_seconds=float(duration_seconds),
            tool_trace_ref=_bounded_text(tool_trace_ref, field="tool_trace_ref", limit=256),
            final_output_ref=_bounded_text(final_output_ref, field="final_output_ref", limit=256),
            assertion_result=assertion,
            status=status,
            candidate_id=candidate_id,
        )
        with self._lock, self._process_lock():
            locked_case = self.get_improvement_case(case_id)
            if (
                locked_case is None
                or locked_case.status in {"not_replayable", "ignored"}
                or locked_case.skill_id != skill_id
                or locked_case.replay_snapshot_ref != snapshot_ref
            ):
                raise SkillImprovementError("replay trial case changed during recording")
            locked_snapshot = self._get_task_snapshot_unlocked(
                snapshot_ref,
                workspace=locked_case.workspace,
                project_id=locked_case.project_id,
                session_id=locked_case.session_id,
                skill_id=skill_id,
            )
            if (
                locked_snapshot is None
                or locked_snapshot.task_id != locked_case.task_id
                or locked_snapshot.content_hash != locked_case.base_skill_hash
                or locked_snapshot.replayable is not True
            ):
                raise SkillImprovementError("replay trial snapshot changed during recording")
            if version == "baseline" and skill_hash.lower() != locked_case.base_skill_hash:
                raise SkillImprovementError("baseline trial hash changed during recording")
            if version == "candidate":
                locked_candidate = self.get_candidate(
                    candidate_id or "",
                    workspace=locked_case.workspace,
                    user_id=locked_case.user_id,
                    project_id=locked_case.project_id,
                )
                if (
                    locked_candidate is None
                    or case_id not in locked_candidate.case_ids
                    or locked_candidate.candidate_hash != skill_hash.lower()
                ):
                    raise SkillImprovementError("candidate trial changed during recording")
            if self._record_exists("replay_trial", trial.trial_id):
                raise SkillImprovementError("replay trial ID already exists")
            self._atomic_write(
                self.replay_trials_dir / f"{trial.trial_id}.json", trial.to_dict()
            )
        return trial

    def get_replay_trial(
        self,
        trial_id: str,
        *,
        workspace: str | Path | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> ReplayTrial | None:
        if workspace is None or project_id is None or user_id is None or session_id is None:
            raise SkillImprovementError("replay trial scope is required")
        if not _record_id_matches(trial_id, "trial"):
            return None
        if not self._record_exists("replay_trial", trial_id):
            return None
        raw = self._read_object(
            self.replay_trials_dir / f"{trial_id}.json", "replay trial"
        )
        trial = _replay_trial_from_dict(raw)
        case = self.get_improvement_case(trial.case_id)
        if case is None:
            return None
        if workspace is not None and case.workspace != str(Path(workspace).expanduser().resolve()):
            return None
        if project_id is not None and case.project_id != project_id:
            return None
        if user_id is not None and case.user_id != user_id:
            return None
        if session_id is not None and case.session_id != session_id:
            return None
        return trial

    def list_replay_trials(
        self,
        *,
        case_id: str | None = None,
        workspace: str | Path | None = None,
        project_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> list[ReplayTrial]:
        if workspace is None or project_id is None or user_id is None or session_id is None:
            raise SkillImprovementError("replay trial scope is required")
        trials = [
            _replay_trial_from_dict(raw)
            for raw in self._record_rows("replay_trial")
        ]
        if case_id is not None:
            trials = [item for item in trials if item.case_id == case_id]
        scoped: list[ReplayTrial] = []
        for trial in trials:
            if self.get_replay_trial(
                trial.trial_id,
                workspace=workspace,
                project_id=project_id,
                user_id=user_id,
                session_id=session_id,
            ) is not None:
                scoped.append(trial)
        return sorted(scoped, key=lambda item: (item.started_at, item.trial_id))

    def configure_scheduled_batch(
        self,
        *,
        batch_id: str,
        job_id: str,
        workspace: str | Path,
        project_id: str,
        user_id: str,
        session_key: str,
        enabled: bool = True,
        max_model_calls: int = 2,
        max_tokens: int = 12_000,
        max_duration_seconds: int = 600,
        notification_mode: str = "new_candidate_and_failure",
    ) -> ScheduledSkillBatch:
        """Create or update one explicit, bounded native-Cron batch policy."""
        if max_model_calls < 1 or max_model_calls > _MAX_SCHEDULED_MODEL_CALLS:
            raise SkillImprovementError("scheduled batch model-call budget must be 1 or 2")
        if max_tokens < 512 or max_tokens > _MAX_SCHEDULED_TOKENS:
            raise SkillImprovementError("scheduled batch token budget is out of bounds")
        if max_duration_seconds < 30 or max_duration_seconds > _MAX_SCHEDULED_DURATION_SECONDS:
            raise SkillImprovementError("scheduled batch duration must be 30-600 seconds")
        if notification_mode != "new_candidate_and_failure":
            raise SkillImprovementError("unsupported scheduled batch notification mode")
        workspace_value = str(Path(workspace).expanduser().resolve())
        batch_value = _bounded_text(batch_id, field="batch_id", limit=128)
        job_value = _bounded_text(job_id, field="job_id", limit=128)
        project_value = _bounded_text(project_id, field="project_id", limit=128)
        user_value = _bounded_text(user_id, field="user_id", limit=128)
        session_value = _bounded_text(session_key, field="session_key", limit=256)
        if not workspace_value:
            raise SkillImprovementError("scheduled batch workspace is required")
        path = self.batches_dir / f"{batch_value}.json"
        with self._lock, self._process_lock():
            existing = self.get_scheduled_batch(batch_value)
            if existing is not None:
                if (
                    existing.job_id != job_value
                    or existing.workspace != workspace_value
                    or existing.project_id != project_value
                    or existing.user_id != user_value
                    or existing.session_key != session_value
                    or existing.skill_id != TRIAL_SKILL_ID
                ):
                    raise SkillImprovementError("scheduled batch scope is immutable")
                now = _now()
                batch = replace(
                    existing,
                    enabled=enabled,
                    max_model_calls=max_model_calls,
                    max_tokens=max_tokens,
                    max_duration_seconds=max_duration_seconds,
                    notification_mode=notification_mode,
                    status=("idle" if enabled and existing.status == "disabled" else (
                        "disabled" if not enabled else existing.status
                    )),
                    lease_owner=None if not enabled else existing.lease_owner,
                    lease_expires_at=None if not enabled else existing.lease_expires_at,
                    updated_at=now,
                )
            else:
                now = _now()
                batch = ScheduledSkillBatch(
                    batch_id=batch_value,
                    job_id=job_value,
                    skill_id=TRIAL_SKILL_ID,
                    workspace=workspace_value,
                    project_id=project_value,
                    user_id=user_value,
                    session_key=session_value,
                    enabled=enabled,
                    read_scope="verified_feedback_only",
                    max_model_calls=max_model_calls,
                    max_tokens=max_tokens,
                    max_duration_seconds=max_duration_seconds,
                    notification_mode=notification_mode,
                    cursor_at=None,
                    processed_evidence_ids=(),
                    claimed_evidence_ids=(),
                    status="idle" if enabled else "disabled",
                    lease_owner=None,
                    lease_expires_at=None,
                    run_count=0,
                    candidate_attempts=0,
                    token_count=0,
                    consecutive_failures=0,
                    candidate_id=None,
                    last_run_at=None,
                    last_error=None,
                    last_notified_error=None,
                    last_candidate_ids=(),
                    created_at=now,
                    updated_at=now,
                )
            self._atomic_write(path, batch.to_dict())
            return batch

    def get_scheduled_batch(self, batch_id: str) -> ScheduledSkillBatch | None:
        """Read a scheduled batch without exposing private records to SkillsLoader."""
        batch_value = str(batch_id)
        if not _record_id_matches(batch_value, "batch"):
            return None
        path = self.batches_dir / f"{batch_value}.json"
        if not self._record_exists("batch", batch_value):
            return None
        return self._batch_from_dict(self._read_object(path, "scheduled batch"))

    def claim_scheduled_batch_error_notification(
        self,
        batch_id: str,
        *,
        error: str,
    ) -> bool:
        """Atomically claim one actionable error notification before delivery."""
        error_value = _bounded_text(error, field="last_notified_error", limit=1_000)
        with self._lock, self._process_lock():
            batch = self.get_scheduled_batch(batch_id)
            if batch is None:
                raise SkillImprovementError("scheduled batch was not found")
            if batch.last_notified_error == error_value:
                return False
            updated = replace(
                batch,
                last_notified_error=error_value,
                updated_at=_now(),
            )
            self._atomic_write(
                self.batches_dir / f"{batch.batch_id}.json", updated.to_dict()
            )
            return True

    def get_scheduled_batch_for_job(self, job_id: str) -> ScheduledSkillBatch | None:
        for raw in self._record_rows("batch"):
            if raw.get("job_id") == job_id:
                return self._batch_from_dict(raw)
        return None

    def set_scheduled_batch_enabled(
        self,
        batch_id: str,
        *,
        enabled: bool,
    ) -> ScheduledSkillBatch:
        with self._lock, self._process_lock():
            batch = self.get_scheduled_batch(batch_id)
            if batch is None:
                raise SkillImprovementError("scheduled batch not found")
            updated = replace(
                batch,
                enabled=enabled,
                status="idle" if enabled else "disabled",
                lease_owner=None if not enabled else batch.lease_owner,
                lease_expires_at=None if not enabled else batch.lease_expires_at,
                updated_at=_now(),
            )
            self._atomic_write(self.batches_dir / f"{batch.batch_id}.json", updated.to_dict())
            return updated

    def claim_scheduled_batch(
        self,
        job_id: str,
        *,
        lease_owner: str,
    ) -> ScheduledBatchClaim | None:
        """Atomically claim new verified evidence before any model invocation."""
        owner = _bounded_text(lease_owner, field="lease_owner", limit=128)
        with self._lock, self._process_lock():
            batch = self.get_scheduled_batch_for_job(job_id)
            if batch is None:
                return None
            if not batch.enabled:
                return ScheduledBatchClaim("disabled", batch)
            now = time.time()
            if batch.lease_owner and batch.lease_expires_at and batch.lease_expires_at > now:
                return ScheduledBatchClaim("busy", batch)
            if batch.status == "budget_exhausted":
                return ScheduledBatchClaim("budget_exhausted", batch)
            baseline = self.read_effective_snapshot(batch.workspace)
            processed = set(batch.processed_evidence_ids)
            if batch.status in {"running", "failed"}:
                candidate = (
                    self.get_candidate(
                        batch.candidate_id,
                        workspace=batch.workspace,
                        user_id=batch.user_id,
                        project_id=batch.project_id,
                    )
                    if batch.candidate_id
                    else None
                )
                if candidate is None:
                    # Candidate creation and batch registration are two durable
                    # records. A worker may die between those writes, so
                    # recover the orphan by its immutable batch/evidence scope
                    # before allowing another model attempt.
                    orphan_candidates = [
                        item
                        for item in self.list_candidates(
                            batch_id=batch.batch_id,
                            workspace=batch.workspace,
                            user_id=batch.user_id,
                            project_id=batch.project_id,
                        )
                        if item.status == "pending_evaluation"
                        and set(item.evidence_ids) == set(batch.claimed_evidence_ids)
                    ]
                    if orphan_candidates:
                        candidate = sorted(
                            orphan_candidates,
                            key=lambda item: (item.created_at, item.candidate_id),
                        )[-1]
                if (
                    candidate is not None
                    and batch.claimed_evidence_ids
                    and set(candidate.evidence_ids) == set(batch.claimed_evidence_ids)
                ):
                    evidence = [
                        self.get_feedback(item) for item in batch.claimed_evidence_ids
                    ]
                    valid_evidence = [item for item in evidence if item is not None]
                    processed.update(item.evidence_id for item in valid_evidence)
                    cursor_at = max(
                        (item.occurred_at for item in valid_evidence),
                        default=batch.cursor_at,
                    )
                    recovered = replace(
                        batch,
                        cursor_at=cursor_at,
                        processed_evidence_ids=tuple(sorted(processed))[
                            -_MAX_PROCESSED_EVIDENCE_IDS:
                        ],
                        claimed_evidence_ids=(),
                        status="candidate_created",
                        lease_owner=None,
                        lease_expires_at=None,
                        consecutive_failures=0,
                        candidate_id=candidate.candidate_id,
                        last_error=None,
                        last_candidate_ids=(candidate.candidate_id,),
                        updated_at=_now(),
                    )
                    self._atomic_write(
                        self.batches_dir / f"{batch.batch_id}.json", recovered.to_dict()
                    )
                    return ScheduledBatchClaim("candidate_created", recovered)
            evidence = [
                item
                for item in self.list_feedback(
                    workspace=batch.workspace,
                    user_id=batch.user_id,
                    project_id=batch.project_id,
                    skill_id=batch.skill_id,
                    processing_state="verified",
                )
                if item.skill_hash == baseline.content_hash
                and item.evidence_id not in processed
                and (batch.cursor_at is None or item.occurred_at >= batch.cursor_at)
                and not self._evidence_has_non_replayable_case(
                    item.evidence_id,
                    workspace=batch.workspace,
                    project_id=batch.project_id,
                    user_id=batch.user_id,
                    skill_id=batch.skill_id,
                )
            ][:_MAX_SCHEDULED_BATCH_EVIDENCE]
            if batch.status in {"running", "failed"} and (
                batch.candidate_attempts >= batch.max_model_calls
                or batch.token_count >= batch.max_tokens
                or batch.consecutive_failures >= batch.max_model_calls
            ):
                exhausted = replace(
                    batch,
                    status="budget_exhausted",
                    lease_owner=None,
                    lease_expires_at=None,
                    last_run_at=_now(),
                    last_error="scheduled batch retry budget exhausted",
                    updated_at=_now(),
                )
                self._atomic_write(
                    self.batches_dir / f"{batch.batch_id}.json", exhausted.to_dict()
                )
                return ScheduledBatchClaim("budget_exhausted", exhausted)
            if not evidence:
                updated = replace(
                    batch,
                    status="no_new_evidence",
                    last_run_at=_now(),
                    updated_at=_now(),
                )
                self._atomic_write(self.batches_dir / f"{batch.batch_id}.json", updated.to_dict())
                return ScheduledBatchClaim("no_new_evidence", updated)
            deadline = now + min(batch.max_duration_seconds, _MAX_SCHEDULED_DURATION_SECONDS)
            retrying = batch.status in {"running", "failed"}
            updated = replace(
                batch,
                status="running",
                lease_owner=owner,
                lease_expires_at=now + _SCHEDULED_LEASE_SECONDS,
                run_count=batch.run_count + 1,
                candidate_attempts=batch.candidate_attempts if retrying else 0,
                token_count=batch.token_count if retrying else 0,
                candidate_id=None,
                claimed_evidence_ids=tuple(item.evidence_id for item in evidence),
                last_run_at=_now(),
                consecutive_failures=batch.consecutive_failures if retrying else 0,
                updated_at=_now(),
            )
            self._atomic_write(self.batches_dir / f"{batch.batch_id}.json", updated.to_dict())
            return ScheduledBatchClaim(
                "claimed",
                updated,
                tuple(evidence),
                lease_owner=owner,
                deadline_at=deadline,
            )

    def reserve_scheduled_candidate_attempt(
        self,
        batch_id: str,
        *,
        lease_owner: str,
        estimated_tokens: int,
    ) -> int:
        """Consume one of the two candidate-generation attempts for a claim."""
        owner = _bounded_text(lease_owner, field="lease_owner", limit=128)
        if estimated_tokens < 1:
            raise SkillImprovementError("scheduled candidate token estimate is invalid")
        with self._lock, self._process_lock():
            batch = self.get_scheduled_batch(batch_id)
            if batch is None or batch.status != "running":
                raise SkillImprovementError("scheduled batch is not running")
            if (
                batch.lease_owner != owner
                or not batch.lease_expires_at
                or batch.lease_expires_at <= time.time()
            ):
                raise SkillImprovementError("scheduled batch lease is invalid")
            if batch.candidate_id is not None:
                raise SkillImprovementError("scheduled batch already saved a candidate")
            if batch.candidate_attempts >= batch.max_model_calls:
                raise SkillImprovementError("scheduled batch model-call budget exhausted")
            if batch.token_count + estimated_tokens > batch.max_tokens:
                raise SkillImprovementError("scheduled batch token budget exhausted")
            attempt = batch.candidate_attempts + 1
            updated = replace(
                batch,
                candidate_attempts=attempt,
                token_count=batch.token_count + estimated_tokens,
                updated_at=_now(),
            )
            self._atomic_write(self.batches_dir / f"{batch.batch_id}.json", updated.to_dict())
            return attempt

    def register_scheduled_candidate(
        self,
        batch_id: str,
        *,
        lease_owner: str,
        candidate_id: str,
    ) -> ScheduledSkillBatch:
        owner = _bounded_text(lease_owner, field="lease_owner", limit=128)
        candidate_value = _bounded_text(candidate_id, field="candidate_id", limit=128)
        with self._lock, self._process_lock():
            batch = self.get_scheduled_batch(batch_id)
            if batch is None or batch.status != "running" or batch.lease_owner != owner:
                raise SkillImprovementError("scheduled batch lease is invalid")
            if batch.candidate_id is not None:
                raise SkillImprovementError("scheduled batch already saved a candidate")
            updated = replace(batch, candidate_id=candidate_value, updated_at=_now())
            self._atomic_write(self.batches_dir / f"{batch.batch_id}.json", updated.to_dict())
            return updated

    def finish_scheduled_batch(
        self,
        batch_id: str,
        *,
        lease_owner: str,
        error: str | None = None,
        cancelled: bool = False,
    ) -> ScheduledSkillBatch:
        """Commit the evidence cursor only after a candidate was durably saved."""
        owner = _bounded_text(lease_owner, field="lease_owner", limit=128)
        with self._lock, self._process_lock():
            batch = self.get_scheduled_batch(batch_id)
            if batch is None or batch.status != "running" or batch.lease_owner != owner:
                raise SkillImprovementError("scheduled batch lease is invalid")
            if batch.candidate_id is None and not error and not cancelled:
                error = "scheduled batch produced no candidate"
            evidence = [self.get_feedback(item) for item in batch.claimed_evidence_ids]
            valid_evidence = [item for item in evidence if item is not None]
            processed = list(batch.processed_evidence_ids)
            cursor_at = batch.cursor_at
            if batch.candidate_id is not None and not error and not cancelled:
                processed.extend(item.evidence_id for item in valid_evidence)
                processed = list(dict.fromkeys(processed))[-_MAX_PROCESSED_EVIDENCE_IDS:]
                cursor_at = max(
                    (item.occurred_at for item in valid_evidence),
                    default=cursor_at,
                )
                status = "candidate_created"
                last_error = None
                last_candidates = (batch.candidate_id,)
            else:
                status = "failed" if not cancelled else "idle"
                last_error = _bounded_text(
                    error or "scheduled batch cancelled",
                    field="last_error",
                    limit=1_000,
                )
                last_candidates = ()
            updated = replace(
                batch,
                cursor_at=cursor_at,
                processed_evidence_ids=tuple(processed),
                claimed_evidence_ids=(),
                status=status,
                lease_owner=None,
                lease_expires_at=None,
                last_error=last_error,
                last_candidate_ids=last_candidates,
                consecutive_failures=(
                    0
                    if status == "candidate_created"
                    else batch.consecutive_failures + 1
                ),
                updated_at=_now(),
            )
            self._atomic_write(self.batches_dir / f"{batch.batch_id}.json", updated.to_dict())
            return updated

    def create_candidate(
        self,
        *,
        user_id: str,
        project_id: str,
        skill_path: str,
        baseline_content: str,
        candidate_content: str,
        evidence_ids: object,
        case_ids: object = (),
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
        case_values = _refs(case_ids)
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
        if any(
            item is not None and item.workspace != workspace_value
            for item in evidence
        ):
            raise SkillImprovementError("candidate evidence workspace mismatch")
        users = {item.user_id for item in evidence if item is not None}
        if len(users) != 1:
            raise SkillImprovementError("candidate evidence must belong to one user")
        if next(iter(users)) != _bounded_text(user_id, field="user_id", limit=128):
            raise SkillImprovementError("candidate evidence does not belong to the requester")
        projects = {item.project_id for item in evidence if item is not None}
        if projects != {project_id}:
            raise SkillImprovementError("candidate evidence must belong to one project")
        cases = [self.get_improvement_case(item) for item in case_values]
        if any(item is None for item in cases):
            raise SkillImprovementError("candidate case was not found")
        if any(item is not None and not self._case_is_replayable(item) for item in cases):
            raise SkillImprovementError("candidate case is not replayable")
        requester = _bounded_text(user_id, field="user_id", limit=128)
        if any(
            item is not None
            and (
                item.skill_id != TRIAL_SKILL_ID
                or item.workspace != workspace_value
                or item.project_id != project_id
                or item.user_id != requester
                or item.base_skill_hash != effective.content_hash
            )
            for item in cases
        ):
            raise SkillImprovementError("candidate case binding does not match the candidate")
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
        with self._lock, self._process_lock():
            locked_cases = [self.get_improvement_case(item) for item in case_values]
            if any(item is None or not self._case_is_replayable(item) for item in locked_cases):
                raise SkillImprovementError("candidate case changed during creation")
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
                user_id=next(iter(users)),
                session_ids=tuple(
                    sorted({item.session_id for item in evidence if item is not None})
                ),
                task_ids=tuple(
                    sorted({item.task_id for item in evidence if item is not None})
                ),
                case_ids=case_values,
            )
            self._atomic_write(
                self.candidates_dir / f"{candidate.candidate_id}.json", candidate.to_dict()
            )
            return candidate

    def get_candidate(
        self,
        candidate_id: str,
        *,
        workspace: str | Path | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> SkillCandidate | None:
        if not _record_id_matches(candidate_id, "candidate"):
            return None
        path = self.candidates_dir / f"{candidate_id}.json"
        if not self._record_exists("candidate", candidate_id):
            return None
        raw = self._read_object(path, "candidate record")
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
            user_id=str(raw.get("user_id") or ""),
            session_ids=_refs(raw.get("session_ids")),
            task_ids=_refs(raw.get("task_ids")),
            case_ids=_refs(raw.get("case_ids")),
        )
        if content_sha256(candidate.candidate_content) != candidate.candidate_hash:
            raise SkillImprovementError("candidate content hash mismatch")
        if workspace is not None:
            workspace_value = str(Path(workspace).expanduser().resolve())
            if candidate.workspace != workspace_value:
                return None
        if user_id is not None and candidate.user_id != _bounded_text(
            user_id, field="user_id", limit=128
        ):
            return None
        if project_id is not None and candidate.project_id != _bounded_text(
            project_id, field="project_id", limit=128
        ):
            return None
        return candidate

    def list_candidates(
        self,
        *,
        batch_id: str | None = None,
        workspace: str | Path | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> list[SkillCandidate]:
        workspace_value = (
            str(Path(workspace).expanduser().resolve()) if workspace is not None else None
        )
        candidates: list[SkillCandidate] = []
        candidate_ids = {
            value["candidate_id"]
            for value in self._record_rows("candidate")
            if isinstance(value.get("candidate_id"), str)
        }
        for candidate_id in sorted(candidate_ids):
            candidate = self.get_candidate(
                candidate_id, user_id=user_id, project_id=project_id
            )
            if (
                candidate is not None
                and (batch_id is None or candidate.batch_id == batch_id)
                and (workspace_value is None or candidate.workspace == workspace_value)
            ):
                candidates.append(candidate)
        return candidates

    def read_effective_snapshot(
        self,
        workspace: str | Path,
        *,
        skill_id: str = TRIAL_SKILL_ID,
    ) -> SkillSnapshot:
        """Read the only trusted on-disk version of one registered Skill."""
        target = skill_improvement_target(skill_id)
        root = Path(workspace).expanduser().resolve()
        lexical_path = root / target.skill_path
        if lexical_path.parent.is_symlink() or lexical_path.is_symlink():
            raise SkillImprovementError(f"effective {skill_id} Skill cannot be a symlink")
        path = lexical_path.resolve()
        expected = root / target.skill_path
        if path != expected or not path.is_file():
            raise SkillImprovementError(f"effective {skill_id} Skill is unavailable")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillImprovementError(f"effective {skill_id} Skill is unreadable") from exc
        return SkillSnapshot(
            skill_id=skill_id,
            skill_path=target.skill_path,
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
        skill_id: str = TRIAL_SKILL_ID,
        allowed_file_scope: tuple[str, ...] | list[str] = (),
        verification_profile: str | None = None,
        source_evidence_refs: tuple[str, ...] | list[str] = (),
    ) -> SkillSnapshot:
        """Pin the first effective version for a task; never silently refresh it."""
        if verification_profile is not None or allowed_file_scope:
            if verification_profile is None:
                raise SkillImprovementError(
                    "verification_profile is required with an allowed file scope"
                )
            try:
                replay_metadata = capture_git_snapshot(
                    workspace,
                    allowed_file_scope=allowed_file_scope,
                    verification_profile=verification_profile,
                )
            except ReplayabilityError as exc:
                raise SkillImprovementError("replay snapshot metadata is invalid") from exc
            if not self._project_is_trusted(project_id, workspace):
                replay_metadata = replace(
                    replay_metadata,
                    replayable=False,
                    reason="project is not registered and trusted",
                )
        else:
            replay_metadata = None
        task_value = _bounded_text(task_id, field="task_id", limit=128)
        task_key = task_snapshot_ref(task_value)
        legacy_task_key = content_sha256(task_value)
        path = self.snapshots_dir / f"{task_key}.json"
        with self._lock:
            session_value = _bounded_text(session_id, field="session_id", limit=128)
            project_value = _bounded_text(project_id, field="project_id", limit=128)
            workspace_value = str(Path(workspace).expanduser().resolve())
            existing_path = path
            existing_id = task_key
            if not self._record_exists("snapshot", existing_id) and self._record_exists(
                "snapshot", legacy_task_key
            ):
                existing_path = self.snapshots_dir / f"{legacy_task_key}.json"
                existing_id = legacy_task_key
            if self._record_exists("snapshot", existing_id):
                raw = self._read_object(existing_path, "task snapshot")
                if (
                    raw.get("task_id") != task_value
                    or raw.get("skill_id") != skill_id
                    or raw.get("session_id") != session_value
                    or raw.get("project_id") != project_value
                    or raw.get("workspace") != workspace_value
                ):
                    raise SkillImprovementError("task snapshot binding mismatch")
                existing = self._snapshot_from_dict(raw)
                if (
                    replay_metadata is not None
                    and existing.replay_metadata_pending
                    and (verification_profile != "pending" or allowed_file_scope)
                ):
                    if (
                        existing.repository_identity
                        and existing.repository_identity != replay_metadata.repository_identity
                    ):
                        raise SkillImprovementError(
                            "task snapshot repository identity changed"
                        )
                    merged = replace(
                        existing,
                        repository_root=existing.repository_root,
                        repository_identity=existing.repository_identity,
                        start_commit_sha=existing.start_commit_sha,
                        working_tree_clean=existing.working_tree_clean,
                        allowed_file_scope=replay_metadata.allowed_file_scope,
                        verification_profile=replay_metadata.verification_profile,
                        replayable=existing.start_replayable is True,
                        replayability_reason=(
                            existing.start_replayability_reason
                            if existing.start_replayable is not True
                            else None
                        ),
                        replay_metadata_pending=False,
                        source_evidence_refs=_refs(
                            (*existing.source_evidence_refs, *source_evidence_refs)
                        ),
                    )
                    if merged != existing:
                        self._atomic_write(
                            existing_path,
                            {
                                "task_id": task_value,
                                "session_id": session_value,
                                "project_id": project_value,
                                "workspace": workspace_value,
                                **self._snapshot_to_dict(merged),
                            },
                        )
                    return merged
                if replay_metadata is not None and (
                    existing.allowed_file_scope
                    and existing.allowed_file_scope != replay_metadata.allowed_file_scope
                    or existing.verification_profile
                    and existing.verification_profile != replay_metadata.verification_profile
                ):
                    raise SkillImprovementError(
                        "task snapshot replay metadata is immutable"
                    )
                return existing
            effective = self.read_effective_snapshot(workspace, skill_id=skill_id)
            snapshot = SkillSnapshot(
                **self._snapshot_to_dict(effective),
                task_id=task_value,
                session_id=session_value,
                project_id=project_value,
                workspace=workspace_value,
            )
            if replay_metadata is not None:
                metadata_pending = verification_profile == "pending" and not allowed_file_scope
                snapshot = replace(
                    snapshot,
                    repository_root=replay_metadata.repository_root,
                    repository_identity=replay_metadata.repository_identity,
                    start_commit_sha=replay_metadata.start_commit_sha,
                    working_tree_clean=replay_metadata.working_tree_clean,
                    allowed_file_scope=replay_metadata.allowed_file_scope,
                    verification_profile=replay_metadata.verification_profile,
                    replayable=False if metadata_pending else replay_metadata.replayable,
                    replayability_reason=(
                        "verification scope is pending"
                        if metadata_pending and replay_metadata.replayable
                        else replay_metadata.reason
                    ),
                    replay_metadata_pending=metadata_pending,
                    source_evidence_refs=_refs(source_evidence_refs),
                    start_replayable=replay_metadata.replayable,
                    start_replayability_reason=replay_metadata.reason,
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

    def get_task_snapshot(
        self,
        snapshot_ref: str,
        *,
        workspace: str | Path | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        skill_id: str | None = None,
    ) -> SkillSnapshot | None:
        """Read a captured snapshot only when its private binding still matches."""
        with self._lock, self._process_lock():
            return self._get_task_snapshot_unlocked(
                snapshot_ref,
                workspace=workspace,
                project_id=project_id,
                session_id=session_id,
                skill_id=skill_id,
            )

    def _get_task_snapshot_unlocked(
        self,
        snapshot_ref: str,
        *,
        workspace: str | Path | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        skill_id: str | None = None,
    ) -> SkillSnapshot | None:
        """Read a snapshot while the caller owns the process lock."""
        if not (_record_id_matches(snapshot_ref, "snapshot") or _is_sha256(snapshot_ref)):
            return None
        path = self.snapshots_dir / f"{snapshot_ref}.json"
        if not self._record_exists("snapshot", snapshot_ref):
            self._invalidate_snapshot_dependents(snapshot_ref)
            return None
        try:
            snapshot = self._snapshot_from_dict(self._read_object(path, "task snapshot"))
        except SkillImprovementError:
            self._invalidate_snapshot_dependents(snapshot_ref)
            return None
        if workspace is not None and snapshot.workspace != str(
            Path(workspace).expanduser().resolve()
        ):
            return None
        if project_id is not None and snapshot.project_id != project_id:
            return None
        if session_id is not None and snapshot.session_id != session_id:
            return None
        if skill_id is not None and snapshot.skill_id != skill_id:
            return None
        return snapshot

    def invalidate_task_snapshot(self, snapshot_ref: str) -> None:
        """Invalidate one private snapshot and all dependent unactivated records."""
        if not (_record_id_matches(snapshot_ref, "snapshot") or _is_sha256(snapshot_ref)):
            raise SkillImprovementError("invalid task snapshot reference")
        with self._lock, self._process_lock():
            self._delete_record_history("snapshot", snapshot_ref)
            self._invalidate_snapshot_dependents(snapshot_ref)

    def _invalidate_snapshot_dependents(self, snapshot_ref: str) -> None:
        for case in self.list_improvement_cases():
            if case.replay_snapshot_ref != snapshot_ref or case.status == "not_replayable":
                continue
            invalidated = replace(case, status="not_replayable")
            self._atomic_write(
                self.cases_dir / f"{case.case_id}.json", invalidated.to_dict()
            )
            for candidate in self.list_candidates(workspace=case.workspace):
                if case.case_id not in candidate.case_ids or candidate.status == "active":
                    continue
                self._atomic_write(
                    self.candidates_dir / f"{candidate.candidate_id}.json",
                    {
                        **candidate.to_dict(),
                        "status": "stale",
                        "result_refs": (),
                    },
                )

    def evaluate_candidate(
        self,
        candidate_id: str,
        *,
        workspace: str | Path,
        user_id: str | None = None,
        project_id: str | None = None,
        model_id: str = "deterministic-synthetic",
        parameters: Mapping[str, Any] | None = None,
        budget: Mapping[str, Any] | None = None,
    ) -> EvaluationReport:
        """Run the immutable 18-case synthetic comparison.

        The case definitions and assertions live in this trusted module.  Skill
        text can affect only the candidate's behavior, never the test set or
        its pass criteria.  No model call is made by this deterministic runner.
        """
        candidate = self.get_candidate(
            candidate_id,
            user_id=user_id,
            project_id=project_id,
        )
        if candidate is None:
            raise SkillImprovementError("candidate not found")
        if candidate.test_set_version != EVALUATION_TEST_SET_VERSION:
            raise SkillImprovementError("candidate uses an unsupported test set")
        workspace_value = str(Path(workspace).expanduser().resolve())
        if candidate.workspace != workspace_value:
            raise SkillImprovementError("candidate workspace mismatch")
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
                for repeat in range(1, EVALUATION_REPEATS + 1):
                    (
                        success,
                        agent_trace,
                        fixture,
                        assertion,
                        environment_id,
                    ) = _run_fixed_skill_trial(
                        category,
                        content,
                        trial_id=f"{category}:{version}:{repeat}",
                    )
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
                            "fixture": fixture,
                            "assertion": assertion,
                            "environment_id": environment_id,
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
        if not self._record_exists("evaluation", evaluation_id):
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
            case_id=raw.get("case_id") if isinstance(raw.get("case_id"), str) else None,
            baseline_trial_id=(
                raw.get("baseline_trial_id")
                if isinstance(raw.get("baseline_trial_id"), str)
                else None
            ),
            candidate_trial_id=(
                raw.get("candidate_trial_id")
                if isinstance(raw.get("candidate_trial_id"), str)
                else None
            ),
        )
        expected_ids = {
            f"{category}:{version}:{repeat}"
            for category in EVALUATION_CATEGORIES
            for version in EVALUATION_VERSIONS
            for repeat in range(1, EVALUATION_REPEATS + 1)
        }
        if len(report.cases) != len(expected_ids) or {
            str(item.get("case_id")) for item in report.cases
        } != expected_ids:
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
        candidate = self.get_candidate(
            candidate_id,
            workspace=workspace,
            user_id=user_id,
        )
        if candidate is None:
            raise SkillImprovementError("candidate is not eligible for confirmation")
        if candidate.workspace != str(Path(workspace).expanduser().resolve()):
            raise SkillImprovementError("candidate workspace mismatch")
        if candidate.project_id != _bounded_text(project_id, field="project_id", limit=128):
            raise SkillImprovementError("candidate project mismatch")
        if candidate.status != "pending_confirmation":
            raise SkillImprovementError("candidate is not eligible for confirmation")
        evaluation = (
            self.get_evaluation(candidate.result_refs[-1])
            if candidate.result_refs
            else None
        )
        if (
            evaluation is None
            or evaluation.candidate_id != candidate.candidate_id
            or evaluation.test_set_version != candidate.test_set_version
            or not evaluation.eligible
        ):
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
        if self._recovery_blocked:
            raise SkillImprovementError("activation recovery is unavailable")
        if not _record_id_matches(confirmation_id, "confirmation"):
            raise SkillImprovementError("confirmation not found")
        path = self.confirmations_dir / f"{confirmation_id}.json"
        with self._lock, self._process_lock():
            confirmation = self._confirmation_from_dict(self._read_object(path, "confirmation"))
            self._check_confirmation(confirmation, user_id, session_id, workspace, project_id)
            candidate = self.get_candidate(
                confirmation.candidate_id,
                workspace=confirmation.workspace,
                user_id=confirmation.user_id,
                project_id=confirmation.project_id,
            )
            if (
                candidate is None
                or candidate.status != "pending_confirmation"
                or candidate.skill_id != confirmation.skill_id
                or candidate.test_set_version != confirmation.test_set_version
                or candidate.candidate_hash != confirmation.candidate_hash
            ):
                raise SkillImprovementError("candidate changed after confirmation")
            evaluation = self.get_evaluation(confirmation.evaluation_id)
            if (
                evaluation is None
                or evaluation.candidate_id != candidate.candidate_id
                or evaluation.test_set_version != confirmation.test_set_version
                or not evaluation.eligible
            ):
                raise SkillImprovementError("evaluation changed after confirmation")
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

    def ignore_candidate(
        self,
        candidate_id: str,
        *,
        workspace: str | Path,
        user_id: str,
        project_id: str,
    ) -> SkillCandidate:
        candidate = self.get_candidate(
            candidate_id,
            workspace=workspace,
            user_id=user_id,
            project_id=project_id,
        )
        if candidate is None:
            raise SkillImprovementError("candidate not found")
        return self._set_candidate_status(candidate, "ignored")

    def restore_candidate(
        self,
        candidate_id: str,
        *,
        workspace: str | Path,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> SkillSnapshot:
        """Restore the saved stable content only if the active file is untouched."""
        with self._lock, self._process_lock():
            candidate = self.get_candidate(
                candidate_id,
                workspace=workspace,
                user_id=user_id,
                project_id=project_id,
            )
            if candidate is None or candidate.status != "active":
                raise SkillImprovementError("candidate is not active")
            workspace_value = str(Path(workspace).expanduser().resolve())
            if candidate.workspace != workspace_value:
                raise SkillImprovementError("candidate workspace mismatch")
            current = self.read_effective_snapshot(workspace)
            if current.content_hash != candidate.candidate_hash:
                raise SkillImprovementError(
                    "restore stopped: effective Skill was externally modified"
                )
            records = [
                item for item in self._read_jsonl(self.activations_file)
                if item.get("candidate_id") == candidate_id
                and item.get("workspace") == workspace_value
                and item.get("phase") == "committed"
            ]
            if not records or not isinstance(records[-1].get("previous_content"), str):
                raise SkillImprovementError("stable version for restore is unavailable")
            skill_path = Path(workspace).expanduser().resolve() / TRIAL_SKILL_RELATIVE_PATH
            restore_journal = {
                "phase": "restore_prepared",
                "candidate_id": candidate_id,
                "workspace": workspace_value,
                "active_hash": candidate.candidate_hash,
                "restore_hash": content_sha256(records[-1]["previous_content"]),
                "restore_content": records[-1]["previous_content"],
                "restored_at": _now(),
            }
            self._append_jsonl(self.activations_file, restore_journal)
            self._atomic_replace_text(skill_path, records[-1]["previous_content"])
            self._append_jsonl(
                self.activations_file,
                {**restore_journal, "phase": "restore_committed"},
            )
            restored = self.read_effective_snapshot(workspace)
            self._set_candidate_status(candidate, "stale")
            return restored

    def _recover_activation_journal(self) -> None:
        """Reconcile a crash between the file swap and metadata persistence."""
        if not self._record_rows("activation"):
            return
        try:
            records = self._read_jsonl(self.activations_file)
        except SkillImprovementError:
            self._recovery_blocked = True
            return
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for record in records:
            candidate_id = record.get("candidate_id")
            workspace = record.get("workspace")
            if isinstance(candidate_id, str) and isinstance(workspace, str):
                latest[(candidate_id, workspace)] = record
        for (candidate_id, workspace), record in latest.items():
            if record.get("phase") in {"restore_prepared", "restore_committed"}:
                candidate = self.get_candidate(candidate_id)
                if candidate is None:
                    continue
                try:
                    current = self.read_effective_snapshot(workspace)
                except SkillImprovementError:
                    continue
                restore_hash = record.get("restore_hash")
                active_hash = record.get("active_hash")
                if current.content_hash == restore_hash:
                    if record.get("phase") == "restore_prepared":
                        self._append_jsonl(
                            self.activations_file,
                            {**record, "phase": "restore_committed"},
                        )
                    self._set_candidate_status(candidate, "stale")
                elif current.content_hash == active_hash:
                    self._append_jsonl(
                        self.activations_file,
                        {**record, "phase": "restore_aborted"},
                    )
                else:
                    self._set_candidate_status(candidate, "stale")
                continue
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
                    if self._record_exists("confirmation", confirmation_id):
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
    def _batch_from_dict(raw: Mapping[str, Any]) -> ScheduledSkillBatch:
        status = str(raw.get("status", "idle"))
        if status not in BATCH_STATES:
            raise SkillImprovementError("scheduled batch has an invalid status")
        return ScheduledSkillBatch(
            batch_id=str(raw["batch_id"]),
            job_id=str(raw["job_id"]),
            skill_id=str(raw["skill_id"]),
            workspace=str(raw["workspace"]),
            project_id=str(raw["project_id"]),
            user_id=str(raw["user_id"]),
            session_key=str(raw["session_key"]),
            enabled=bool(raw["enabled"]),
            read_scope=str(raw["read_scope"]),
            max_model_calls=int(raw["max_model_calls"]),
            max_tokens=int(raw["max_tokens"]),
            max_duration_seconds=int(raw["max_duration_seconds"]),
            notification_mode=str(raw["notification_mode"]),
            cursor_at=raw.get("cursor_at") if isinstance(raw.get("cursor_at"), str) else None,
            processed_evidence_ids=_refs(raw.get("processed_evidence_ids")),
            claimed_evidence_ids=_refs(raw.get("claimed_evidence_ids")),
            status=status,
            lease_owner=raw.get("lease_owner") if isinstance(raw.get("lease_owner"), str) else None,
            lease_expires_at=(
                float(raw["lease_expires_at"])
                if raw.get("lease_expires_at") is not None
                else None
            ),
            run_count=int(raw.get("run_count", 0)),
            candidate_attempts=int(raw.get("candidate_attempts", 0)),
            token_count=int(raw.get("token_count", 0)),
            consecutive_failures=int(raw.get("consecutive_failures", 0)),
            candidate_id=(
                raw.get("candidate_id")
                if isinstance(raw.get("candidate_id"), str)
                else None
            ),
            last_run_at=raw.get("last_run_at") if isinstance(raw.get("last_run_at"), str) else None,
            last_error=raw.get("last_error") if isinstance(raw.get("last_error"), str) else None,
            last_notified_error=(
                raw.get("last_notified_error")
                if isinstance(raw.get("last_notified_error"), str)
                else None
            ),
            last_candidate_ids=_refs(raw.get("last_candidate_ids")),
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
        )

    @staticmethod
    def _snapshot_to_dict(snapshot: SkillSnapshot) -> dict[str, Any]:
        return {
            "skill_id": snapshot.skill_id,
            "skill_path": snapshot.skill_path,
            "content": snapshot.content,
            "content_hash": snapshot.content_hash,
            "captured_at": snapshot.captured_at,
            "repository_root": snapshot.repository_root,
            "repository_identity": snapshot.repository_identity,
            "start_commit_sha": snapshot.start_commit_sha,
            "working_tree_clean": snapshot.working_tree_clean,
            "allowed_file_scope": list(snapshot.allowed_file_scope),
            "verification_profile": snapshot.verification_profile,
            "replayable": snapshot.replayable,
            "replayability_reason": snapshot.replayability_reason,
            "replay_metadata_pending": snapshot.replay_metadata_pending,
            "source_evidence_refs": list(snapshot.source_evidence_refs),
            "start_replayable": snapshot.start_replayable,
            "start_replayability_reason": snapshot.start_replayability_reason,
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
            repository_root=(
                raw.get("repository_root")
                if isinstance(raw.get("repository_root"), str)
                else None
            ),
            repository_identity=(
                raw.get("repository_identity")
                if isinstance(raw.get("repository_identity"), str)
                else None
            ),
            start_commit_sha=(
                raw.get("start_commit_sha")
                if isinstance(raw.get("start_commit_sha"), str)
                else None
            ),
            working_tree_clean=(
                raw.get("working_tree_clean")
                if isinstance(raw.get("working_tree_clean"), bool)
                else None
            ),
            allowed_file_scope=tuple(
                item
                for item in raw.get("allowed_file_scope", [])
                if isinstance(item, str)
            )
            if isinstance(raw.get("allowed_file_scope", []), list)
            else (),
            verification_profile=(
                raw.get("verification_profile")
                if isinstance(raw.get("verification_profile"), str)
                else None
            ),
            replayable=(
                raw.get("replayable")
                if isinstance(raw.get("replayable"), bool)
                else None
            ),
            replayability_reason=(
                raw.get("replayability_reason")
                if isinstance(raw.get("replayability_reason"), str)
                else None
            ),
            replay_metadata_pending=(
                raw.get("replay_metadata_pending") is True
            ),
            source_evidence_refs=(
                _refs(raw.get("source_evidence_refs"))
            ),
            start_replayable=(
                raw.get("start_replayable")
                if isinstance(raw.get("start_replayable"), bool)
                else None
            ),
            start_replayability_reason=(
                raw.get("start_replayability_reason")
                if isinstance(raw.get("start_replayability_reason"), str)
                else None
            ),
        )

    def _read_object(self, path: Path, label: str) -> dict[str, Any]:
        record_type, record_id = self._namespace(path)
        if record_id is None:
            raise SkillImprovementError(f"{label} is not a single record")
        try:
            rows = self._record_rows(record_type, record_id)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise SkillImprovementError(f"{label} is unreadable") from exc
        if not rows:
            raise SkillImprovementError(f"{label} is unavailable")
        return rows[-1]

    def _load_integrity_key(self) -> bytes:
        legacy_key: bytes | None = None
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            row = connection.execute(
                "select payload_json from skill_improvement_records "
                "where record_key = 'integrity:key'"
            ).fetchone()
            if row is not None:
                try:
                    legacy_key = bytes.fromhex(str(json.loads(row[0])["key"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise SkillImprovementError("integrity key is invalid") from exc
                if len(legacy_key) != 32:
                    raise SkillImprovementError("integrity key is invalid")

        self.integrity_key_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = self.integrity_key_file.read_bytes()
        except FileNotFoundError:
            key = legacy_key or secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    self.integrity_key_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                key = self.integrity_key_file.read_bytes()
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise SkillImprovementError("integrity key is unreadable") from exc
        if len(key) != 32:
            raise SkillImprovementError("integrity key is invalid")
        # Migrate the pre-P8-02 key out of the business table.  New records
        # never store the integrity root alongside data it authenticates.
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            connection.execute(
                "delete from skill_improvement_records where record_key = 'integrity:key'"
            )
        return key

    def _delete_record_history(self, record_type: str, record_id: str) -> None:
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            connection.execute(
                "delete from skill_improvement_records "
                "where record_type = ? and (record_id = ? or record_id like ?)" ,
                (record_type, record_id, f"{record_id}:%"),
            )

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

    @contextmanager
    def _process_lock(self):
        """Serialize confirmation consumption across nanobot processes."""
        self.process_lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self.process_lock_file.open("a+b") as handle:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover - exercised on POSIX CI
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - exercised on POSIX CI
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
        record_type, record_id = self._namespace(path)
        if record_id is not None:
            raise SkillImprovementError("append-only storage namespace is invalid")
        return self._record_rows(record_type)

    def _append_jsonl(self, path: Path, value: Mapping[str, Any]) -> None:
        safe = _json_safe(value)
        record_type, _record_id = self._namespace(path)
        record_id = str(safe.get("evidence_id") or safe.get("candidate_id") or uuid.uuid4())
        self._write_record(record_type, f"{record_id}:{uuid.uuid4()}", safe, replace=False)

    def _atomic_write(self, path: Path, value: Mapping[str, Any]) -> None:
        safe = _json_safe(value)
        record_type, record_id = self._namespace(path)
        if record_id is None:
            raise SkillImprovementError("single-record storage namespace is invalid")
        self._write_record(record_type, record_id, safe, replace=True)

    def _write_record(
        self,
        record_type: str,
        record_id: str,
        value: Mapping[str, Any],
        *,
        replace: bool,
    ) -> None:
        safe = _json_safe(value)
        payload = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record_key = f"{record_type}:{record_id}"
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            if replace:
                connection.execute(
                    "delete from skill_improvement_records where record_key = ?",
                    (record_key,),
                )
            connection.execute(
                "insert into skill_improvement_records "
                "(record_key, record_type, record_id, payload_json, record_mac, "
                "workspace, project_id, created_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_key,
                    record_type,
                    record_id,
                    payload,
                    self._record_mac(safe),
                    safe.get("workspace"),
                    safe.get("project_id"),
                    _now(),
                ),
            )


def _run_fixed_skill_trial(
    category: str, content: str, *, trial_id: str
) -> tuple[bool, tuple[str, ...], str, str, str]:
    """Run a trusted Skill protocol against an immutable fixture and assertion.

    This is intentionally offline and protocol-level: it executes fixed fixture
    checks through named Agent/Tool steps, rather than claiming live-model quality.
    The candidate can influence the simulated guidance, but cannot change the
    fixture bytes, expected action, or assertion.
    """
    scenario = next(
        (item for item in FIXED_SKILL_SCENARIOS if item.category == category),
        None,
    )
    if scenario is None:
        raise SkillImprovementError("unknown fixed evaluation category")
    fixture = f"p8-fixed/{scenario.fixture_id}"
    environment_id = f"p8-isolated:{trial_id}"
    with tempfile.TemporaryDirectory(prefix="devpilot-p8-trial-") as isolated_root:
        fixture_path = Path(isolated_root) / scenario.fixture_id
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(scenario.source_text, encoding="utf-8")
        fixture_content = fixture_path.read_text(encoding="utf-8")
        source_hash = content_sha256(fixture_content)
        if source_hash != content_sha256(scenario.source_text):
            raise SkillImprovementError("fixed fixture changed during trial setup")
        if category == "original_failure":
            expected = "read_current_file_before_proposing_patch"
            has_guidance = "Re-read the current file before proposing a patch." in content
            trace = (
                f"fixture:{fixture_path.name}",
                "tool:read_file",
                f"tool:file_hash:{source_hash}",
                "agent:propose_patch",
            ) if has_guidance else ("agent:propose_patch",)
            success = (
                has_guidance
                and fixture_content.startswith("def discount")
                and scenario.failure_evidence.startswith("10 percent")
                and source_hash == content_sha256(scenario.source_text)
                and expected == scenario.expected_action
            )
            return (
                success,
                trace,
                fixture,
                "fixed assertion: read current fixture before proposal",
                environment_id,
            )
        if category == "nearby_holdout":
            expected = "read_failure_evidence"
            has_guidance = "Read the failure evidence." in content
            trace = (
                f"fixture:{fixture_path.name}",
                "tool:read_failure_evidence",
                "agent:propose_patch",
            ) if has_guidance else ("agent:propose_patch",)
            success = (
                has_guidance
                and fixture_content.startswith("def parse")
                and scenario.failure_evidence.startswith("The parser")
            )
            return (
                success,
                trace,
                fixture,
                "fixed assertion: failure evidence is read before proposal",
                environment_id,
            )
        if category == "regression":
            expected = "preserve_regression_contract"
            has_guidance = "# Bug fix" in content and "Read the failure evidence." in content
            trace = (
                f"fixture:{fixture_path.name}",
                "tool:read_failure_evidence",
                "agent:preserve_regression_contract",
                "agent:propose_patch",
            ) if has_guidance else ("agent:propose_patch",)
            success = (
                has_guidance
                and fixture_content.startswith("# Bug fix")
                and expected == scenario.expected_action
            )
            return (
                success,
                trace,
                fixture,
                "fixed assertion: protected bug-fix contract is preserved",
                environment_id,
            )
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


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def _improvement_case_from_dict(raw: Mapping[str, Any]) -> ImprovementCase:
    status = str(raw.get("status", "eligible"))
    if status not in IMPROVEMENT_CASE_STATES:
        raise SkillImprovementError("invalid improvement case status")
    skill_id = str(raw["skill_id"])
    target = skill_improvement_target(skill_id)
    if str(raw["profile_binding"]) != target.profile_binding:
        raise SkillImprovementError("improvement case profile binding does not match target")
    base_hash = str(raw["base_skill_hash"])
    if not _is_sha256(base_hash):
        raise SkillImprovementError("improvement case baseline hash is invalid")
    return ImprovementCase(
        case_id=str(raw["case_id"]),
        skill_id=skill_id,
        profile_binding=str(raw["profile_binding"]),
        origin_run_id=str(raw["origin_run_id"]),
        task_id=str(raw["task_id"]),
        project_id=str(raw["project_id"]),
        workspace=str(raw["workspace"]),
        user_id=str(raw["user_id"]),
        session_id=str(raw["session_id"]),
        base_skill_hash=base_hash.lower(),
        trigger_type=str(raw["trigger_type"]),
        trigger_summary=str(raw["trigger_summary"]),
        expected_behavior=str(raw["expected_behavior"]),
        evidence_refs=_refs(raw.get("evidence_refs")),
        replay_snapshot_ref=(
            str(raw["replay_snapshot_ref"])
            if isinstance(raw.get("replay_snapshot_ref"), str)
            else None
        ),
        assertion_spec=dict(raw.get("assertion_spec") or {}),
        status=status,
        selected_batch_id=(
            str(raw["selected_batch_id"])
            if isinstance(raw.get("selected_batch_id"), str)
            else None
        ),
        created_at=str(raw["created_at"]),
    )


def _replay_trial_from_dict(raw: Mapping[str, Any]) -> ReplayTrial:
    status = str(raw["status"])
    if status not in REPLAY_TRIAL_STATUSES:
        raise SkillImprovementError("invalid replay trial status")
    skill_hash = str(raw["skill_hash"])
    if not _is_sha256(skill_hash):
        raise SkillImprovementError("replay trial Skill hash is invalid")
    version = str(raw["version"])
    if version not in {"baseline", "candidate"}:
        raise SkillImprovementError("replay trial version must be baseline or candidate")
    return ReplayTrial(
        trial_id=str(raw["trial_id"]),
        case_id=str(raw["case_id"]),
        skill_id=str(raw["skill_id"]),
        skill_hash=skill_hash.lower(),
        version=version,
        model_id=str(raw["model_id"]),
        parameters=dict(raw.get("parameters") or {}),
        snapshot_ref=str(raw["snapshot_ref"]),
        started_at=str(raw["started_at"]),
        finished_at=str(raw["finished_at"]),
        token_count=int(raw["token_count"]),
        duration_seconds=float(raw["duration_seconds"]),
        tool_trace_ref=str(raw["tool_trace_ref"]),
        final_output_ref=str(raw["final_output_ref"]),
        assertion_result=dict(raw.get("assertion_result") or {}),
        status=status,
        candidate_id=(
            str(raw["candidate_id"])
            if isinstance(raw.get("candidate_id"), str)
            else None
        ),
    )


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
