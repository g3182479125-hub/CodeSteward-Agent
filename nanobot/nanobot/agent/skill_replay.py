"""Run one Skill-improvement trial in an isolated AgentRunner worktree.

The replay module deliberately keeps the worktree provider at a small seam.
The DevPilot control plane owns trusted Git snapshot creation; nanobot owns the
actual AgentRunner execution and profile tool restrictions.  This prevents a
replay from quietly falling back to the user's live project.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import ProviderConversationState
from nanobot.utils.llm_runtime import LLMRuntime

REPLAY_VERSIONS = frozenset({"baseline", "candidate"})
REPLAY_STATUSES = frozenset(
    {"passed", "failed", "timeout", "cancelled", "infrastructure_error"}
)
_PROTECTED_SKILL_PREFIX = "skills/"


class SkillReplayError(ValueError):
    """Raised when a replay request cannot satisfy the isolation contract."""


@dataclass(frozen=True, slots=True)
class ReplayWorktreeLease:
    """A server-created worktree and its mandatory cleanup callback."""

    path: Path
    cleanup: Callable[[], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    """Trusted inputs for one baseline or candidate replay.

    ``worktree_factory`` must create a fresh private worktree from the same
    server-captured snapshot for every request.  It is intentionally injected
    so the control plane can reuse its existing Git snapshot implementation.
    """

    profile_name: str
    skill_id: str
    version: str
    skill_relative_path: str
    snapshot_ref: str
    worktree_factory: Callable[[], ReplayWorktreeLease]
    initial_messages: tuple[Mapping[str, Any], ...]
    tools: ToolRegistry
    runtime: LLMRuntime
    allowed_tools: frozenset[str]
    task_id: str
    project_id: str
    session_key: str
    model_id: str
    max_iterations: int = 8
    max_tool_result_chars: int = 12_000
    context_block_limit: int | None = None
    llm_timeout_s: float | None = 600.0
    provider_state: ProviderConversationState | None = None


@dataclass(frozen=True, slots=True)
class ReplayTrialResult:
    """Non-persistent result returned from one isolated AgentRunner trial."""

    status: str
    final_output: str
    tool_events: tuple[Mapping[str, str], ...]
    tools_used: tuple[str, ...]
    usage: Mapping[str, int]
    duration_seconds: float
    changed_paths: tuple[str, ...]
    worktree_path: str
    error: str | None = None
    cleanup_error: str | None = None

    @property
    def token_count(self) -> int:
        total = self.usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool):
            return max(0, total)
        return max(
            0,
            sum(
                value
                for key, value in self.usage.items()
                if key in {"prompt_tokens", "completion_tokens"}
                and isinstance(value, int)
                and not isinstance(value, bool)
            ),
        )


@dataclass(frozen=True, slots=True)
class ReplayGrade:
    """A deterministic, server-owned assertion over a replay result."""

    grader_kind: str
    passed: bool
    reason: str
    changed_paths: tuple[str, ...] = ()


def _validate_request(request: ReplayRequest) -> None:
    if request.version not in REPLAY_VERSIONS:
        raise SkillReplayError("replay version must be baseline or candidate")
    if not request.profile_name or not request.skill_id:
        raise SkillReplayError("replay profile and Skill are required")
    relative = Path(request.skill_relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise SkillReplayError("replay Skill path is unsafe")
    if not request.skill_relative_path.startswith(_PROTECTED_SKILL_PREFIX):
        raise SkillReplayError("replay Skill path must be under skills/")
    if not request.snapshot_ref:
        raise SkillReplayError("replay snapshot reference is required")
    if request.max_iterations <= 0:
        raise SkillReplayError("replay iteration budget must be positive")
    if request.max_tool_result_chars <= 0:
        raise SkillReplayError("replay tool-result budget must be positive")
    if not request.allowed_tools.issubset(set(request.tools.tool_names)):
        raise SkillReplayError("replay allowed tools are not registered")


def _skill_message(messages: Sequence[Mapping[str, Any]], skill_content: str) -> list[dict[str, Any]]:
    """Add a trusted Skill snapshot without mutating the caller's messages."""

    if not isinstance(skill_content, str) or not skill_content.strip():
        raise SkillReplayError("replay Skill content is empty")
    result = [deepcopy(dict(message)) for message in messages]
    block = (
        "\n\n<devpilot-replay-skill>\n"
        "The following Skill is server-selected for this isolated replay.\n"
        f"{skill_content}\n"
        "</devpilot-replay-skill>"
    )
    for message in result:
        if message.get("role") == "system":
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = content + block
            else:
                message["content"] = block.lstrip()
            return result
    result.insert(0, {"role": "system", "content": block.lstrip()})
    return result


def _changed_paths(worktree: Path) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillReplayError("replay worktree status is unavailable") from exc
    if result.returncode != 0:
        raise SkillReplayError("replay worktree status failed")
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[-1]
        if value.startswith(('"', "'")) and value[-1:] == value[0]:
            value = value[1:-1]
        paths.append(value.replace("\\", "/"))
    return tuple(sorted(set(paths)))


def _status_from_result(result: Any) -> tuple[str, str | None]:
    if getattr(result, "error", None):
        return "infrastructure_error", str(result.error)
    stop_reason = str(getattr(result, "stop_reason", "completed"))
    if stop_reason in {"cancelled", "canceled"}:
        return "cancelled", None
    if stop_reason in {"timeout", "timed_out"}:
        return "timeout", None
    if stop_reason in {"error", "provider_error", "remote_driver_unavailable"}:
        return "infrastructure_error", stop_reason
    if stop_reason in {"max_iterations", "driver_paused", "driver_finished"}:
        return "failed", stop_reason
    return "passed", None


