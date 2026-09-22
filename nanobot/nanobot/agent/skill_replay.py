"""Run one Skill-improvement trial in an isolated AgentRunner worktree.

The replay module deliberately keeps the worktree provider at a small seam.
The DevPilot control plane owns trusted Git snapshot creation; nanobot owns the
actual AgentRunner execution and profile tool restrictions.  This prevents a
replay from quietly falling back to the user's live project.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from nanobot.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse, ProviderConversationState
from nanobot.utils.llm_runtime import LLMRuntime

REPLAY_VERSIONS = frozenset({"baseline", "candidate"})
REPLAY_STATUSES = frozenset(
    {"passed", "failed", "timeout", "cancelled", "infrastructure_error"}
)
_PROTECTED_SKILL_PREFIX = "skills/"
_PROFILE_SKILLS = {
    "repo-analysis": "repo-analysis",
    "feature-delivery": "feature-delivery",
    "bug-fix": "bug-fix",
}
_ANALYSIS_READ_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "grep",
        "search_files",
        "devpilot_inspect_project",
        "devpilot_read_code",
        "devpilot_search_code",
    }
)


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
    llm_timeout_s: float | None = 300.0
    provider_state: ProviderConversationState | None = None
    private_worktree_root: Path | None = None
    source_repository_root: Path | None = None
    snapshot_commit: str | None = None
    max_tokens: int = 8_000


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
        if total is not None:
            return max(0, total)
        return max(
            0,
            sum(
                value
                for key, value in self.usage.items()
                if key in {"prompt_tokens", "completion_tokens"}
            ),
        )


@dataclass(frozen=True, slots=True)
class ReplayGrade:
    """A deterministic, server-owned assertion over a replay result."""

    grader_kind: str
    passed: bool
    reason: str
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    """The two isolated executions that make up one trusted A/B comparison."""

    baseline: ReplayTrialResult
    candidate: ReplayTrialResult


class _ReplayBudgetProvider(LLMProvider):
    """Provider proxy that stops a multi-turn replay at its hard budget."""

    def __init__(self, delegate: LLMProvider, *, max_tokens: int, timeout_s: float) -> None:
        super().__init__(api_key=delegate.api_key, api_base=delegate.api_base)
        self._delegate = delegate
        self._remaining_tokens = max_tokens
        self._deadline = time.perf_counter() + timeout_s
        self.supports_progress_deltas = delegate.supports_progress_deltas

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        remaining_seconds = self._deadline - time.perf_counter()
        if remaining_seconds <= 0:
            return LLMResponse(
                content="replay time budget exhausted",
                finish_reason="error",
                error_kind="timeout",
            )
        if self._remaining_tokens <= 0:
            return LLMResponse(
                content="replay token budget exhausted",
                finish_reason="error",
                error_kind="infrastructure",
            )
        response = await self._delegate.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=min(max_tokens, self._remaining_tokens),
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )
        if not response.usage:
            return LLMResponse(
                content="replay provider usage unavailable",
                finish_reason="error",
                error_kind="infrastructure",
            )
        usage = dict(response.usage)
        total = usage.get("total_tokens") or (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        )
        if total <= 0:
            return LLMResponse(
                content="replay provider usage unavailable",
                finish_reason="error",
                error_kind="infrastructure",
                usage=usage,
            )
        self._remaining_tokens -= total
        if self._remaining_tokens < 0:
            return LLMResponse(
                content="replay token budget exhausted",
                finish_reason="error",
                error_kind="infrastructure",
                usage=usage,
            )
        return response

    def get_default_model(self) -> str:
        return self._delegate.get_default_model()


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
    if _PROFILE_SKILLS.get(request.profile_name) != request.skill_id:
        raise SkillReplayError("replay profile and Skill do not match")
    if request.skill_relative_path != f"skills/{request.skill_id}/SKILL.md":
        raise SkillReplayError("replay Skill path does not match its Skill")
    if not request.snapshot_ref:
        raise SkillReplayError("replay snapshot reference is required")
    if request.private_worktree_root is None or request.source_repository_root is None:
        raise SkillReplayError("trusted replay roots are required")
    if not request.snapshot_commit or not re.fullmatch(r"[0-9a-fA-F]{40}", request.snapshot_commit):
        raise SkillReplayError("replay snapshot commit is required")
    if request.max_iterations <= 0 or request.max_iterations > 32:
        raise SkillReplayError("replay iteration budget must be between 1 and 32")
    if request.max_tool_result_chars <= 0:
        raise SkillReplayError("replay tool-result budget must be positive")
    if request.max_tokens <= 0 or request.max_tokens > 8_000:
        raise SkillReplayError("replay token budget must be between 1 and 8000")
    if request.llm_timeout_s is not None and (
        request.llm_timeout_s <= 0 or request.llm_timeout_s > 300
    ):
        raise SkillReplayError("replay timeout must be between 1 and 300 seconds")
    if not request.allowed_tools.issubset(set(request.tools.tool_names)):
        raise SkillReplayError("replay allowed tools are not registered")


def _validate_worktree(request: ReplayRequest, worktree: Path) -> Path:
    """Enforce that the factory returned a fresh Git worktree in private storage."""

    if request.private_worktree_root is None or request.source_repository_root is None:
        raise SkillReplayError("trusted replay roots are required")
    snapshot_commit = request.snapshot_commit
    if snapshot_commit is None:
        raise SkillReplayError("replay snapshot commit is required")
    private_root = request.private_worktree_root.expanduser().resolve()
    source_root = request.source_repository_root.expanduser().resolve()
    worktree = worktree.expanduser().resolve()
    if private_root == source_root or source_root in private_root.parents:
        raise SkillReplayError("private replay root is inside the source repository")
    try:
        worktree.relative_to(private_root)
    except ValueError as exc:
        raise SkillReplayError("replay worktree is outside the trusted private root") from exc
    if worktree == private_root or worktree.name.startswith("."):
        raise SkillReplayError("replay worktree path is not private")
    if source_root == worktree or source_root in worktree.parents:
        raise SkillReplayError("replay worktree is the source repository")
    if any(item.is_symlink() for item in (*private_root.parents, private_root, *worktree.parents)):
        raise SkillReplayError("replay worktree path cannot use symlinks")
    try:
        root_result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        commit_result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        clean_result = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillReplayError("replay worktree validation failed") from exc
    if (
        root_result.returncode != 0
        or Path(root_result.stdout.strip()).resolve() != worktree
        or commit_result.returncode != 0
        or commit_result.stdout.strip().lower() != snapshot_commit.lower()
        or clean_result.returncode != 0
        or clean_result.stdout.strip()
    ):
        raise SkillReplayError("replay worktree is not a clean captured snapshot")
    return worktree


def _skill_message(messages: Sequence[Mapping[str, Any]], skill_content: str) -> list[dict[str, Any]]:
    """Add a trusted Skill snapshot without mutating the caller's messages."""

    if not skill_content.strip():
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


