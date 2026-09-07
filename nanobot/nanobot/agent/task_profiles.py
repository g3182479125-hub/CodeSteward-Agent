"""Trusted task profiles and bounded loop detection for nanobot sessions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

TASK_PROFILE_METADATA_KEY = "task_profile"
TASK_PROFILE_VERSION_METADATA_KEY = "task_profile_version"


class TaskProfileError(ValueError):
    """A task profile is missing, invalid, or cannot run in this process."""


@dataclass(frozen=True)
class TaskProfile:
    """A developer-owned capability and output contract for one task family."""

    name: str
    skill_names: tuple[str, ...]
    allowed_tools: frozenset[str]
    output_contract: tuple[str, ...]
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.name or not self.skill_names or not self.allowed_tools or not self.output_contract:
            raise ValueError("task profile fields must not be empty")
        if len(set(self.skill_names)) != len(self.skill_names):
            raise ValueError("task profile skill names must be unique")


DEFAULT_TASK_PROFILES: Mapping[str, TaskProfile] = {
    "repo-analysis": TaskProfile(
        name="repo-analysis",
        skill_names=("repo-analysis",),
        allowed_tools=frozenset({
            "devpilot_inspect_project",
            "devpilot_publish_artifact",
        }),
        output_contract=(
            "technical_stack",
            "entry_points",
            "tests",
            "dependencies",
            "risks",
            "unknowns",
            "evidence_files",
        ),
    ),
    "feature-delivery": TaskProfile(
        name="feature-delivery",
        skill_names=("feature-delivery",),
        allowed_tools=frozenset({
            "devpilot_inspect_project",
            "devpilot_search_code",
            "devpilot_read_code",
            "devpilot_feature_delivery_propose",
            "devpilot_propose_changeset",
            "devpilot_request_approval",
            "devpilot_publish_artifact",
            "devpilot_get_changeset",
            "devpilot_get_delivery_report",
            "devpilot_get_run_state",
            "devpilot_watch_run",
            "devpilot_confirmed_action",
        }),
        output_contract=(
            "facts_and_evidence",
            "change_summary",
            "verification_result",
            "next_step",
        ),
    ),
    "bug-fix": TaskProfile(
        name="bug-fix",
        skill_names=("bug-fix",),
        allowed_tools=frozenset({
            "devpilot_inspect_project",
            "devpilot_search_code",
            "devpilot_read_code",
            "devpilot_bug_fix_propose",
            "devpilot_propose_changeset",
            "devpilot_request_approval",
            "devpilot_get_changeset",
            "devpilot_get_delivery_report",
            "devpilot_get_run_state",
            "devpilot_watch_run",
            "devpilot_confirmed_action",
        }),
        output_contract=(
            "failure_evidence",
            "root_cause",
            "change_summary",
            "verification_result",
            "next_step",
        ),
    ),
}


class TaskProfileCatalog:
    """Resolve and validate developer-owned profiles without model input."""

    def __init__(self, profiles: Mapping[str, TaskProfile] | None = None) -> None:
        values = dict(profiles or DEFAULT_TASK_PROFILES)
        if set(values) != {profile.name for profile in values.values()}:
            raise ValueError("task profile map keys must match profile names")
        self._profiles = values

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def get(self, name: str) -> TaskProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            available = ", ".join(self.names) or "(none)"
            raise TaskProfileError(f"unknown task profile '{name}'; available: {available}") from exc

    def validate_runtime(self, profile: TaskProfile, registered_tools: set[str], skills: set[str]) -> None:
        missing_tools = sorted(profile.allowed_tools - registered_tools)
        missing_skills = sorted(set(profile.skill_names) - skills)
        if missing_tools or missing_skills:
            details: list[str] = []
            if missing_tools:
                details.append("tools=" + ",".join(missing_tools))
            if missing_skills:
                details.append("skills=" + ",".join(missing_skills))
            raise TaskProfileError(
                f"task profile '{profile.name}' is unavailable: " + "; ".join(details)
            )

    def describe(self, current: str | None = None) -> str:
        lines = ["Task profiles:"]
        for name in self.names:
            marker = " (active)" if name == current else ""
            profile = self._profiles[name]
            lines.append(
                f"- {name}{marker}: skills={','.join(profile.skill_names)}; "
                f"tools={len(profile.allowed_tools)}; output={','.join(profile.output_contract)}"
            )
        return "\n".join(lines)


def _stable_json(value: Any) -> str:
    """Serialize only for hashing; raw parameters never leave this process."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(value)


@dataclass
class TaskLoopGuard:
    """Pause a profiled task after repeated identical failed tool attempts."""

    same_error_limit: int = 3
    max_diagnostic_branches: int = 1
    _last_fingerprint: str | None = field(default=None, init=False)
    _consecutive: int = field(default=0, init=False)
    _seen_errors: set[str] = field(default_factory=set, init=False)
    _branches: int = field(default=0, init=False)

    def observe(self, tool_name: str, params: Any, error: str) -> str | None:
        normalized_error = error.split("\n", 1)[0].strip()[:240]
        fingerprint = hashlib.sha256(
            _stable_json({"tool": tool_name, "params": params, "error": normalized_error}).encode()
        ).hexdigest()
        if fingerprint == self._last_fingerprint:
            self._consecutive += 1
        else:
            if self._seen_errors:
                self._branches += 1
            self._last_fingerprint = fingerprint
            self._consecutive = 1
            self._seen_errors.add(fingerprint)

        if self._consecutive >= self.same_error_limit:
            return (
                "Task paused: the same tool error repeated without new evidence "
                f"({self._consecutive} attempts). Known error: {normalized_error}. "
                "Report the known facts, unknowns, and the next diagnostic step; do not retry it."
            )
        if self._branches > self.max_diagnostic_branches:
            return (
                "Task paused: the bounded diagnostic-branch budget was exhausted. "
                "Report the known facts, unknowns, and the next diagnostic step."
            )
        return None

    def observe_success(self) -> None:
        """Reset only the consecutive-error streak after new evidence succeeds."""

        self._last_fingerprint = None
        self._consecutive = 0
