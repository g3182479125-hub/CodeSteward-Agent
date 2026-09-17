"""Trusted task profiles and bounded loop detection for nanobot sessions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from nanobot.agent.plugins.contracts import DynamicPluginPolicy

TASK_PROFILE_METADATA_KEY = "task_profile"
TASK_PROFILE_VERSION_METADATA_KEY = "task_profile_version"
TASK_CONTEXT_METADATA_KEY = "task_context"
TASK_LOOP_GUARD_METADATA_KEY = "task_loop_guard"

_SENSITIVE_CONTEXT_RE = re.compile(
    r"(?i)(?:authorization\s*[:=]\s*bearer\s+[^\s,;]+|"
    r"(?:api[_ -]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,;]+)"
)


class TaskProfileError(ValueError):
    """A task profile is missing, invalid, or cannot run in this process."""


MODEL_CANDIDATE_POLICY = DynamicPluginPolicy(
    enabled=True,
    allowed_kinds=frozenset({"tool", "loop"}),
    allowed_capabilities=frozenset(),
    allowed_actions=frozenset({"activate", "stop", "remove", "switch-loop"}),
    allowed_scopes=frozenset({"session", "project"}),
    max_active_plugins=3,
)


@dataclass(frozen=True)
class TaskProfile:
    """A developer-owned capability and output contract for one task family."""

    name: str
    skill_names: tuple[str, ...]
    allowed_tools: frozenset[str]
    output_contract: tuple[str, ...]
    version: str = "1"
    dynamic_plugins: DynamicPluginPolicy = field(default_factory=DynamicPluginPolicy)

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
        version="2",
        allowed_tools=frozenset({
            "devpilot_inspect_project",
            "devpilot_search_code",
            "devpilot_read_code",
            "devpilot_feature_delivery_propose",
            "devpilot_request_approval",
            "devpilot_publish_artifact",
            "devpilot_get_changeset",
            "devpilot_get_delivery_report",
            "devpilot_get_run_state",
            "devpilot_watch_run",
            "devpilot_confirmed_action",
            "devpilot_model_plugin",
        }),
        output_contract=(
            "facts_and_evidence",
            "change_summary",
            "verification_result",
            "next_step",
        ),
        dynamic_plugins=MODEL_CANDIDATE_POLICY,
    ),
    "bug-fix": TaskProfile(
        name="bug-fix",
        skill_names=("bug-fix",),
        allowed_tools=frozenset({
            "devpilot_inspect_project",
            "devpilot_search_code",
            "devpilot_read_code",
            "devpilot_test_run",
            "devpilot_bug_fix_propose",
            "devpilot_propose_changeset",
            "devpilot_request_approval",
            "devpilot_get_changeset",
            "devpilot_get_delivery_report",
            "devpilot_get_run_state",
            "devpilot_watch_run",
            "devpilot_confirmed_action",
            "devpilot_skill_feedback",
            "devpilot_skill_candidate",
            "devpilot_model_plugin",
        }),
        output_contract=(
            "failure_evidence",
            "root_cause",
            "change_summary",
            "verification_result",
            "next_step",
        ),
        dynamic_plugins=MODEL_CANDIDATE_POLICY,
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


@dataclass(frozen=True)
class TaskContextState:
    """Small, non-authoritative context record carried by one profiled session."""

    session_ref: str
    project_ref: str
    profile_binding: str
    goal: str = ""
    constraints: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    next_step: str = ""
    task_id: str | None = None
    run_id: str | None = None
    change_set_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    file_hashes: tuple[tuple[str, str], ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        """Return a JSON-safe copy; this record never contains credentials."""
        return {
            "session_ref": self.session_ref,
            "project_ref": self.project_ref,
            "profile_binding": self.profile_binding,
            "goal": _safe_context_text(self.goal),
            "constraints": [_safe_context_text(item) for item in self.constraints],
            "completed": [_safe_context_text(item) for item in self.completed],
            "next_step": _safe_context_text(self.next_step),
            "task_id": _optional_context_string(self.task_id),
            "run_id": _optional_context_string(self.run_id),
            "change_set_id": _optional_context_string(self.change_set_id),
            "evidence_refs": [_safe_context_text(item) for item in self.evidence_refs],
            "file_hashes": [
                {"path": _safe_context_text(path), "sha256": digest}
                for path, digest in self.file_hashes
            ],
        }

    def prompt_text(self) -> str:
        """Render only bounded facts and references for the model context."""
        fields = self.to_metadata()
        return "\n".join(f"- {key}: {value}" for key, value in fields.items() if value not in (None, "", []))

    @classmethod
    def from_metadata(cls, value: Any) -> TaskContextState | None:
        if not isinstance(value, dict):
            return None
        required = ("session_ref", "project_ref", "profile_binding")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            return None
        return cls(
            session_ref=value["session_ref"],
            project_ref=value["project_ref"],
            profile_binding=value["profile_binding"],
            goal=_safe_context_text(value.get("goal", "")),
            constraints=_context_strings(value.get("constraints")),
            completed=_context_strings(value.get("completed")),
            next_step=_safe_context_text(value.get("next_step", "")),
            task_id=_optional_context_string(value.get("task_id")),
            run_id=_optional_context_string(value.get("run_id")),
            change_set_id=_optional_context_string(value.get("change_set_id")),
            evidence_refs=_context_strings(value.get("evidence_refs")),
            file_hashes=_context_file_hashes(value.get("file_hashes")),
        )


def _safe_context_text(value: Any) -> str:
    text = str(value or "").strip()[:512]
    return _SENSITIVE_CONTEXT_RE.sub(
        lambda match: re.split(r"[:=]", match.group(0), maxsplit=1)[0].strip()
        + "=[REDACTED]",
        text,
    )


def _context_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(_safe_context_text(item) for item in value if isinstance(item, str))[:20]


def _optional_context_string(value: Any) -> str | None:
    return _safe_context_text(value) if isinstance(value, str) and value else None


def _context_file_hashes(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        return ()
    refs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path, digest = item.get("path"), item.get("sha256")
        if (
            isinstance(path, str)
            and isinstance(digest, str)
            and len(digest) == 64
            and all(char in "0123456789abcdefABCDEF" for char in digest)
        ):
            refs.append((_safe_context_text(path), digest.lower()))
    return tuple(refs[:20])


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
        """Reset the current diagnostic sequence after new evidence succeeds.

        A successful tool call establishes fresh evidence.  Errors before that
        success must not consume the bounded diagnostic budget for the next
        step: ordinary multi-step tasks routinely recover from a malformed
        tool call, inspect more code, and then encounter an unrelated error.
        """

        self._last_fingerprint = None
        self._consecutive = 0
        self._seen_errors.clear()
        self._branches = 0

    def to_state(self, profile_binding: str) -> dict[str, Any]:
        """Serialize bounded counters and hashes for the current profile version."""
        return {
            "profile_binding": profile_binding,
            "same_error_limit": self.same_error_limit,
            "max_diagnostic_branches": self.max_diagnostic_branches,
            "last_fingerprint": self._last_fingerprint,
            "consecutive": self._consecutive,
            "seen_errors": sorted(self._seen_errors)[:20],
            "branches": self._branches,
        }

    @classmethod
    def from_state(cls, value: Any, profile_binding: str) -> TaskLoopGuard:
        """Restore a guard only when its state belongs to the active profile version."""
        guard = cls()
        if not isinstance(value, dict) or value.get("profile_binding") != profile_binding:
            return guard
        last = value.get("last_fingerprint")
        seen = value.get("seen_errors")
        if isinstance(last, str) and len(last) == 64:
            guard._last_fingerprint = last
        if isinstance(value.get("consecutive"), int) and value["consecutive"] >= 0:
            guard._consecutive = min(value["consecutive"], guard.same_error_limit)
        if isinstance(seen, list):
            guard._seen_errors = {item for item in seen if isinstance(item, str) and len(item) == 64}
        if isinstance(value.get("branches"), int) and value["branches"] >= 0:
            guard._branches = min(value["branches"], guard.max_diagnostic_branches + 1)
        return guard