def _replay_tool_events(result: AgentRunResult, limit: int) -> tuple[Mapping[str, str], ...]:
    """Persist bounded tool outputs beside events for trusted evidence grading."""

    events: list[Mapping[str, str]] = []
    for index, raw_event in enumerate(result.tool_events):
        event = dict(raw_event)
        if index < len(result.tool_outputs):
            tool_name, output = result.tool_outputs[index]
            if tool_name == event.get("name"):
                event["output"] = str(output)[:limit]
        events.append(event)
    return tuple(events)


def _status_from_result(result: AgentRunResult) -> tuple[str, str | None]:
    if result.error:
        return "infrastructure_error", result.error
    stop_reason = result.stop_reason
    if stop_reason in {"cancelled", "canceled"}:
        return "cancelled", None
    if stop_reason in {"timeout", "timed_out"}:
        return "timeout", None
    if stop_reason in {"error", "provider_error", "remote_driver_unavailable"}:
        return "infrastructure_error", stop_reason
    if stop_reason in {"max_iterations", "driver_paused", "driver_finished"}:
        return "failed", stop_reason
    if stop_reason == "completed":
        return "passed", None
    return "infrastructure_error", f"unknown stop reason: {stop_reason}"


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
    *,
    lease: ReplayWorktreeLease | None = None,
) -> ReplayTrialResult:
    """Run one Skill with the shared AgentRunner in a fresh private worktree."""

    _validate_request(request)
    lease = lease or request.worktree_factory()
    worktree = lease.path.expanduser().resolve()
    started = time.perf_counter()
    result: AgentRunResult | None = None
    error: str | None = None
    cleanup_error: str | None = None
    status = "infrastructure_error"
    changed: tuple[str, ...] = ()
    try:
        worktree = _validate_worktree(request, worktree)
        skill_path = worktree / request.skill_relative_path
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(skill_content, encoding="utf-8")
        replay_runtime = request.runtime.with_generation_overrides(
            max_tokens=request.max_tokens,
        )
        replay_runtime = replace(
            replay_runtime,
            provider=_ReplayBudgetProvider(
                request.runtime.provider,
                max_tokens=request.max_tokens,
                timeout_s=request.llm_timeout_s or 300,
            ),
        )
        context = RequestContext(
            channel="skill-replay",
            chat_id=request.session_key,
            session_key=request.session_key,
            runtime=replay_runtime,
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
                    runtime=replay_runtime,
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

    usage = result.usage if result is not None else {}
    return ReplayTrialResult(
        status=status,
        final_output=(result.final_content or "") if result is not None else "",
        tool_events=(
            _replay_tool_events(result, request.max_tool_result_chars)
            if result is not None
            else ()
        ),
        tools_used=tuple(result.tools_used) if result is not None else (),
        usage=dict(usage),
        duration_seconds=max(0.0, time.perf_counter() - started),
        changed_paths=changed,
        worktree_path=str(worktree),
        error=error,
        cleanup_error=cleanup_error,
    )


async def run_replay_comparison(
    baseline_request: ReplayRequest,
    candidate_request: ReplayRequest,
    *,
    baseline_skill: str,
    candidate_skill: str,
) -> ReplayComparison:
    """Run baseline and candidate from one snapshot in separate worktrees."""

    _validate_request(baseline_request)
    _validate_request(candidate_request)
    if baseline_request.version != "baseline" or candidate_request.version != "candidate":
        raise SkillReplayError("replay comparison requires baseline and candidate requests")
    if any(
        left != right
        for left, right in (
            (baseline_request.profile_name, candidate_request.profile_name),
            (baseline_request.skill_id, candidate_request.skill_id),
            (baseline_request.snapshot_ref, candidate_request.snapshot_ref),
            (baseline_request.snapshot_commit, candidate_request.snapshot_commit),
            (baseline_request.source_repository_root, candidate_request.source_repository_root),
            (baseline_request.private_worktree_root, candidate_request.private_worktree_root),
        )
    ):
        raise SkillReplayError("baseline and candidate are not bound to one snapshot")
    if baseline_request.session_key == candidate_request.session_key:
        raise SkillReplayError("baseline and candidate must use different sessions")
    if baseline_request.provider_state is not None or candidate_request.provider_state is not None:
        raise SkillReplayError("replay cannot reuse provider conversation state")
    if baseline_request.runtime.provider is candidate_request.runtime.provider:
        raise SkillReplayError("baseline and candidate must use different providers")
    baseline_lease = baseline_request.worktree_factory()
    try:
        candidate_lease = candidate_request.worktree_factory()
    except Exception:
        await _cleanup(baseline_lease)
        raise
    try:
        baseline_path = _validate_worktree(baseline_request, baseline_lease.path)
        candidate_path = _validate_worktree(candidate_request, candidate_lease.path)
    except BaseException:
        await _cleanup(baseline_lease)
        await _cleanup(candidate_lease)
        raise
    if baseline_path == candidate_path:
        await _cleanup(baseline_lease)
        await _cleanup(candidate_lease)
        raise SkillReplayError("baseline and candidate reused one worktree")
    try:
        baseline = await run_skill_replay(
            baseline_request,
            baseline_skill,
            lease=baseline_lease,
        )
    except BaseException:
        await _cleanup(candidate_lease)
        raise
    if (
        not baseline.usage
        or baseline.token_count > baseline_request.max_tokens
        or baseline.duration_seconds > 300
    ):
        await _cleanup(candidate_lease)
        return ReplayComparison(
            baseline=baseline,
            candidate=ReplayTrialResult(
                status="infrastructure_error",
                final_output="",
                tool_events=(),
                tools_used=(),
                usage={},
                duration_seconds=0.0,
                changed_paths=(),
                worktree_path=str(candidate_path),
                error="replay baseline exceeded its hard budget or had unknown usage",
            ),
        )
    remaining_seconds = 600 - baseline.duration_seconds
    if remaining_seconds <= 0:
        await _cleanup(candidate_lease)
        return ReplayComparison(
            baseline=baseline,
            candidate=ReplayTrialResult(
                status="infrastructure_error",
                final_output="",
                tool_events=(),
                tools_used=(),
                usage={},
                duration_seconds=0.0,
                changed_paths=(),
                worktree_path=str(candidate_path),
                error="replay comparison time budget was exhausted before candidate",
            ),
        )
    candidate_request = replace(
        candidate_request,
        llm_timeout_s=min(
            candidate_request.llm_timeout_s or 300,
            remaining_seconds,
        ),
    )
    candidate = await run_skill_replay(
        candidate_request,
        candidate_skill,
        lease=candidate_lease,
    )
    total_tokens = baseline.token_count + candidate.token_count
    total_seconds = baseline.duration_seconds + candidate.duration_seconds
    if (
        not candidate.usage
        or candidate.token_count > candidate_request.max_tokens
        or candidate.duration_seconds > 300
        or total_tokens > 20_000
        or total_seconds > 600
    ):
        candidate = replace(
            candidate,
            status="infrastructure_error",
            error=(
                "replay comparison budget was exhausted or usage was unknown"
            ),
        )
    if baseline.worktree_path == candidate.worktree_path:
        raise SkillReplayError("baseline and candidate reused one worktree")
    return ReplayComparison(baseline=baseline, candidate=candidate)


def grade_replay(
    request: ReplayRequest,
    result: ReplayTrialResult,
    *,
    expected_behavior: str | None = None,
    trusted_verification_passed: bool = False,
    original_failure_repaired: bool = False,
    allowed_file_scope: Sequence[str] = (),
    expected_paths: Sequence[str] = (),
    expected_evidence_refs: Sequence[str] = (),
) -> ReplayGrade:
    """Grade a trial using trusted facts; Skill text cannot alter this logic."""

    grader_kind = {
        "repo-analysis": "analysis_fact",
        "feature-delivery": "delivery_verification",
        "bug-fix": "bug_fix_verification",
    }.get(request.profile_name)
    if grader_kind is None:
        raise SkillReplayError("unsupported replay profile")
    if _PROFILE_SKILLS.get(request.profile_name) != request.skill_id:
        raise SkillReplayError("replay profile and Skill do not match")
    if result.status != "passed":
        return ReplayGrade(grader_kind, False, f"replay status is {result.status}", result.changed_paths)

    skill_path = request.skill_relative_path.replace("\\", "/")
    changed_without_skill = tuple(
        item for item in result.changed_paths if item != skill_path
    )
    if grader_kind == "analysis_fact":
        event_texts = [
            json.dumps(event, ensure_ascii=False, sort_keys=True)
            for event in result.tool_events
            if event.get("status") == "ok"
            and event.get("name") in _ANALYSIS_READ_TOOLS
        ]
        expected_paths_ok = all(
            any(str(path).replace("\\", "/") in text for text in event_texts)
            for path in expected_paths
        )
        expected_evidence_ok = all(
            any(str(reference) in text for text in event_texts)
            for reference in expected_evidence_refs
        )
        passed = (
            bool(expected_behavior)
            and expected_behavior in result.final_output
            and expected_paths_ok
            and expected_evidence_ok
            and not changed_without_skill
            and all(tool in _ANALYSIS_READ_TOOLS for tool in result.tools_used)
            and all(
                event.get("status") == "ok"
                and event.get("name") in _ANALYSIS_READ_TOOLS
                for event in result.tool_events
            )
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