async def _cleanup(lease: ReplayWorktreeLease) -> str | None:
    try:
        outcome = lease.cleanup()
        if asyncio.iscoroutine(outcome):
            await outcome
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # cleanup is reported, never silently swallowed
        return str(exc)
    return None


async def run_skill_replay(
    request: ReplayRequest,
    skill_content: str,
) -> ReplayTrialResult:
    """Run one Skill with the shared AgentRunner in a fresh private worktree."""

    _validate_request(request)
    lease = request.worktree_factory()
    worktree = lease.path.expanduser().resolve()
    started = time.perf_counter()
    result: Any = None
    error: str | None = None
    cleanup_error: str | None = None
    status = "infrastructure_error"
    changed: tuple[str, ...] = ()
    try:
        skill_path = worktree / request.skill_relative_path
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill_content, encoding="utf-8")
        context = RequestContext(
            channel="skill-replay",
            chat_id=request.session_key,
            session_key=request.session_key,
            runtime=request.runtime,
            workspace=worktree,
            attributes={
                "devpilot_replay": True,
                "devpilot_profile": request.profile_name,
                "devpilot_skill_id": request.skill_id,
                "devpilot_snapshot_ref": request.snapshot_ref,
            },
        )
        runner = AgentRunner()
        with request_context(context):
            result = await runner.run(
                AgentRunSpec(
                    initial_messages=_skill_message(request.initial_messages, skill_content),
                    tools=request.tools.restricted_to(request.allowed_tools),
                    runtime=request.runtime,
                    max_iterations=request.max_iterations,
                    max_tool_result_chars=request.max_tool_result_chars,
                    workspace=worktree,
                    session_key=request.session_key,
                    task_id=request.task_id,
                    project_id=request.project_id,
                    context_block_limit=request.context_block_limit,
                    llm_timeout_s=request.llm_timeout_s,
                    strict_streaming_timeout=request.llm_timeout_s is not None,
                    error_message="replay execution failed",
                    fail_on_tool_error=True,
                    stream_progress_deltas=False,
                    provider_state=request.provider_state,
                )
            )
        status, error = _status_from_result(result)
        changed = _changed_paths(worktree)
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except (OSError, SkillReplayError, RuntimeError) as exc:
        status = "infrastructure_error"
        error = str(exc)
        try:
            changed = _changed_paths(worktree)
        except SkillReplayError:
            changed = ()
    finally:
        cleanup_error = await _cleanup(lease)
        if cleanup_error is not None:
            status = "infrastructure_error"
            error = error or "replay worktree cleanup failed"

    usage = getattr(result, "usage", {}) if result is not None else {}
    if not isinstance(usage, dict):
        usage = {}
    return ReplayTrialResult(
        status=status,
        final_output=str(getattr(result, "final_content", None) or ""),
        tool_events=tuple(getattr(result, "tool_events", ()) or ()),
        tools_used=tuple(str(item) for item in (getattr(result, "tools_used", ()) or ())),
        usage={str(key): int(value) for key, value in usage.items() if isinstance(value, int)},
        duration_seconds=max(0.0, time.perf_counter() - started),
        changed_paths=changed,
        worktree_path=str(worktree),
        error=error,
        cleanup_error=cleanup_error,
    )


def grade_replay(
    request: ReplayRequest,
    result: ReplayTrialResult,
    *,
    expected_behavior: str | None = None,
    trusted_verification_passed: bool = False,
    original_failure_repaired: bool = False,
    allowed_file_scope: Sequence[str] = (),
) -> ReplayGrade:
    """Grade a trial using trusted facts; Skill text cannot alter this logic."""

    grader_kind = {
        "repo-analysis": "analysis_fact",
        "feature-delivery": "delivery_verification",
        "bug-fix": "bug_fix_verification",
    }.get(request.profile_name)
    if grader_kind is None:
        raise SkillReplayError("unsupported replay profile")
    if result.status != "passed":
        return ReplayGrade(grader_kind, False, f"replay status is {result.status}", result.changed_paths)

    skill_path = request.skill_relative_path.replace("\\", "/")
    changed_without_skill = tuple(
        item for item in result.changed_paths if item != skill_path
    )
    if grader_kind == "analysis_fact":
        passed = (
            bool(expected_behavior)
            and expected_behavior in result.final_output
            and not changed_without_skill
            and not result.tools_used
        )
        return ReplayGrade(
            grader_kind,
            passed,
            "expected fact present with no tool execution" if passed else "analysis assertion failed",
            changed_without_skill,
        )

    allowed = {
        str(item).replace("\\", "/").strip("/")
        for item in allowed_file_scope
    }
    outside_scope = tuple(item for item in changed_without_skill if item not in allowed)
    if outside_scope:
        return ReplayGrade(grader_kind, False, "replay changed files outside the allowed scope", outside_scope)
    if not trusted_verification_passed:
        return ReplayGrade(grader_kind, False, "trusted server verification did not pass", outside_scope)
    if grader_kind == "bug_fix_verification" and not original_failure_repaired:
        return ReplayGrade(grader_kind, False, "original failure was not repaired", outside_scope)
    return ReplayGrade(grader_kind, True, "trusted verification and scope checks passed", outside_scope)


def skill_content_hash(content: str) -> str:
    """Hash the exact UTF-8 Skill bytes used by a replay."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()
