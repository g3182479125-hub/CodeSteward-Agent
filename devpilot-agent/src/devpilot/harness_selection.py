"""H9's durable, bounded candidate-selection gate for plugins and Loop Drivers."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

_KINDS = frozenset({"plugin", "loop"})
_CATEGORIES = ("target", "holdout", "regression")
_VERSIONS = ("baseline", "candidate")
_REPEATS = (1, 2)
_EVIDENCE_SOURCES = frozenset({"task_result", "tool_result", "user_correction"})
_FIXTURE_INPUTS = {
    ("target", 1): {"case": "target-1"},
    ("target", 2): {"case": "target-2"},
    ("holdout", 1): {"case": "holdout-1"},
    ("holdout", 2): {"case": "holdout-2"},
    ("regression", 1): {"case": "regression-1"},
    ("regression", 2): {"case": "regression-2"},
}
_CONFIRMATION_TTL = timedelta(minutes=15)


class HarnessSelectionError(ValueError):
    """Raised when a candidate, evaluation, or promotion binding is invalid."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class HarnessCandidate:
    candidate_id: str
    kind: str
    target_id: str
    baseline_version: str
    candidate_version: str
    baseline_hash: str
    candidate_hash: str
    scope: dict[str, str]
    evidence_refs: tuple[str, ...]
    round_id: str
    budget: dict[str, int]
    status: str
    evaluation_id: str | None
    created_at: str


@dataclass(frozen=True)
class HarnessEvaluation:
    evaluation_id: str
    candidate_id: str
    cases: tuple[dict[str, object], ...]
    baseline_successes: int
    candidate_successes: int
    eligible: bool
    total_runs: int
    result_hash: str
    created_at: str
    status: str = "completed"
    infrastructure_failure: bool = False
    failure_type: str | None = None
    failure_phase: str | None = None


@dataclass(frozen=True)
class HarnessConfirmation:
    confirmation_id: str
    candidate_id: str
    evaluation_id: str
    user_id: str
    session_id: str
    scope: dict[str, str]
    baseline_hash: str
    candidate_hash: str
    evaluation_hash: str
    consumed_at: str | None
    expires_at: str
    invalidated_at: str | None = None


def _stored_text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise HarnessSelectionError(f"selection record field {key} is invalid")
    return value


def _stored_hash(raw: Mapping[str, object], key: str) -> str:
    value = _stored_text(raw, key)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise HarnessSelectionError(f"selection record field {key} is invalid")
    return value


def _stored_optional_text(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise HarnessSelectionError(f"selection record field {key} is invalid")
    return value


def _stored_scope(raw: Mapping[str, object]) -> dict[str, str]:
    value = raw.get("scope")
    if not isinstance(value, Mapping):
        raise HarnessSelectionError("selection record scope is invalid")
    if set(value) != {"project_id", "workspace"} or any(
        not isinstance(key, str) or not isinstance(item, str) or not item
        for key, item in value.items()
    ):
        raise HarnessSelectionError("selection record scope is invalid")
    return {cast(str, key): cast(str, item) for key, item in value.items()}


def _stored_string_tuple(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise HarnessSelectionError(f"selection record field {key} is invalid")
    return tuple(cast(str, item) for item in value)


def _stored_budget(raw: Mapping[str, object]) -> dict[str, int]:
    value = raw.get("budget")
    if not isinstance(value, Mapping) or set(value) != {
        "max_candidates",
        "max_evaluations",
        "max_tokens",
    }:
        raise HarnessSelectionError("selection record budget is invalid")
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item < 1
        for item in value.values()
    ):
        raise HarnessSelectionError("selection record budget is invalid")
    max_candidates = cast(int, value["max_candidates"])
    max_evaluations = cast(int, value["max_evaluations"])
    max_tokens = cast(int, value["max_tokens"])
    if max_candidates > 2 or max_evaluations > 48:
        raise HarnessSelectionError("selection record budget exceeds H9 limits")
    return {
        "max_candidates": max_candidates,
        "max_evaluations": max_evaluations,
        "max_tokens": max_tokens,
    }


def _stored_nonnegative_int(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HarnessSelectionError(f"selection record field {key} is invalid")
    return value


def _candidate_from_raw(raw: Mapping[str, object]) -> HarnessCandidate:
    kind = _stored_text(raw, "kind")
    if kind not in _KINDS:
        raise HarnessSelectionError("selection record candidate kind is invalid")
    return HarnessCandidate(
        candidate_id=_stored_text(raw, "candidate_id"),
        kind=kind,
        target_id=_stored_text(raw, "target_id"),
        baseline_version=_stored_text(raw, "baseline_version"),
        candidate_version=_stored_text(raw, "candidate_version"),
        baseline_hash=_stored_hash(raw, "baseline_hash"),
        candidate_hash=_stored_hash(raw, "candidate_hash"),
        scope=_stored_scope(raw),
        evidence_refs=_stored_string_tuple(raw, "evidence_refs"),
        round_id=_stored_text(raw, "round_id"),
        budget=_stored_budget(raw),
        status=_stored_text(raw, "status"),
        evaluation_id=_stored_optional_text(raw, "evaluation_id"),
        created_at=_stored_text(raw, "created_at"),
    )


def _confirmation_from_raw(raw: Mapping[str, object]) -> HarnessConfirmation:
    scope = _stored_scope(raw)
    return HarnessConfirmation(
        confirmation_id=_stored_text(raw, "confirmation_id"),
        candidate_id=_stored_text(raw, "candidate_id"),
        evaluation_id=_stored_text(raw, "evaluation_id"),
        user_id=_stored_text(raw, "user_id"),
        session_id=_stored_text(raw, "session_id"),
        scope=scope,
        baseline_hash=_stored_hash(raw, "baseline_hash"),
        candidate_hash=_stored_hash(raw, "candidate_hash"),
        evaluation_hash=_stored_hash(raw, "evaluation_hash"),
        consumed_at=_stored_optional_text(raw, "consumed_at"),
        expires_at=_stored_text(raw, "expires_at"),
        invalidated_at=_stored_optional_text(raw, "invalidated_at"),
    )


def _evaluation_from_raw(raw: Mapping[str, object]) -> HarnessEvaluation:
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or any(not isinstance(case, Mapping) for case in raw_cases):
        raise HarnessSelectionError("evaluation cases are invalid")
    cases = tuple(
        cast(dict[str, object], dict(cast(Mapping[str, object], case)))
        for case in raw_cases
    )
    status = raw.get("status", "completed")
    infrastructure_failure = raw.get("infrastructure_failure", False)
    if not isinstance(status, str) or not status or not isinstance(infrastructure_failure, bool):
        raise HarnessSelectionError("evaluation status is invalid")
    return HarnessEvaluation(
        evaluation_id=_stored_text(raw, "evaluation_id"),
        candidate_id=_stored_text(raw, "candidate_id"),
        cases=cases,
        baseline_successes=_stored_nonnegative_int(raw, "baseline_successes"),
        candidate_successes=_stored_nonnegative_int(raw, "candidate_successes"),
        eligible=(
            raw["eligible"]
            if isinstance(raw.get("eligible"), bool)
            else (_raise_invalid_evaluation_field("eligible"))
        ),
        total_runs=_stored_nonnegative_int(raw, "total_runs"),
        result_hash=_stored_hash(raw, "result_hash"),
        created_at=_stored_text(raw, "created_at"),
        status=status,
        infrastructure_failure=infrastructure_failure,
        failure_type=_stored_optional_text(raw, "failure_type"),
        failure_phase=_stored_optional_text(raw, "failure_phase"),
    )


def _raise_invalid_evaluation_field(key: str) -> bool:
    raise HarnessSelectionError(f"evaluation field {key} is invalid")


class HarnessSelectionStore:
    """Use the existing DevPilot database; candidates never define their own suite."""

    def __init__(self, database_path: str | Path, *, integrity_key: bytes) -> None:
        if not integrity_key:
            raise HarnessSelectionError("integrity key is required")
        self.database_path = Path(database_path).expanduser().resolve()
        self.integrity_key = integrity_key
        self._require_table()

    def create_candidate(
        self,
        *,
        kind: str,
        target_id: str,
        baseline_version: str,
        candidate_version: str,
        baseline_hash: str,
        candidate_hash: str,
        scope: Mapping[str, object],
        evidence_refs: Sequence[str],
        round_id: str,
        budget: Mapping[str, object],
    ) -> HarnessCandidate:
        if kind not in _KINDS or not target_id or baseline_version == candidate_version:
            raise HarnessSelectionError("candidate target is invalid")
        if (
            not self._hash(baseline_hash)
            or not self._hash(candidate_hash)
            or baseline_hash == candidate_hash
        ):
            raise HarnessSelectionError("candidate version hashes are invalid")
        normalized_scope = {
            key: value
            for key, value in scope.items()
            if isinstance(key, str) and isinstance(value, str) and value
        }
        if set(normalized_scope) != {"project_id", "workspace"}:
            raise HarnessSelectionError("candidate scope must bind project_id and workspace")
        refs = tuple(dict.fromkeys(ref for ref in evidence_refs if isinstance(ref, str) and ref))
        if not refs:
            raise HarnessSelectionError("candidate requires trusted evidence")
        evidence = [self._read(reference, "evidence") for reference in refs]
        if any(_stored_text(item, "state") != "verified" for item in evidence):
            raise HarnessSelectionError("candidate evidence is not verified")
        if any(
            _stored_text(item, "project_id") != normalized_scope["project_id"]
            or _stored_text(item, "workspace") != normalized_scope["workspace"]
            or _stored_hash(item, "version_hash") != baseline_hash
            for item in evidence
        ):
            raise HarnessSelectionError("candidate evidence does not match its baseline scope")
        if not round_id or len(round_id) > 128:
            raise HarnessSelectionError("selection round is invalid")
        normalized_budget = self._budget(budget)
        candidate = HarnessCandidate(
            candidate_id=f"harness-candidate-{uuid.uuid4()}",
            kind=kind,
            target_id=target_id,
            baseline_version=baseline_version,
            candidate_version=candidate_version,
            baseline_hash=baseline_hash,
            candidate_hash=candidate_hash,
            scope=normalized_scope,
            evidence_refs=refs,
            round_id=round_id,
            budget=normalized_budget,
            status="pending_evaluation",
            evaluation_id=None,
            created_at=_now(),
        )
        # Claim the round slot under SQLite's writer lock so concurrent callers
        # cannot both observe the same remaining candidate capacity.
        with sqlite3.connect(self.database_path, timeout=30, isolation_level=None) as connection:
            connection.execute("begin immediate")
            round_candidates = [
                stored_candidate
                for stored_candidate in (
                    _candidate_from_raw(raw)
                    for raw in self._records_in_transaction(connection, "candidate")
                )
                if stored_candidate.round_id == round_id
            ]
            if len(round_candidates) >= normalized_budget["max_candidates"]:
                connection.rollback()
                raise HarnessSelectionError("selection candidate budget is exhausted")
            if any(item.budget != normalized_budget for item in round_candidates):
                connection.rollback()
                raise HarnessSelectionError("selection round budget cannot be changed")
            if any(
                item.kind != kind or item.target_id != target_id
                for item in round_candidates
            ):
                connection.rollback()
                raise HarnessSelectionError(
                    "selection round can change only one plugin or loop Driver target"
                )
            self._write_in_transaction(
                connection, candidate.candidate_id, "candidate", asdict(candidate)
            )
            connection.commit()
        return candidate

    def record_evidence(
        self,
        *,
        task_id: str,
        project_id: str,
        workspace: str,
        version_hash: str,
        source_type: str,
        summary: str,
        usage: Mapping[str, object] | None = None,
        infrastructure_failure: bool = False,
    ) -> str:
        """Persist a bounded H9 fact before any candidate can cite it."""
        if (
            not task_id
            or not project_id
            or not workspace
            or not summary
            or source_type not in _EVIDENCE_SOURCES
            or not self._hash(version_hash)
        ):
            raise HarnessSelectionError("selection evidence is invalid")
        evidence_id = f"harness-evidence-{uuid.uuid4()}"
        raw_usage = dict(usage or {})
        calls = raw_usage.get("calls", 0)
        input_tokens = raw_usage.get("input_tokens")
        output_tokens = raw_usage.get("output_tokens")
        if (
            not isinstance(calls, int)
            or isinstance(calls, bool)
            or calls < 0
            or input_tokens is not None
            and (
                not isinstance(input_tokens, int)
                or isinstance(input_tokens, bool)
                or input_tokens < 0
            )
            or output_tokens is not None
            and (
                not isinstance(output_tokens, int)
                or isinstance(output_tokens, bool)
                or output_tokens < 0
            )
        ):
            raise HarnessSelectionError("selection evidence usage is invalid")
        self._write(
            evidence_id,
            "evidence",
            {
                "evidence_id": evidence_id,
                "task_id": task_id,
                "project_id": project_id,
                "workspace": workspace,
                "version_hash": version_hash,
                "source_type": source_type,
                "summary": summary[:4000],
                "usage": {
                    "calls": calls,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost": None,
                },
                "state": "infrastructure_failure" if infrastructure_failure else "verified",
            },
        )
        return evidence_id

    def get_candidate(self, candidate_id: str) -> HarnessCandidate:
        return _candidate_from_raw(self._read(candidate_id, "candidate"))

    def _record_evaluation(
        self, candidate_id: str, cases: Sequence[Mapping[str, object]]
    ) -> HarnessEvaluation:
        candidate = self.get_candidate(candidate_id)
        if candidate.status != "pending_evaluation":
            raise HarnessSelectionError("candidate is not pending evaluation")
        if candidate.budget["max_evaluations"] < 24:
            raise HarnessSelectionError("selection evaluation budget cannot fund the fixed suite")
        round_candidate_ids = {
            stored_candidate.candidate_id
            for stored_candidate in self._candidates()
            if stored_candidate.round_id == candidate.round_id
        }
        used_runs = sum(
            stored_evaluation.total_runs
            for stored_evaluation in (
                _evaluation_from_raw(raw) for raw in self._records("evaluation")
            )
            if stored_evaluation.candidate_id in round_candidate_ids
        )
        if used_runs + 24 > candidate.budget["max_evaluations"]:
            raise HarnessSelectionError("selection round evaluation budget is exhausted")
        normalized = tuple(self._case(case) for case in cases)
        expected = {
            f"{category}:{scenario}:{version}:{repeat}"
            for category in _CATEGORIES
            for scenario in (1, 2)
            for version in _VERSIONS
            for repeat in _REPEATS
        }
        if len(normalized) != 24 or {str(case["case_id"]) for case in normalized} != expected:
            raise HarnessSelectionError("evaluation must contain the fixed 24-case suite")
        if any(
            not case["contract_passed"]
            or case["side_effect_count"] != 0
            or case["permission_violation"]
            for case in normalized
        ):
            eligible = False
        else:
            eligible = self._is_improved(normalized)
        used_tokens = sum(
            sum(
                value
                for value in (
                    case["usage"].get("input_tokens"),
                    case["usage"].get("output_tokens"),
                )
                if isinstance(value, int)
            )
            for case in normalized
        )
        if used_tokens > candidate.budget["max_tokens"]:
            eligible = False
        baseline_successes = sum(
            case["success"] for case in normalized if case["version"] == "baseline"
        )
        candidate_successes = sum(
            case["success"] for case in normalized if case["version"] == "candidate"
        )
        evaluation = HarnessEvaluation(
            evaluation_id=f"harness-evaluation-{uuid.uuid4()}",
            candidate_id=candidate_id,
            cases=normalized,
            baseline_successes=baseline_successes,
            candidate_successes=candidate_successes,
            eligible=eligible,
            total_runs=len(normalized),
            result_hash=hashlib.sha256(
                _json(
                    {
                        "candidate_id": candidate_id,
                        "cases": normalized,
                        "eligible": eligible,
                    }
                ).encode("utf-8")
            ).hexdigest(),
            created_at=_now(),
        )
        with sqlite3.connect(self.database_path, timeout=30, isolation_level=None) as connection:
            connection.execute("begin immediate")
            current = self._candidate_in_transaction(connection, candidate_id)
            if current.status != "pending_evaluation":
                connection.rollback()
                raise HarnessSelectionError("candidate changed during evaluation")
            candidates = {
                stored_candidate.candidate_id: stored_candidate
                for stored_candidate in (
                    _candidate_from_raw(raw)
                    for raw in self._records_in_transaction(connection, "candidate")
                )
            }
            used_runs = sum(
                stored_evaluation.total_runs
                for stored_evaluation in (
                    _evaluation_from_raw(raw)
                    for raw in self._records_in_transaction(connection, "evaluation")
                )
                if (
                    stored_evaluation.candidate_id in candidates
                    and candidates[stored_evaluation.candidate_id].round_id == current.round_id
                )
            )
            if used_runs + 24 > current.budget["max_evaluations"]:
                connection.rollback()
                raise HarnessSelectionError("selection round evaluation budget is exhausted")
            self._write_in_transaction(
                connection, evaluation.evaluation_id, "evaluation", asdict(evaluation)
            )
            updated = HarnessCandidate(
                **{
                    **asdict(current),
                    "status": "pending_confirmation" if eligible else "evaluation_failed",
                    "evaluation_id": evaluation.evaluation_id,
                }
            )
            self._write_in_transaction(connection, candidate_id, "candidate", asdict(updated))
            connection.commit()
        return evaluation

    def get_evaluation(self, evaluation_id: str) -> HarnessEvaluation | None:
        """Read one persisted evaluation or infrastructure-failure receipt."""
        try:
            raw = self._read(evaluation_id, "evaluation")
        except HarnessSelectionError as exc:
            if str(exc) == "selection record not found":
                return None
            raise
        return _evaluation_from_raw(raw)

    def run_fixed_evaluation(
        self,
        candidate_id: str,
        host_trial: Callable[[str, str, int, int, Mapping[str, object]], Mapping[str, object]],
    ) -> HarnessEvaluation:
        """Run the H9-owned six scenarios; candidates never provide the suite."""
        cases: list[dict[str, object]] = []
        for category in _CATEGORIES:
            for scenario in (1, 2):
                for version in _VERSIONS:
                    for repeat in _REPEATS:
                        fixture = _FIXTURE_INPUTS[category, scenario]
                        try:
                            result = dict(host_trial(category, version, scenario, repeat, fixture))
                            contract_hash = result.get("fixture_contract_hash")
                            fixture_hash = hashlib.sha256(
                                _json(
                                    {"fixture": fixture, "contract_hash": contract_hash}
                                    if contract_hash is not None
                                    else fixture
                                ).encode()
                            ).hexdigest()
                            case = self._case(
                                {
                                    **result,
                                    "category": category,
                                    "scenario": scenario,
                                    "version": version,
                                    "repeat": repeat,
                                    "fixture_hash": fixture_hash,
                                }
                            )
                        except Exception as exc:
                            fixture_hash = hashlib.sha256(_json(fixture).encode()).hexdigest()
                            phase = f"{category}:{scenario}:{version}:{repeat}"
                            self._record_infrastructure_failure(
                                candidate_id,
                                cases=[
                                    *cases,
                                    {
                                        "case_id": phase,
                                        "category": category,
                                        "scenario": scenario,
                                        "version": version,
                                        "repeat": repeat,
                                        "fixture_hash": fixture_hash,
                                        "status": "infrastructure_failure",
                                    },
                                ],
                                failure_type=type(exc).__name__,
                                failure_phase=phase,
                            )
                            raise HarnessSelectionError(
                                "fixed evaluation stopped after infrastructure failure"
                            ) from exc
                        cases.append(case)
        return self._record_evaluation(candidate_id, cases)

    def _record_infrastructure_failure(
        self,
        candidate_id: str,
        *,
        cases: Sequence[Mapping[str, object]],
        failure_type: str,
        failure_phase: str,
    ) -> HarnessEvaluation:
        """Close a fixed-evaluation attempt without treating infrastructure as a result."""
        with sqlite3.connect(self.database_path, timeout=30, isolation_level=None) as connection:
            connection.execute("begin immediate")
            candidate = self._candidate_in_transaction(connection, candidate_id)
            if candidate.status != "pending_evaluation":
                connection.rollback()
                raise HarnessSelectionError("candidate changed during evaluation")
            evaluation = HarnessEvaluation(
                evaluation_id=f"harness-evaluation-{uuid.uuid4()}",
                candidate_id=candidate_id,
                cases=tuple(dict(case) for case in cases),
                baseline_successes=0,
                candidate_successes=0,
                eligible=False,
                total_runs=len(cases),
                result_hash=hashlib.sha256(
                    _json(
                        {
                            "candidate_id": candidate_id,
                            "cases": cases,
                            "status": "infrastructure_failure",
                            "failure_type": failure_type,
                            "failure_phase": failure_phase,
                        }
                    ).encode("utf-8")
                ).hexdigest(),
                created_at=_now(),
                status="infrastructure_failure",
                infrastructure_failure=True,
                failure_type=failure_type,
                failure_phase=failure_phase,
            )
            self._write_in_transaction(
                connection, evaluation.evaluation_id, "evaluation", asdict(evaluation)
            )
            self._write_in_transaction(
                connection,
                candidate_id,
                "candidate",
                asdict(
                    replace(
                        candidate,
                        status="evaluation_failed",
                        evaluation_id=evaluation.evaluation_id,
                    )
                ),
            )
            connection.commit()
        return evaluation

    def issue_confirmation(
        self, candidate_id: str, *, user_id: str, session_id: str
    ) -> HarnessConfirmation:
        candidate = self.get_candidate(candidate_id)
        if candidate.status != "pending_confirmation" or not candidate.evaluation_id:
            raise HarnessSelectionError("candidate is not eligible for confirmation")
        evaluation = _evaluation_from_raw(self._read(candidate.evaluation_id, "evaluation"))
        if (
            evaluation.status != "completed"
            or evaluation.eligible is not True
            or evaluation.infrastructure_failure is True
        ):
            raise HarnessSelectionError("candidate evaluation is not eligible for confirmation")
        confirmation = HarnessConfirmation(
            confirmation_id=f"harness-confirmation-{uuid.uuid4()}",
            candidate_id=candidate_id,
            evaluation_id=candidate.evaluation_id,
            user_id=user_id,
            session_id=session_id,
            scope=dict(candidate.scope),
            baseline_hash=candidate.baseline_hash,
            candidate_hash=candidate.candidate_hash,
            evaluation_hash=evaluation.result_hash,
            consumed_at=None,
            expires_at=(datetime.now(UTC) + _CONFIRMATION_TTL).isoformat(),
        )
        self._write(confirmation.confirmation_id, "confirmation", asdict(confirmation))
        return confirmation

    def consume_confirmation(
        self, confirmation_id: str, *, user_id: str, session_id: str, evaluation_id: str
    ) -> HarnessCandidate:
        """Atomically activate one fully bound candidate without replaying work."""
        with sqlite3.connect(self.database_path, timeout=30, isolation_level=None) as connection:
            connection.execute("begin immediate")
            raw_confirmation = self._read_in_transaction(
                connection, confirmation_id, "confirmation"
            )
            confirmation = _confirmation_from_raw(raw_confirmation)
            if confirmation.user_id != user_id or confirmation.session_id != session_id:
                connection.rollback()
                raise HarnessSelectionError("confirmation scope mismatch")
            if confirmation.evaluation_id != evaluation_id:
                connection.rollback()
                raise HarnessSelectionError("confirmation evaluation changed")
            if confirmation.invalidated_at is not None:
                connection.rollback()
                raise HarnessSelectionError("confirmation is no longer valid")
            if self._confirmation_expired(confirmation.expires_at):
                connection.rollback()
                raise HarnessSelectionError("confirmation has expired")
            evaluation = _evaluation_from_raw(
                self._read_in_transaction(connection, evaluation_id, "evaluation")
            )
            if (
                evaluation.status != "completed"
                or evaluation.eligible is not True
                or evaluation.infrastructure_failure is True
            ):
                connection.rollback()
                raise HarnessSelectionError("evaluation is not eligible for confirmation")
            if evaluation.result_hash != confirmation.evaluation_hash:
                connection.rollback()
                raise HarnessSelectionError("evaluation result changed after confirmation")
            candidate = self._candidate_in_transaction(connection, confirmation.candidate_id)
            if (
                candidate.evaluation_id != evaluation_id
                or candidate.scope != confirmation.scope
                or candidate.baseline_hash != confirmation.baseline_hash
                or candidate.candidate_hash != confirmation.candidate_hash
            ):
                connection.rollback()
                raise HarnessSelectionError("candidate changed after confirmation")
            if confirmation.consumed_at is not None or candidate.status == "active":
                connection.commit()
                return replace(candidate, status="already_consumed")
            if candidate.status != "pending_confirmation":
                connection.rollback()
                raise HarnessSelectionError("candidate changed after confirmation")
            if any(
                other.candidate_id != candidate.candidate_id
                and other.kind == candidate.kind
                and other.scope == candidate.scope
                and other.status == "active"
                for other in self._candidates()
            ):
                connection.rollback()
                raise HarnessSelectionError("an active candidate already owns this scope")
            activated = replace(candidate, status="active")
            self._write_in_transaction(
                connection, candidate.candidate_id, "candidate", asdict(activated)
            )
            self._write_in_transaction(
                connection,
                confirmation_id,
                "confirmation",
                asdict(replace(confirmation, consumed_at=_now())),
            )
            connection.commit()
        return activated

    def get_confirmation(self, confirmation_id: str) -> HarnessConfirmation:
        """Read a confirmation binding before native-command consumption."""
        return _confirmation_from_raw(self._read(confirmation_id, "confirmation"))

    def rollback_confirmation_consumption(
        self, confirmation_id: str, *, candidate_id: str
    ) -> HarnessCandidate:
        """Undo an unreferenced selection claim when materialization fails.

        The native command consumes the durable claim before copying a plugin
        artifact into project scope.  If that metadata-only copy fails, this
        restores the pending state so a failed promotion cannot leave a
        project selection falsely marked active.
        """
        with sqlite3.connect(self.database_path, timeout=30, isolation_level=None) as connection:
            connection.execute("begin immediate")
            raw_confirmation = self._read_in_transaction(
                connection, confirmation_id, "confirmation"
            )
            confirmation = _confirmation_from_raw(raw_confirmation)
            if confirmation.candidate_id != candidate_id:
                connection.rollback()
                raise HarnessSelectionError("confirmation candidate changed")
            current = self._candidate_in_transaction(connection, candidate_id)
            if current.status != "active":
                connection.rollback()
                raise HarnessSelectionError("candidate is not an active confirmation claim")
            if any(
                _stored_text(record, "candidate_id") == candidate_id
                for record in self._records_in_transaction(connection, "task_reference")
            ):
                connection.rollback()
                raise HarnessSelectionError("candidate already has a task reference")
            restored = replace(current, status="pending_confirmation")
            self._write_in_transaction(connection, candidate_id, "candidate", asdict(restored))
            self._write_in_transaction(
                connection,
                confirmation_id,
                "confirmation",
                asdict(replace(confirmation, consumed_at=None)),
            )
            connection.commit()
        return restored

    def record_task_reference(self, candidate_id: str, *, task_id: str, recoverable: bool) -> None:
        if not task_id or not isinstance(recoverable, bool):
            raise HarnessSelectionError("task reference is invalid")
        with sqlite3.connect(self.database_path, timeout=30, isolation_level=None) as connection:
            connection.execute("begin immediate")
            candidate = self._candidate_in_transaction(connection, candidate_id)
            if candidate.status != "active":
                connection.rollback()
                raise HarnessSelectionError("only an active candidate can be referenced")
            self._write_in_transaction(
                connection,
                f"harness-task-reference-{candidate_id}-{task_id}",
                "task_reference",
                {
                    "candidate_id": candidate_id,
                    "task_id": task_id,
                    "recoverable": recoverable,
                },
            )
            connection.commit()

    def retire_candidate(self, candidate_id: str) -> HarnessCandidate:
        with sqlite3.connect(self.database_path, timeout=30, isolation_level=None) as connection:
            connection.execute("begin immediate")
            candidate = self._candidate_in_transaction(connection, candidate_id)
            if candidate.status not in {"active", "pending_confirmation", "evaluation_failed"}:
                connection.rollback()
                raise HarnessSelectionError("candidate cannot be retired")
            references = self._records_in_transaction(connection, "task_reference")
            if any(
                _stored_text(record, "candidate_id") == candidate_id
                and record.get("recoverable") is True
                for record in references
            ):
                connection.rollback()
                raise HarnessSelectionError("candidate has a recoverable task reference")
            retired = replace(candidate, status="retired")
            self._write_in_transaction(connection, candidate_id, "candidate", asdict(retired))
            for raw_confirmation in self._records_in_transaction(connection, "confirmation"):
                confirmation = _confirmation_from_raw(raw_confirmation)
                if (
                    confirmation.candidate_id == candidate_id
                    and confirmation.consumed_at is None
                    and confirmation.invalidated_at is None
                ):
                    self._write_in_transaction(
                        connection,
                        confirmation.confirmation_id,
                        "confirmation",
                        asdict(replace(confirmation, invalidated_at=_now())),
                    )
            connection.commit()
        return retired

    def active_loop_driver(self, scope: Mapping[str, object]) -> tuple[str, str] | None:
        """Resolve a persistent H9 Loop version for a *new* exact-scope task."""
        candidate = self.active_loop_candidate(scope)
        return (
            (candidate.target_id, candidate.candidate_version)
            if candidate is not None
            else None
        )

    def active_plugin_version(self, scope: Mapping[str, object]) -> tuple[str, str] | None:
        """Resolve one persistent H9 plugin version for an exact new-task scope."""
        return self._active_version("plugin", scope)

    def active_loop_candidate(self, scope: Mapping[str, object]) -> HarnessCandidate | None:
        """Return the full immutable binding needed by a remote Loop Driver."""
        normalized_scope = {key: value for key, value in scope.items()}
        matches = [
            candidate
            for candidate in self._candidates()
            if candidate.kind == "loop"
            and candidate.status == "active"
            and candidate.scope == normalized_scope
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise HarnessSelectionError("multiple active loop candidates share a scope")
        return matches[0]

    def _active_version(self, kind: str, scope: Mapping[str, object]) -> tuple[str, str] | None:
        normalized_scope = {key: value for key, value in scope.items()}
        matches = [
            candidate
            for candidate in self._candidates()
            if candidate.kind == kind
            and candidate.status == "active"
            and candidate.scope == normalized_scope
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise HarnessSelectionError(f"multiple active {kind} candidates share a scope")
        return matches[0].target_id, matches[0].candidate_version

    @staticmethod
    def _hash(value: str) -> bool:
        return len(value) == 64 and all(char in "0123456789abcdef" for char in value)

    @staticmethod
    def _budget(raw: Mapping[str, object]) -> dict[str, int]:
        values = {key: raw.get(key) for key in ("max_candidates", "max_evaluations", "max_tokens")}
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in values.values()
        ):
            raise HarnessSelectionError("selection budget must be set before evaluation")
        if values["max_candidates"] > 2 or values["max_evaluations"] > 48:
            raise HarnessSelectionError("selection budget exceeds H9 limits")
        return {key: int(value) for key, value in values.items()}

    @staticmethod
    def _case(raw: Mapping[str, object]) -> dict[str, object]:
        category, scenario, version, repeat = (
            raw.get("category"),
            raw.get("scenario"),
            raw.get("version"),
            raw.get("repeat"),
        )
        if (
            category not in _CATEGORIES
            or scenario not in (1, 2)
            or version not in _VERSIONS
            or repeat not in _REPEATS
        ):
            raise HarnessSelectionError("evaluation case identity is invalid")
        contract_hash = raw.get("fixture_contract_hash")
        if contract_hash is not None and (
            not isinstance(contract_hash, str) or not HarnessSelectionStore._hash(contract_hash)
        ):
            raise HarnessSelectionError("evaluation fixture contract binding is invalid")
        expected_fixture_hash = hashlib.sha256(
            _json(
                {
                    "fixture": _FIXTURE_INPUTS[category, scenario],
                    "contract_hash": contract_hash,
                }
                if contract_hash is not None
                else _FIXTURE_INPUTS[category, scenario]
            ).encode()
        ).hexdigest()
        if raw.get("fixture_hash") != expected_fixture_hash:
            raise HarnessSelectionError("evaluation fixture binding is invalid")
        if not isinstance(raw.get("success"), bool) or not isinstance(
            raw.get("contract_passed"), bool
        ):
            raise HarnessSelectionError("evaluation case result is invalid")
        side_effect_count = raw.get("side_effect_count")
        permission_violation = raw.get("permission_violation")
        if (
            not isinstance(side_effect_count, int)
            or isinstance(side_effect_count, bool)
            or side_effect_count < 0
            or not isinstance(permission_violation, bool)
        ):
            raise HarnessSelectionError("evaluation case safety result is invalid")
        usage = raw.get("usage")
        if not isinstance(usage, Mapping) or not isinstance(usage.get("calls"), int) or isinstance(
            usage.get("calls"), bool
        ) or usage["calls"] < 0:
            raise HarnessSelectionError("evaluation must retain actual token and call usage")
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise HarnessSelectionError("evaluation token usage is invalid")
        return {
            "case_id": f"{category}:{scenario}:{version}:{repeat}",
            "category": category,
            "scenario": scenario,
            "version": version,
            "repeat": repeat,
            "fixture_hash": expected_fixture_hash,
            "fixture_contract_hash": contract_hash,
            "success": raw["success"],
            "contract_passed": raw["contract_passed"],
            "side_effect_count": side_effect_count,
            "permission_violation": permission_violation,
            "usage": {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "calls": usage["calls"],
            },
        }

    @staticmethod
    def _confirmation_expired(expires_at: str) -> bool:
        if not expires_at:
            return True
        try:
            deadline = datetime.fromisoformat(expires_at)
        except ValueError as exc:
            raise HarnessSelectionError("confirmation expiry is invalid") from exc
        if deadline.tzinfo is None:
            raise HarnessSelectionError("confirmation expiry is invalid")
        return datetime.now(UTC) >= deadline.astimezone(UTC)

    @staticmethod
    def _is_improved(cases: Sequence[Mapping[str, object]]) -> bool:
        totals = {
            (category, version): sum(
                case["success"]
                for case in cases
                if case["category"] == category and case["version"] == version
            )
            for category in _CATEGORIES
            for version in _VERSIONS
        }
        return totals["target", "candidate"] > totals["target", "baseline"] and all(
            totals[category, "candidate"] >= totals[category, "baseline"]
            for category in ("holdout", "regression")
        )

    def _require_table(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            present = connection.execute(
                "select 1 from sqlite_master where type = 'table' "
                "and name = 'harness_selection_records'"
            ).fetchone()
        if present is None:
            raise HarnessSelectionError("database schema must be upgraded for H9")

    def _write(self, record_id: str, record_type: str, payload: Mapping[str, object]) -> None:
        with sqlite3.connect(self.database_path) as connection:
            self._write_in_transaction(connection, record_id, record_type, payload)

    def _read(self, record_id: str, record_type: str) -> dict[str, object]:
        with sqlite3.connect(self.database_path) as connection:
            return self._read_in_transaction(connection, record_id, record_type)

    def _records(self, record_type: str) -> list[dict[str, object]]:
        with sqlite3.connect(self.database_path) as connection:
            return self._records_in_transaction(connection, record_type)

    def _records_in_transaction(
        self, connection: sqlite3.Connection, record_type: str
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            "select payload_json, record_mac from harness_selection_records "
            "where record_type = ?",
            (record_type,),
        ).fetchall()
        records: list[dict[str, object]] = []
        for payload, mac in rows:
            if not isinstance(payload, str) or not isinstance(mac, str):
                raise HarnessSelectionError("selection record encoding is invalid")
            expected = hmac.new(self.integrity_key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(mac, expected):
                raise HarnessSelectionError("selection record integrity check failed")
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise HarnessSelectionError("selection record is invalid")
            records.append(cast(dict[str, object], value))
        return records

    def _candidates(self) -> list[HarnessCandidate]:
        return [_candidate_from_raw(raw) for raw in self._records("candidate")]

    def _read_in_transaction(
        self, connection: sqlite3.Connection, record_id: str, record_type: str
    ) -> dict[str, object]:
        row = connection.execute(
            "select payload_json, record_mac from harness_selection_records "
            "where record_id = ? and record_type = ?",
            (record_id, record_type),
        ).fetchone()
        if row is None:
            raise HarnessSelectionError("selection record not found")
        payload, mac = row
        if not isinstance(payload, str) or not isinstance(mac, str):
            raise HarnessSelectionError("selection record encoding is invalid")
        expected = hmac.new(self.integrity_key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            raise HarnessSelectionError("selection record integrity check failed")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise HarnessSelectionError("selection record is invalid")
        return cast(dict[str, object], value)

    def _write_in_transaction(
        self,
        connection: sqlite3.Connection,
        record_id: str,
        record_type: str,
        payload: Mapping[str, object],
    ) -> None:
        encoded = _json(payload)
        mac = hmac.new(self.integrity_key, encoded.encode(), hashlib.sha256).hexdigest()
        connection.execute(
            "insert or replace into harness_selection_records "
            "(record_id, record_type, payload_json, record_mac, created_at) "
            "values (?, ?, ?, ?, ?)",
            (record_id, record_type, encoded, mac, _now()),
        )

    def _candidate_in_transaction(
        self, connection: sqlite3.Connection, candidate_id: str
    ) -> HarnessCandidate:
        return _candidate_from_raw(
            self._read_in_transaction(connection, candidate_id, "candidate")
        )
