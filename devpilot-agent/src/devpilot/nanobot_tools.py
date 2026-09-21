"""nanobot entry-point adapters for DevPilot analysis and chat-confirmed execution."""

# Optional nanobot imports intentionally precede DevPilot's local imports.
# ruff: noqa: E402

# The package remains importable in DevPilot's own test environment.  When loaded
# by nanobot, the real Tool base class and schema decorator are used.
# pyright: reportMissingImports=false, reportMissingTypeStubs=false, reportAssignmentType=false
# pyright: reportUnknownVariableType=false, reportUnknownParameterType=false
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUntypedClassDecorator=false, reportUntypedBaseClass=false, reportGeneralTypeIssues=false

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

_CURRENT_TURN_PROPOSAL: ContextVar[dict[str, str] | None] = ContextVar(
    "devpilot_current_turn_proposal",
    default=None,
)
_TURN_PROPOSALS: dict[str, dict[str, str]] = {}
_TURN_PROPOSALS_LOCK = Lock()
_transport_logger = logging.getLogger(__name__)

try:
    from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
except ImportError:  # pragma: no cover - exercised only outside nanobot

    class Tool:
        pass

    class ToolResult(str):
        is_error: bool

        def __new__(cls, content: str, *, is_error: bool = False) -> ToolResult:
            result = str.__new__(cls, content)
            result.is_error = is_error
            return result

        @classmethod
        def error(cls, content: str) -> ToolResult:
            return cls(content, is_error=True)

    def tool_parameters(schema: dict[str, Any]) -> Callable[[type[Any]], type[Any]]:
        return lambda cls: cls


if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext

from .action_broker import (
    ActionBroker,
    BrokerActionRequest,
    ConfirmationContext,
    ConfirmationReservation,
)
from .code_access import CodeAccessError, CodeAccessService
from .event_bridge import project_run_state, project_sse_events
from .inspector import MAX_FILE_BYTES, MAX_FILES, MAX_METADATA_BYTES
from .paths import AllowedRoots, PathBoundaryError
from .services import ProjectAnalysisService
from .skill_improvement import SkillImprovementError, SkillImprovementStore, task_snapshot_ref
from .storage import ProjectStore
from .user_intent import capability_confirmation_matches, confirmation_matches

_CONTROL_PLANE_ERROR_CODES = frozenset(
    {
        "APPROVAL_ALREADY_ISSUED",
        "APPROVAL_INVALID",
        "APPROVAL_SCOPE_MISMATCH",
        "ARTIFACT_ROOT_INVALID",
        "AUTONOMOUS_REPAIR_BLOCKED",
        "CAPABILITY_INVALID",
        "DECISION_INVALID",
        "CHANGESET_NOT_APPLICABLE",
        "CHANGESET_DECISION_FINAL",
        "CHANGESET_LEGACY_READ_ONLY",
        "CHANGESET_NOT_ROLLBACKABLE",
        "CHANGESET_NOT_STOPPABLE",
        "CHANGESET_NOT_FOUND",
        "CHANGESET_NOT_VERIFIABLE",
        "EXECUTION_BACKEND_UNAVAILABLE",
        "EXECUTION_PROJECT_NOT_ALLOWLISTED",
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_IN_PROGRESS",
        "IDEMPOTENCY_REQUIRED",
        "PATH_DENIED",
        "PATH_INVALID",
        "PATH_NOT_RELATIVE",
        "PATH_OUTSIDE_PROJECT",
        "TARGET_NOT_FILE",
        "TARGET_EXISTS",
        "TARGET_MISSING",
        "HASH_INVALID",
        "PRECONDITION_REQUIRED",
        "PRECONDITION_HASH_MISMATCH",
        "PERSISTENCE_REQUIRED",
        "PROJECT_NOT_FOUND",
        "PROJECT_TRUST_CONFIRMATION_REQUIRED",
        "PROJECT_TRUST_INVALID",
        "PROJECT_TRUST_REQUIRED",
        "RUN_NOT_FOUND",
        "VERIFICATION_FAILED",
        "WORKFLOW_ARTIFACT_WRITE_FAILED",
        "WORKFLOW_EVIDENCE_INVALID",
        "WORKFLOW_EVIDENCE_REQUIRED",
        "BUG_FIX_FAILURE_EVIDENCE_INVALID",
        "BUG_FIX_INPUT_INVALID",
        "BUG_FIX_SCOPE_INVALID",
        "PATCH_REPLACEMENT_INVALID",
        "PATCH_REPLACEMENT_NOT_UNIQUE",
        "FILE_READ_FAILED",
        "TEST_REPAIR_BUDGET_EXCEEDED",
        "TEST_REPAIR_FAILURE_EVIDENCE_INVALID",
        "TEST_REPAIR_FAILURE_FINGERPRINT_INVALID",
        "TEST_REPAIR_INPUT_INVALID",
        "TEST_REPAIR_NOT_FOUND",
        "TEST_REPAIR_PERSISTENCE_INVALID",
        "TEST_REPAIR_REPEAT_FAILURE",
        "TEST_REPAIR_SCOPE_INVALID",
        "VERIFICATION_PROFILE_UNSUPPORTED",
        "DELIVERY_REPORT_INVALID",
    }
)

_PATCH_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relative_path": {
            "type": "string",
            "minLength": 1,
            "description": "Project-relative target file path.",
        },
        "operation": {
            "type": "string",
            "enum": ["create", "update", "delete"],
        },
        "content": {
            "type": ["string", "null"],
            "description": "Complete new text; omit when using replacements.",
        },
        "expected_sha256": {"type": ["string", "null"]},
        "replacements": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "description": (
                "Preferred for small updates. Each item replaces one exact text block "
                "the requested count; use update only and do not send content."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "find": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "replace": {"type": "string", "maxLength": 20000},
                    "count": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["find", "replace"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["relative_path", "operation"],
    "additionalProperties": False,
}


def _normalize_patch_hashes(patches: list[Any]) -> list[Any]:
    """Normalize valid SHA-256 preconditions without mutating model input."""
    normalized: list[Any] = []
    for patch in patches:
        if not isinstance(patch, dict):
            normalized.append(patch)
            continue
        copied = dict(patch)
        expected = copied.get("expected_sha256")
        if isinstance(expected, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            copied["expected_sha256"] = expected.lower()
        normalized.append(copied)
    return normalized


def _hydrate_full_access_patch_hashes(
    project_path: str, patches: list[Any]
) -> list[Any]:
    """Fill omitted update/delete preconditions from the current local file.

    Full Access is an explicit, trusted local capability.  The control plane
    still enforces the hash at apply time, but the adapter can obtain the
    current hash without making the model recite a mechanical value.  Invalid
    paths are deliberately left to the control plane so it can return its
    stable path diagnostic.
    """

    normalized = _normalize_patch_hashes(patches)
    try:
        root = Path(project_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return normalized

    hydrated: list[Any] = []
    for patch in normalized:
        if not isinstance(patch, dict):
            hydrated.append(patch)
            continue
        copied = dict(patch)
        if copied.get("operation") not in {"update", "delete"} or copied.get(
            "expected_sha256"
        ):
            hydrated.append(copied)
            continue
        relative_path = copied.get("relative_path")
        if not isinstance(relative_path, str):
            hydrated.append(copied)
            continue
        try:
            target = (root / relative_path).resolve(strict=True)
            target.relative_to(root)
            if target.is_file():
                copied["expected_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        except (OSError, RuntimeError, ValueError):
            pass
        hydrated.append(copied)
    return hydrated

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
    re.compile(r"(?i)deepseek_api_key\s*[=:]\s*\S+"),
)


def _patches_require_privacy_confirmation(patches: list[Any]) -> bool:
    """Detect credential-shaped patch content before Full Access writes it.

    This is deliberately narrow: ordinary source code and logs continue
    automatically.  A hit does not reveal the matching value to the model or
    UI; it only moves this single write to the privacy-confirmation path.
    """

    try:
        content = json.dumps(patches, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)
_PATH_DENIAL_CODES = frozenset(
    {
        "PATH_INVALID",
        "PATH_NOT_RELATIVE",
        "PATH_OUTSIDE_PROJECT",
        "WINDOWS_PATH_ALIAS",
        "TARGET_NOT_FILE",
    }
)

_BROKER_SUCCESS_STATUSES = frozenset(
    {"applied", "verified", "reverted", "apply_failed", "verification_failed"}
)


def _broker_terminal_status(status: str) -> str:
    """Map control-plane success states to the broker's durable terminal state."""
    return "completed" if status in _BROKER_SUCCESS_STATUSES else status


def _trusted_confirmation_attributes(request_context: Any | None = None) -> dict[str, Any] | None:
    """Read the WebUI-only confirmation envelope; it is never model input."""
    try:
        from nanobot.agent.tools.context import current_request_context

        context = current_request_context()
        value = context.attributes.get("devpilot_confirmation") if context is not None else None
        if isinstance(value, dict):
            return value
    except (ImportError, TypeError, ValueError):
        pass
    # Tool calls can run in a child task whose ContextVar snapshot was created
    # before the runner bound the current turn.  The registry already injects
    # the same authoritative RequestContext into ContextAware tools; use that
    # object only as a trusted transport fallback, never from model arguments.
    value = getattr(request_context, "attributes", {}).get("devpilot_confirmation")
    if isinstance(value, dict):
        return value
    # The inbound message bus keeps the gateway-supplied attributes in its own
    # task-local ContextVar before dispatching the turn.  A tool call may run
    # in a child asyncio task whose request-context object was created before
    # that dispatch boundary; recover the same trusted envelope from the bus
    # snapshot as the final trusted transport fallback.
    try:
        from nanobot.bus.events import current_inbound_attributes

        value = current_inbound_attributes().get("devpilot_confirmation")
        return value if isinstance(value, dict) else None
    except (ImportError, TypeError, ValueError):
        return None


def _is_trusted_full_access(request_context: Any | None = None) -> bool:
    """Return whether this tool call belongs to a trusted WebUI Full Access turn.

    The model never supplies this value.  It is read from nanobot's task-local
    workspace scope, with the registry request context as the child-task
    fallback used elsewhere in this adapter.
    """

    try:
        from nanobot.security.workspace_access import current_workspace_scope

        scope = current_workspace_scope()
        if scope is not None:
            return (
                scope.source_channel == "websocket"
                and scope.access_mode == "full"
                and not scope.restrict_to_workspace
            )
    except (ImportError, AttributeError, TypeError, ValueError):
        pass
    scope = getattr(request_context, "attributes", {}).get("workspace_scope")
    if not isinstance(scope, Mapping):
        return False
    return (
        scope.get("source_channel") == "websocket"
        and scope.get("access_mode") == "full"
        and scope.get("restrict_to_workspace") is False
    )


def _skill_improvement_context(
    request_context: Any | None,
    *,
    project_path: str,
    skill_id: str,
    summary: str,
    expected_behavior: str,
    failure_evidence_refs: object = (),
    allowed_file_scope: object = (),
    verification_profile: str | None = None,
) -> dict[str, Any] | None:
    """Build a server-verifiable improvement context for one Full Access turn."""
    if os.environ.get("DEVPILOT_ENABLE_SKILL_IMPROVEMENT") != "1":
        return None
    attributes = getattr(request_context, "attributes", {})
    if not isinstance(attributes, Mapping):
        return None
    task_id = attributes.get("devpilot_task_id")
    project_id = attributes.get("devpilot_project_id")
    profile_binding = attributes.get("devpilot_profile_binding")
    sender_id = getattr(request_context, "sender_id", None)
    session_id = (
        getattr(request_context, "session_key", None)
        or getattr(request_context, "chat_id", None)
    )
    workspace = Path(project_path).expanduser().resolve()
    if not all(
        isinstance(value, str) and value
        for value in (task_id, project_id, profile_binding, sender_id, session_id)
    ):
        return None
    task_id_value = cast(str, task_id)
    project_id_value = cast(str, project_id)
    profile_binding_value = cast(str, profile_binding)
    sender_id_value = cast(str, sender_id)
    session_id_value = cast(str, session_id)
    raw_failure_refs = (
        failure_evidence_refs
        if isinstance(failure_evidence_refs, (list, tuple))
        else ()
    )
    source_refs = tuple(
        item for item in raw_failure_refs if isinstance(item, str) and item.strip()
    )
    try:
        store = SkillImprovementStore.from_environment(workspace)
        snapshot = store.capture_task_snapshot(
            task_id=task_id_value,
            workspace=workspace,
            session_id=session_id_value,
            project_id=project_id_value,
            skill_id=skill_id,
            allowed_file_scope=(
                tuple(item for item in allowed_file_scope if isinstance(item, str))
                if isinstance(allowed_file_scope, (list, tuple))
                else ()
            ),
            verification_profile=verification_profile or "pending",
            source_evidence_refs=source_refs,
        )
    except (OSError, ValueError, SkillImprovementError):
        return None
    trusted_failure_refs = [
        item if item.startswith("failure-") else f"failure-{item}"
        for item in raw_failure_refs
        if isinstance(item, str) and item.strip()
    ]
    return {
        "skill_id": skill_id,
        "profile_binding": profile_binding_value,
        "task_id": task_id_value,
        "user_id": sender_id_value,
        "session_id": session_id_value,
        "skill_hash": snapshot.content_hash,
        "replay_snapshot_ref": task_snapshot_ref(task_id_value),
        "summary": summary,
        "expected_behavior": expected_behavior,
        "verification_kind": "semantic",
        "allowed_file_scope": list(snapshot.allowed_file_scope),
        "verification_profile": snapshot.verification_profile,
        "replayable": snapshot.replayable,
        "replayability_reason": snapshot.replayability_reason,
        "source_evidence_refs": list(snapshot.source_evidence_refs),
        "failure_evidence_refs": trusted_failure_refs,
    }


def _record_current_turn_proposal(
    result: Mapping[str, Any],
    *,
    request_context: Any | None = None,
) -> None:
    """Expose a safe proposal reference to the WebSocket delivery seam only."""
    values = {
        key: result.get(key)
        for key in ("change_set_id", "plan_hash", "patch_hash", "scope_hash", "project_id")
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        _transport_logger.info(
            "DevPilot proposal capture: valid=False context=%s values=%s",
            type(request_context).__name__ if request_context is not None else None,
            sorted(key for key, value in values.items() if isinstance(value, str) and value),
        )
        return
    proposal = cast(dict[str, str], values)
    context = request_context
    if context is None:
        try:
            from nanobot.agent.tools.context import current_request_context

            context = current_request_context()
        except (ImportError, TypeError, ValueError):
            context = None
    if context is not None:
        try:
            context.attributes["devpilot_proposal"] = dict(proposal)
            chat_id = getattr(context, "chat_id", None)
            if isinstance(chat_id, str) and chat_id:
                with _TURN_PROPOSALS_LOCK:
                    _TURN_PROPOSALS[chat_id] = dict(proposal)
                _transport_logger.info(
                    "DevPilot proposal capture: chat_id=%s stored=True",
                    chat_id,
                )
        except (AttributeError, TypeError, ValueError):
            pass
    _CURRENT_TURN_PROPOSAL.set(dict(proposal))
    try:
        from nanobot.agent.tools.context import current_request_context

        current = current_request_context()
        if current is not None:
            current.attributes["devpilot_proposal"] = dict(proposal)
    except (ImportError, TypeError, ValueError):
        return


def take_current_turn_proposal(chat_id: str | None = None) -> dict[str, str] | None:
    """Consume the proposal created in this task when the request context was replaced."""
    if isinstance(chat_id, str) and chat_id:
        with _TURN_PROPOSALS_LOCK:
            proposal = _TURN_PROPOSALS.pop(chat_id, None)
        _transport_logger.info(
            "DevPilot proposal consume: chat_id=%s found=%s",
            chat_id,
            proposal is not None,
        )
        if proposal is not None:
            return dict(proposal)
    proposal = _CURRENT_TURN_PROPOSAL.get()
    _CURRENT_TURN_PROPOSAL.set(None)
    return dict(proposal) if proposal is not None else None


def _configured_roots(workspace: Path) -> AllowedRoots:
    raw = os.environ.get("DEVPILOT_ALLOWED_ROOTS", "")
    values: list[str | Path] = [item for item in raw.split(os.pathsep) if item]
    return AllowedRoots.from_paths(values or [workspace])


def _data_dir() -> Path:
    return Path(
        os.environ.get(
            "DEVPILOT_DATA_DIR",
            str(Path.home() / ".nanobot-skill-improvement"),
        )
    ).expanduser().resolve()


def _skill_improvement_enabled() -> bool:
    """Return whether the opt-in P8 Skill trial is enabled for this process."""
    return os.environ.get("DEVPILOT_ENABLE_SKILL_IMPROVEMENT") == "1"


def _durable_store() -> ProjectStore:
    """Open only an Alembic-initialized store; never create production tables implicitly."""
    store = ProjectStore(_data_dir() / "devpilot.db")
    with store.engine.connect() as connection:
        has_version = connection.exec_driver_sql(
            "select name from sqlite_master where type='table' and name='alembic_version'"
        ).scalar_one_or_none()
    if not has_version:
        raise RuntimeError("database schema is not initialized by Alembic")
    store.require_schema_head("0011_p8_skill_improvement_records")
    return store


def _read_only_code_access_store() -> ProjectStore:
    """Open a readable legacy store without creating or mutating its schema.

    Older local nanobot installations predate Alembic but still contain the
    project registry needed by the read-only code tools.  Action execution
    never uses this compatibility path; it remains gated by ``_durable_store``.
    """
    store = ProjectStore(_data_dir() / "devpilot.db")
    with store.engine.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.exec_driver_sql(
                "select name from sqlite_master where type='table'"
            ).all()
        }
        has_version = "alembic_version" in tables
    if has_version:
        store.require_schema_head("0011_p8_skill_improvement_records")
    elif "projects" not in tables:
        raise RuntimeError("database schema is not initialized")
    return store


def _service(workspace: Path) -> ProjectAnalysisService:
    store = _durable_store()
    return ProjectAnalysisService(allowed_roots=_configured_roots(workspace), store=store)


def _code_access_service(workspace: Path) -> CodeAccessService:
    store = _read_only_code_access_store()
    return CodeAccessService(allowed_roots=_configured_roots(workspace), store=store)


def _public_code(exc: Exception) -> str:
    if isinstance(exc, CodeAccessError):
        return "PATH_DENIED" if exc.code in _PATH_DENIAL_CODES else exc.code
    return type(exc).__name__


def _project_path(value: str, workspace: Path) -> str:
    candidate = Path(value).expanduser()
    return str((workspace / candidate).resolve() if not candidate.is_absolute() else candidate)


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "minLength": 1},
            "test_kind": {"type": "string", "enum": ["python-pytest", "node-test"]},
            "target_files": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1},
            },
            "symptom": {"type": "string", "minLength": 1, "maxLength": 4000},
            "expected_behavior": {"type": "string", "minLength": 1, "maxLength": 4000},
        },
        "required": [
            "project_path",
            "test_kind",
            "target_files",
            "symptom",
            "expected_behavior",
        ],
        "additionalProperties": False,
    }
)
class ProjectTestRunTool(Tool):
    """Run a project test in the configured sandbox and preserve the real receipt."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_test_run"

    @property
    def description(self) -> str:
        return (
            "Run a needed project test in the configured sandbox. The server snapshots files, "
            "records the actual receipt, and only a real non-zero exit creates failure evidence. "
            "Use after inspecting the project; this never changes project files."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            from nanobot.agent.tools.context import current_request_context

            request_context = current_request_context()
            attributes = getattr(request_context, "attributes", {})
            task_id = (
                attributes.get("devpilot_task_id")
                if isinstance(attributes, Mapping)
                else None
            )
            session_id = (
                (getattr(request_context, "session_key", None)
                or getattr(request_context, "chat_id", None))
                if request_context is not None
                else None
            )
            user_id = getattr(request_context, "sender_id", None)
            result = await asyncio.to_thread(
                _post_project_test,
                {
                    "project_path": str(kwargs["project_path"]),
                    "test_kind": str(kwargs["test_kind"]),
                    "target_files": kwargs["target_files"],
                    "symptom": str(kwargs["symptom"]),
                    "expected_behavior": str(kwargs["expected_behavior"]),
                    **(
                        {
                            "task_id": task_id,
                            "session_id": session_id,
                            "user_id": user_id,
                        }
                        if all(
                            isinstance(value, str) and value
                            for value in (task_id, session_id, user_id)
                        )
                        else {}
                    ),
                },
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"project test rejected: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"project test rejected: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_path": {
                "type": "string",
                "description": (
                    "Absolute project directory, or a path relative to the nanobot workspace."
                ),
                "minLength": 1,
            }
        },
        "required": ["project_path"],
        "additionalProperties": False,
    }
)
class InspectProjectTool(Tool):
    """Expose bounded project facts without executing repository code."""

    _scopes = {"core"}

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    @classmethod
    def create(cls, ctx: ToolContext) -> InspectProjectTool:
        return cls(Path(ctx.workspace).expanduser().resolve())

    @property
    def name(self) -> str:
        return "devpilot_inspect_project"

    @property
    def description(self) -> str:
        return (
            "Read-only bounded repository inspection. Identifies Python/Node stacks, "
            "declared entry points, tests, dependencies, configuration and unknowns; "
            "never installs, executes or writes repository files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": (
                        "Absolute project directory, or a path relative to the nanobot workspace."
                    ),
                    "minLength": 1,
                }
            },
            "required": ["project_path"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            project_path = _project_path(str(kwargs["project_path"]), self.workspace)
            from nanobot.agent.tools.context import current_request_context

            request_context = current_request_context()
            attributes = getattr(request_context, "attributes", {})
            task_id = (
                attributes.get("devpilot_task_id")
                if isinstance(attributes, Mapping)
                else None
            )
            session_id = (
                (getattr(request_context, "session_key", None)
                or getattr(request_context, "chat_id", None))
                if request_context is not None
                else None
            )
            user_id = getattr(request_context, "sender_id", None)
            # Repository discovery and its SQLite writes are synchronous.  Keep
            # them off nanobot's single event loop so a slow filesystem or
            # database lock cannot freeze WebUI health and unrelated chats.
            service = _service(self.workspace)
            context_kwargs = {
                key: value
                for key, value in {
                    "task_id": task_id,
                    "session_id": session_id,
                    "user_id": user_id,
                }.items()
                if isinstance(value, str) and value
            }
            if context_kwargs:
                outcome = await asyncio.to_thread(
                    service.inspect, project_path, **context_kwargs
                )
            else:
                outcome = await asyncio.to_thread(service.inspect, project_path)
            try:
                from nanobot.agent.tools.context import current_request_context

                request_context = current_request_context()
                if request_context is not None:
                    # A repo-analysis run remains open while bounded search/read
                    # facts are collected.  Remember the server-issued ID for
                    # the current turn so a weaker model need not reproduce it
                    # exactly in every following tool argument.
                    request_context.attributes["devpilot_repo_analysis"] = {
                        "project_id": outcome.project_id,
                        "run_id": outcome.run_id,
                    }
            except ImportError:
                pass
            payload = {
                "project_id": outcome.project_id,
                "run_id": outcome.run_id,
                "facts": outcome.inspection.to_dict(),
                "next_step": (
                    "使用这些事实生成 project-overview.md，再调用 devpilot_publish_artifact。"
                ),
                "limits": {
                    "max_file_bytes": MAX_FILE_BYTES,
                    "max_files": MAX_FILES,
                    "max_metadata_bytes": MAX_METADATA_BYTES,
                },
            }
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (KeyError, TypeError, ValueError, OSError, PathBoundaryError) as exc:
            return ToolResult.error(f"P1 project inspection rejected: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "minLength": 1},
            "run_id": {"type": "string", "minLength": 1},
            "content": {"type": "string", "minLength": 1, "maxLength": 1_048_576},
            "name": {"type": "string", "enum": ["project-overview.md"]},
        },
        "required": ["project_id", "run_id", "content", "name"],
        "additionalProperties": False,
    }
)
class PublishArtifactTool(Tool):
    """Write a server-owned Markdown Artifact and close its Run."""

    _scopes = {"core"}

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    @classmethod
    def create(cls, ctx: ToolContext) -> PublishArtifactTool:
        return cls(Path(ctx.workspace).expanduser().resolve())

    @property
    def name(self) -> str:
        return "devpilot_publish_artifact"

    @property
    def description(self) -> str:
        return (
            "Write one bounded project-overview.md Artifact under the server-owned data directory."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "minLength": 1},
                "run_id": {"type": "string", "minLength": 1},
                "content": {"type": "string", "minLength": 1, "maxLength": 1_048_576},
                "name": {"type": "string", "enum": ["project-overview.md"]},
            },
            "required": ["project_id", "run_id", "content", "name"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        content = str(kwargs.get("content", ""))
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            return ToolResult.error("P1 Artifact rejected: possible secret detected")
        if kwargs.get("name") != "project-overview.md":
            return ToolResult.error("P1 Artifact rejected: unsupported name")
        try:
            store = _durable_store()
            write = ProjectAnalysisService(
                allowed_roots=_configured_roots(self.workspace), store=store
            ).publish_overview(
                project_id=str(kwargs["project_id"]),
                run_id=str(kwargs["run_id"]),
                content=content,
                artifacts_root=_data_dir(),
            )
            return json.dumps(
                {"path": str(write.path), "sha256": write.sha256, "size_bytes": write.size_bytes},
                ensure_ascii=False,
                sort_keys=True,
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return ToolResult.error(f"P1 Artifact rejected: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "minLength": 1},
            "query": {"type": "string", "minLength": 1, "maxLength": 256},
            "glob": {"type": "string", "maxLength": 256},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "case_sensitive": {"type": "boolean"},
            "evidence_run_id": {
                "type": "string",
                "minLength": 1,
                "description": "Open repo-analysis Run ID used to persist provenance.",
            },
        },
        "required": ["project_id", "query"],
        "additionalProperties": False,
    }
)
class SearchCodeTool(Tool):
    """Search registered project source using bounded literal matching."""

    _scopes = {"core"}

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    @classmethod
    def create(cls, ctx: ToolContext) -> SearchCodeTool:
        return cls(Path(ctx.workspace).expanduser().resolve())

    @property
    def name(self) -> str:
        return "devpilot_search_code"

    @property
    def description(self) -> str:
        return (
            "Read-only literal search within a previously inspected project. "
            "It skips secrets, generated directories, binary files, and symbolic links."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return getattr(type(self), "__tool_parameters__", {}) or {
            "type": "object",
            "required": ["project_id", "query"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            evidence_run_id = kwargs.get("evidence_run_id")
            if evidence_run_id is None:
                try:
                    from nanobot.agent.tools.context import current_request_context

                    request_context = current_request_context()
                    analysis = (
                        request_context.attributes.get("devpilot_repo_analysis")
                        if request_context is not None
                        else None
                    )
                    if (
                        isinstance(analysis, Mapping)
                        and analysis.get("project_id") == str(kwargs["project_id"])
                        and isinstance(analysis.get("run_id"), str)
                    ):
                        evidence_run_id = analysis["run_id"]
                except ImportError:
                    pass
            result = _code_access_service(self.workspace).search(
                str(kwargs["project_id"]),
                str(kwargs["query"]),
                glob=str(kwargs["glob"]) if kwargs.get("glob") is not None else None,
                limit=int(kwargs.get("limit", 50)),
                case_sensitive=bool(kwargs.get("case_sensitive", False)),
                evidence_run_id=str(evidence_run_id) if evidence_run_id is not None else None,
            )
            return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
        except (KeyError, TypeError, ValueError, OSError, CodeAccessError) as exc:
            return ToolResult.error(f"P2A code search rejected: {_public_code(exc)}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "minLength": 1},
            "path": {"type": "string", "minLength": 1, "maxLength": 512},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "evidence_run_id": {
                "type": "string",
                "minLength": 1,
                "description": "Open repo-analysis Run ID used to persist provenance.",
            },
        },
        "required": ["project_id", "path"],
        "additionalProperties": False,
    }
)
class ReadCodeTool(Tool):
    """Read a bounded line range from registered project source."""

    _scopes = {"core"}

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    @classmethod
    def create(cls, ctx: ToolContext) -> ReadCodeTool:
        return cls(Path(ctx.workspace).expanduser().resolve())

    @property
    def name(self) -> str:
        return "devpilot_read_code"

    @property
    def description(self) -> str:
        return (
            "Read-only bounded UTF-8 source access for a previously inspected project. "
            "Returns line metadata and the full-file SHA-256 precondition hash."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return getattr(type(self), "__tool_parameters__", {}) or {
            "type": "object",
            "required": ["project_id", "path"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            evidence_run_id = kwargs.get("evidence_run_id")
            if evidence_run_id is None:
                try:
                    from nanobot.agent.tools.context import current_request_context

                    request_context = current_request_context()
                    analysis = (
                        request_context.attributes.get("devpilot_repo_analysis")
                        if request_context is not None
                        else None
                    )
                    if (
                        isinstance(analysis, Mapping)
                        and analysis.get("project_id") == str(kwargs["project_id"])
                        and isinstance(analysis.get("run_id"), str)
                    ):
                        evidence_run_id = analysis["run_id"]
                except ImportError:
                    pass
            result = _code_access_service(self.workspace).read(
                str(kwargs["project_id"]),
                str(kwargs["path"]),
                start_line=int(kwargs.get("start_line", 1)),
                end_line=(int(kwargs["end_line"]) if kwargs.get("end_line") is not None else None),
                evidence_run_id=str(evidence_run_id) if evidence_run_id is not None else None,
            )
            return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
        except (KeyError, TypeError, ValueError, OSError, CodeAccessError) as exc:
            return ToolResult.error(f"P2A code read rejected: {_public_code(exc)}")


def _control_plane_url() -> str:
    raw = os.environ.get("DEVPILOT_CONTROL_PLANE_URL", "http://127.0.0.1:18800").rstrip("/")
    parsed = urlparse(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path not in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("control plane must be a localhost HTTP URL")
    return raw


def _model_result(result: dict[str, Any]) -> dict[str, Any]:
    """Remove control-plane metadata before a result reaches model context."""
    result.pop("approval_token", None)
    result.pop("request_id", None)
    return result


def _post_preview(payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{_control_plane_url()}/v1/change-sets/preview",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        response_body = response.read(256 * 1024 + 1)
    if len(response_body) > 256 * 1024:
        raise ValueError("control-plane response is too large")
    result = json.loads(response_body.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("control-plane response is invalid")
    return _model_result(result)


def _post_feature_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{_control_plane_url()}/v1/feature-delivery/proposals",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        response_body = response.read(256 * 1024 + 1)
    if len(response_body) > 256 * 1024:
        raise ValueError("control-plane response is too large")
    result = json.loads(response_body.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("control-plane response is invalid")
    result["requires_user_confirmation"] = True
    return _model_result(result)


def _post_bug_fix_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{_control_plane_url()}/v1/bug-fix/proposals",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        response_body = response.read(256 * 1024 + 1)
    if len(response_body) > 256 * 1024:
        raise ValueError("control-plane response is too large")
    result = json.loads(response_body.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("control-plane response is invalid")
    result["requires_user_confirmation"] = True
    return _model_result(result)


def _post_project_test(payload: dict[str, Any]) -> dict[str, Any]:
    canonical_payload = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    idempotency_key = "project-test-" + hashlib.sha256(
        canonical_payload.encode("utf-8")
    ).hexdigest()[:48]
    # The control plane can spend up to the FC project-test limit executing
    # pytest (300 seconds).  Keep a small response/cleanup margin so the
    # nanobot adapter does not report a client-side timeout after FC has
    # already run the test successfully.
    return _post_control_plane(
        "/v1/project-tests",
        payload,
        idempotency_key=idempotency_key,
        timeout_seconds=330,
    )


def _post_test_repair_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{_control_plane_url()}/v1/test-repair/proposals",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        response_body = response.read(256 * 1024 + 1)
    if len(response_body) > 256 * 1024:
        raise ValueError("control-plane response is too large")
    result = json.loads(response_body.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("control-plane response is invalid")
    result["requires_user_confirmation"] = True
    return _model_result(result)


def _post_full_access_apply_verify(
    change_set_id: str,
    verification_profile: str | None,
    skill_improvement_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a server-owned direct action for one trusted Full Access proposal."""

    payload: dict[str, Any] = {"profile": verification_profile or "python-syntax"}
    if skill_improvement_context is not None:
        payload["skill_improvement_context"] = dict(skill_improvement_context)
    return _post_control_plane(
        _change_set_path(change_set_id, "full-access-apply-verify"),
        payload,
        idempotency_key=f"full-access-{change_set_id}",
    )


def _full_access_preview_and_execute(
    *,
    project_path: str,
    patches: list[Any],
    verification_profile: str | None,
    request_context: Any | None,
    project_id: str | None = None,
    skill_id: str | None = None,
    improvement_summary: str = "",
    improvement_expected_behavior: str = "",
    failure_evidence_refs: object = (),
) -> dict[str, Any]:
    """Preview, apply and verify a normal project edit without workflow gates."""

    payload: dict[str, Any] = {
        "project_path": project_path,
        "patches": _hydrate_full_access_patch_hashes(project_path, patches),
    }
    if project_id:
        payload["project_id"] = project_id
    request_attributes = getattr(request_context, "attributes", {})
    task_id = (
        request_attributes.get("devpilot_task_id")
        if isinstance(request_attributes, Mapping)
        else None
    )
    if isinstance(task_id, str) and task_id:
        payload["task_id"] = task_id
    proposal = _post_preview(payload)
    _record_current_turn_proposal(proposal, request_context=request_context)
    if _patches_require_privacy_confirmation(patches):
        proposal.update(
            {
                "requires_user_confirmation": True,
                "privacy_confirmation_required": True,
                "execution_mode": "full_access",
                "next_step": (
                    "本次修改疑似包含密钥或隐私凭据，已保留 Diff 但未写入。"
                    "请明确确认是否允许写入该敏感内容。"
                ),
            }
        )
        return proposal
    improvement_context = None
    if skill_id is not None:
        allowed_file_scope = tuple(
            str(item.get("relative_path"))
            for item in patches
            if isinstance(item, Mapping) and isinstance(item.get("relative_path"), str)
        )
        improvement_context = _skill_improvement_context(
            request_context,
            project_path=project_path,
            skill_id=skill_id,
            summary=improvement_summary or "Full Access verification failed",
            expected_behavior=improvement_expected_behavior
            or f"{verification_profile or 'python-syntax'} verification passes",
            failure_evidence_refs=failure_evidence_refs,
            allowed_file_scope=allowed_file_scope,
            verification_profile=verification_profile or "python-syntax",
        )
    if improvement_context is None:
        execution = _post_full_access_apply_verify(
            str(proposal["change_set_id"]),
            verification_profile,
        )
    else:
        execution = _post_full_access_apply_verify(
            str(proposal["change_set_id"]),
            verification_profile,
            improvement_context,
        )
    proposal.update(
        {
            "requires_user_confirmation": False,
            "execution_mode": "full_access",
            "execution": execution,
            "next_step": "完全访问已直接应用并验证本次修改；请根据执行回执继续处理。",
        }
    )
    return proposal


def _request_approval(change_set_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _post_control_plane(_change_set_path(change_set_id, "approval-requests"), payload)


def _http_error_message(exc: HTTPError) -> str:
    """Expose stable local API diagnostics without leaking response bodies or tokens."""
    try:
        body = exc.read(4 * 1024).decode("utf-8", errors="replace").strip()
    except OSError:
        body = ""
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            # The API message is not trusted: it can contain user code, paths,
            # or a token-shaped value. The stable error code is sufficient for
            # the adapter contract and is safe to expose to nanobot.
            code = str(error["code"])
            if code in _CONTROL_PLANE_ERROR_CODES:
                return f"HTTP {exc.code}: {code}"
            return f"HTTP {exc.code}: CONTROL_PLANE_ERROR"
    except (TypeError, ValueError):
        pass
    return f"HTTP {exc.code}"


def _post_control_plane(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    authorization_token: str | None = None,
    idempotency_key: str | None = None,
    redact: bool = True,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Call one local control-plane action and remove bearer-like secrets."""
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
    }
    internal_auth_token = os.environ.get("DEVPILOT_CONTROL_PLANE_TOKEN")
    if internal_auth_token:
        headers["X-DevPilot-Internal-Token"] = internal_auth_token
    if authorization_token:
        headers["Authorization"] = f"DevPilotGrant {authorization_token}"
    request = Request(
        f"{_control_plane_url()}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        response_body = response.read(256 * 1024 + 1)
    if len(response_body) > 256 * 1024:
        raise ValueError("control-plane response is too large")
    result = json.loads(response_body.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("control-plane response is invalid")
    return _model_result(result) if redact else result


def _get_control_plane(path: str) -> dict[str, Any]:
    headers: dict[str, str] = {}
    internal_auth_token = os.environ.get("DEVPILOT_CONTROL_PLANE_TOKEN")
    if internal_auth_token:
        headers["X-DevPilot-Internal-Token"] = internal_auth_token
    request = Request(f"{_control_plane_url()}{path}", headers=headers, method="GET")
    with urlopen(request, timeout=10) as response:
        response_body = response.read(256 * 1024 + 1)
    if len(response_body) > 256 * 1024:
        raise ValueError("control-plane response is too large")
    result = json.loads(response_body.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("control-plane response is invalid")
    return _model_result(result)


def _snapshot_verification_profile(snapshot: Mapping[str, Any]) -> str:
    """Read the server-owned verification profile from a ChangeSet snapshot."""
    direct = snapshot.get("verification_profile")
    if isinstance(direct, str) and direct in {
        "python-syntax",
        "python-regression",
        "python-run",
        "node-syntax",
        "react-static",
    }:
        return direct
    for key in ("bug_fix_contract", "test_repair_contract"):
        contract = snapshot.get(key)
        if isinstance(contract, Mapping):
            profile = contract.get("verification_profile")
            if isinstance(profile, str) and profile in {
                "python-syntax",
                "python-run",
                "node-syntax",
                "react-static",
            }:
                return profile
    return "python-syntax"


def _get_control_plane_events(
    path: str, *, last_event_id: int | None = None
) -> tuple[str, str, list[dict[str, Any]]]:
    """Read the finite, server-owned SSE projection without accepting arbitrary URLs."""
    headers: dict[str, str] = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    internal_auth_token = os.environ.get("DEVPILOT_CONTROL_PLANE_TOKEN")
    if internal_auth_token:
        headers["X-DevPilot-Internal-Token"] = internal_auth_token
    request = Request(f"{_control_plane_url()}{path}", headers=headers, method="GET")
    with urlopen(request, timeout=15) as response:
        body = response.read(512 * 1024 + 1)
    if len(body) > 512 * 1024:
        raise ValueError("control-plane event stream is too large")

    events: list[dict[str, Any]] = []
    event_id: str | None = None
    event_name: str | None = None
    data_lines: list[str] = []
    previous_event_id: int | None = None
    previous_sequence: int | None = None

    def flush() -> None:
        nonlocal event_id, event_name, data_lines, previous_event_id, previous_sequence
        if not data_lines:
            event_id = None
            event_name = None
            return
        try:
            payload = json.loads("\n".join(data_lines))
        except (TypeError, ValueError) as exc:
            raise ValueError("control-plane SSE data is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("control-plane SSE event is invalid")
        if event_id is None:
            raise ValueError("control-plane SSE event id is missing")
        try:
            numeric_id = int(event_id)
        except ValueError as exc:
            raise ValueError("control-plane SSE event id is invalid") from exc
        if numeric_id < 0:
            raise ValueError("control-plane SSE event id is invalid")
        sequence_value = payload.get("sequence")
        if not isinstance(sequence_value, (int, str)) or isinstance(sequence_value, bool):
            raise ValueError("control-plane SSE sequence is invalid")
        try:
            numeric_sequence = int(sequence_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("control-plane SSE sequence is invalid") from exc
        if numeric_sequence < 1:
            raise ValueError("control-plane SSE sequence is invalid")
        if previous_event_id is not None and numeric_id <= previous_event_id:
            raise ValueError("control-plane SSE event order is invalid")
        if previous_sequence is not None and numeric_sequence <= previous_sequence:
            raise ValueError("control-plane SSE sequence order is invalid")
        # The SSE frame is the server-owned envelope.  Never let JSON data
        # override the cursor or event type parsed from that envelope.
        payload["event_id"] = numeric_id
        payload["sequence"] = numeric_sequence
        if event_name:
            payload["type"] = event_name
        events.append(payload)
        previous_event_id = numeric_id
        previous_sequence = numeric_sequence
        event_id = None
        event_name = None
        data_lines = []

    for raw_line in body.decode("utf-8", errors="strict").splitlines():
        if not raw_line:
            flush()
            continue
        if raw_line.startswith(":"):
            continue
        field, separator, value = raw_line.partition(":")
        if not separator:
            continue
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            event_id = value
        elif field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    flush()

    run_id = ""
    status = "running"
    if events:
        run_id = str(events[-1].get("run_id", ""))
        for event in reversed(events):
            event_type = event.get("type")
            if event_type == "run.completed":
                status = "completed"
                break
            if event_type == "run.failed":
                status = "failed"
                break
    return run_id, status, events


def _change_set_path(change_set_id: str, action: str) -> str:
    """Build a safe local control-plane path from an exact ChangeSet ID."""
    value = str(change_set_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid ChangeSet ID")
    if action not in {
        "approve",
        "apply",
        "verify",
        "rollback",
        "approval-requests",
        "full-access-apply-verify",
    }:
        raise ValueError("unsupported ChangeSet action")
    return f"/v1/change-sets/{value}/{action}"


def _apply_changeset(change_set_id: str, approval_token: str) -> dict[str, Any]:
    apply_result = _post_control_plane(
        _change_set_path(change_set_id, "apply"),
        authorization_token=approval_token,
    )
    return {
        "change_set_id": change_set_id,
        "apply": apply_result,
        "status": apply_result.get("status", "unknown"),
        "next_step": (
            "Apply 已完成。请通过控制面为精确的 ChangeSet 签发 r2.verify 审批，"
            "再调用 devpilot_verify_changeset；本工具不会自行签发审批。"
        ),
    }


def _verify_changeset(
    change_set_id: str,
    verification_profile: str,
    approval_token: str,
) -> dict[str, Any]:
    verify_result = _post_control_plane(
        _change_set_path(change_set_id, "verify"),
        {"profile": verification_profile},
        authorization_token=approval_token,
    )
    return {
        "change_set_id": change_set_id,
        "verification": verify_result,
        "status": verify_result.get("status", "unknown"),
    }


def _safe_resource_path(identifier: str, resource: str) -> str:
    value = str(identifier)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid resource ID")
    return f"/v1/{resource}/{value}"


@tool_parameters(
    {
        "type": "object",
        "properties": {"change_set_id": {"type": "string", "minLength": 1}},
        "required": ["change_set_id"],
        "additionalProperties": False,
    }
)
class GetChangeSetTool(Tool):
    """Read one server-owned immutable ChangeSet preview and its persisted contract."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_get_changeset"

    @property
    def description(self) -> str:
        return "Read-only ChangeSet state, Diff, hashes and persisted approval contract."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            result = _get_control_plane(
                _safe_resource_path(str(kwargs["change_set_id"]), "change-sets")
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P4 ChangeSet read failed: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P4 ChangeSet read failed: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {"change_set_id": {"type": "string", "minLength": 1}},
        "required": ["change_set_id"],
        "additionalProperties": False,
    }
)
class GetDeliveryReportTool(Tool):
    """Read the compact, redacted P4-04 delivery report."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_get_delivery_report"

    @property
    def description(self) -> str:
        return (
            "Read-only delivery outcome, verification metadata, artifacts and available "
            "stop/rollback recovery actions; never changes files or capabilities."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            result = _get_control_plane(
                _safe_resource_path(str(kwargs["change_set_id"]), "change-sets")
                + "/delivery-report"
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P4 delivery report failed: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P4 delivery report failed: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {"run_id": {"type": "string", "minLength": 1}},
        "required": ["run_id"],
        "additionalProperties": False,
    }
)
class GetRunStateTool(Tool):
    """Read one server-owned Run state and redacted event stream."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_get_run_state"

    @property
    def description(self) -> str:
        return "Read-only Run lifecycle state and structured events; never changes execution."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            result = _get_control_plane(_safe_resource_path(str(kwargs["run_id"]), "runs"))
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P4 Run read failed: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P4 Run read failed: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "minLength": 1},
            "last_event_id": {"type": "integer", "minimum": 0},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    }
)
class WatchRunTool(Tool):
    """Project persisted Run events into safe nanobot conversation updates."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_watch_run"

    @property
    def description(self) -> str:
        return (
            "Read the next redacted DevPilot Run events through the internal SSE projection. "
            "Supports Last-Event-ID replay and falls back to the durable snapshot on a "
            "transport failure; never returns raw output, paths, credentials or tokens."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            run_id = str(kwargs["run_id"])
            cursor = kwargs.get("last_event_id")
            if cursor is not None and (not isinstance(cursor, int) or cursor < 0):
                raise ValueError("last_event_id must be a non-negative integer")
            path = _safe_resource_path(run_id, "runs") + "/events"
            try:
                streamed_run_id, status, events = _get_control_plane_events(
                    path, last_event_id=cursor
                )
                if events:
                    projection = project_sse_events(
                        run_id=streamed_run_id or run_id,
                        status=status,
                        events=events,
                    )
                else:
                    # An empty replay says only that no new events arrived;
                    # it cannot prove that the Run is still running.  Read the
                    # durable snapshot to recover the authoritative terminal
                    # status and latest event cursor.
                    snapshot = _get_control_plane(_safe_resource_path(run_id, "runs"))
                    projection = project_run_state(snapshot, fallback=True)
            except (OSError, URLError, TimeoutError, ValueError):
                # The database snapshot is the source of truth.  A broken SSE
                # connection or malformed replay is never interpreted as success.
                snapshot = _get_control_plane(_safe_resource_path(run_id, "runs"))
                projection = project_run_state(snapshot, fallback=True)
            return json.dumps(projection.to_dict(), ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P5 Run event bridge failed: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P5 Run event bridge failed: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["apply", "verify", "rollback", "apply_verify"]},
            "change_set_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "plan_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "patch_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "scope_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "project_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "verification_profile": {
                "type": ["string", "null"],
                "description": (
                    "Optional hint only. For verify/apply_verify the ChangeSet's server-bound "
                    "profile is authoritative; omit this field."
                ),
                "enum": [
                    "python-syntax", "python-regression", "python-run", "node-syntax",
                    "react-static", None,
                ],
            },
        },
        "required": [
            "action",
            "change_set_id",
            "plan_hash",
            "patch_hash",
            "scope_hash",
            "project_id",
        ],
        "additionalProperties": False,
    }
)
class ConfirmedActionTool(Tool):
    """Request one trusted user-confirmed action without accepting a token."""

    _scopes = {"core"}

    def __init__(self) -> None:
        self._request_context: Any | None = None

    def set_context(self, context: Any) -> None:
        """Keep the authoritative request context for child-task execution."""
        self._request_context = context

    @property
    def name(self) -> str:
        return "devpilot_confirmed_action"

    @property
    def description(self) -> str:
        return (
            "Execute a trusted apply, verify, rollback, or bounded apply-and-verify action only "
            "when the nanobot "
            "Gateway supplies a valid out-of-band signed user confirmation. The model cannot "
            "provide a token, confirmation context, or idempotency key."
        )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        request_context = self._request_context
        attributes = _trusted_confirmation_attributes(self._request_context)
        _transport_logger.warning(
            "DevPilot confirmation entry: has_attributes=%s action=%s attr_keys=%s",
            attributes is not None,
            kwargs.get("action"),
            sorted(attributes) if isinstance(attributes, dict) else [],
        )
        if attributes is None or attributes.get("action") != kwargs.get("action"):
            _transport_logger.warning(
                "DevPilot confirmation rejected at entry: expected_action=%s actual_action=%s",
                kwargs.get("action"),
                attributes.get("action") if isinstance(attributes, dict) else None,
            )
            return ToolResult.error(
                "P5 action rejected: APPROVAL_BROKER_UNAVAILABLE; "
                "本次用户意图已收到，但审批通道暂不可用，动作未执行；不要要求用户重复确认。"
            )
        try:
            verifier = cast(Callable[[str, str], bool], attributes.get("verifier"))
            if not callable(verifier):
                raise ValueError("missing trusted verifier")
            context = ConfirmationContext.from_mapping(attributes["context"])
            store = _durable_store()
            request_action = str(kwargs.get("action", ""))

            def consume(
                request: BrokerActionRequest, idempotency_key: str
            ) -> bool | Mapping[str, Any] | ConfirmationReservation:
                tuple_payload: dict[str, Any] = {
                    "session_id": request.context.session_id,
                    "thread_id": request.context.thread_id,
                    "confirmation_id": request.context.confirmation_id,
                    "user_event_nonce": request.context.user_event_nonce,
                    "user_message_seq": request.context.user_message_seq,
                    "action": request.action,
                    "change_set_id": request.change_set_id,
                    "plan_hash": request.plan_hash,
                    "patch_hash": request.patch_hash,
                    "scope_hash": request.scope_hash,
                    "project_id": request.project_id,
                }
                reserved = store.reserve_broker_action(
                    {
                        **tuple_payload,
                        "signature_digest": hashlib.sha256(
                            request.context.signature.encode()
                        ).hexdigest(),
                        "idempotency_digest": hashlib.sha256(idempotency_key.encode()).hexdigest(),
                        "idempotency_key": idempotency_key,
                        "status": "authorized",
                        "expires_at": request.context.validate_time(datetime.now(UTC))[1],
                    }
                )
                if reserved:
                    return True
                replayed = store.get_broker_action_result(tuple_payload)
                if replayed is not None:
                    return replayed
                previous_key = store.get_broker_action_idempotency_key(tuple_payload)
                if previous_key is not None:
                    return ConfirmationReservation(previous_key)
                return False

            def snapshot(change_set_id: str) -> dict[str, Any]:
                result = _get_control_plane(_safe_resource_path(change_set_id, "change-sets"))
                security = _get_control_plane(f"/v1/projects/{kwargs['project_id']}/security")
                profile = _snapshot_verification_profile(result)
                return {
                    **result,
                    "trust_state": security.get("trust_state"),
                    "verification_profile": profile,
                    "verification_profile_version": result.get("verification_profile_version"),
                    "verification_snapshot_hash": result.get("verification_snapshot_hash"),
                    "verification_check_set_hash": result.get("verification_check_set_hash"),
                    "verification_available": result.get("verification_available") is True,
                }

            def finish(
                request: BrokerActionRequest, status: str, result: Mapping[str, Any]
            ) -> None:
                # The control plane reports its domain terminal state (for
                # example, ``applied`` or ``verified``), while the broker
                # record uses ``completed`` for every successful terminal
                # action. Keep the domain status in result_json for replay.
                broker_status = _broker_terminal_status(status)
                store.finish_broker_action(
                    request.context.confirmation_id,
                    broker_status,
                    dict(result),
                    expected={
                        "session_id": request.context.session_id,
                        "thread_id": request.context.thread_id,
                        "action": request.action,
                        "change_set_id": request.change_set_id,
                        "plan_hash": request.plan_hash,
                        "patch_hash": request.patch_hash,
                        "scope_hash": request.scope_hash,
                        "project_id": request.project_id,
                    },
                )

            def issue(action_name: str, change_set_id: str) -> str:
                raw = _post_control_plane(
                    _change_set_path(change_set_id, "approve"),
                    {
                        "capability": {
                            "apply": "r1.apply",
                            "verify": "r2.verify",
                            "rollback": "r1.rollback",
                        }[action_name]
                    },
                    redact=False,
                )
                token = raw.get("approval_token")
                if not isinstance(token, str):
                    raise ValueError("approval token unavailable")
                return token

            def execute(
                action_name: str, change_set_id: str, token: str, key: str, profile: str | None
            ) -> dict[str, Any]:
                payload: dict[str, Any] | None = (
                    {"profile": profile or "python-syntax"} if action_name == "verify" else None
                )
                if action_name == "verify" and payload is not None:
                    bound = _get_control_plane(
                        _safe_resource_path(str(change_set_id), "change-sets")
                    )
                    contract = bound.get("bug_fix_contract")
                    if not isinstance(contract, Mapping):
                        contract = {}
                    request_attributes = getattr(request_context, "attributes", {})
                    skill_id = (
                        request_attributes.get("devpilot_skill_id")
                        if isinstance(request_attributes, Mapping)
                        else None
                    )
                    if skill_id in {"feature-delivery", "bug-fix"}:
                        allowed_files = contract.get("allowed_files", ())
                        improvement_context = _skill_improvement_context(
                            request_context,
                            project_path=str(
                                getattr(request_context, "workspace", None) or Path.cwd()
                            ),
                            skill_id=str(skill_id),
                            summary=str(
                                contract.get("symptom")
                                or "Confirmed verification failed"
                            ),
                            expected_behavior=str(
                                contract.get("expected_behavior")
                                or f"{profile or 'python-syntax'} verification passes"
                            ),
                            failure_evidence_refs=contract.get(
                                "failure_evidence_ids", ()
                            ),
                            allowed_file_scope=allowed_files,
                            verification_profile=profile,
                        )
                        if improvement_context is not None:
                            payload["skill_improvement_context"] = improvement_context
                raw = _post_control_plane(
                    _change_set_path(change_set_id, action_name),
                    payload,
                    authorization_token=token,
                    idempotency_key=key,
                )
                return {
                    key: raw.get(key) for key in ("status", "run_id", "error_code", "result_ref")
                }

            broker = ActionBroker(
                signature_verifier=verifier,
                snapshot_reader=snapshot,
                capability_issuer=issue,
                capability_executor=execute,
                confirmation_consumer=consume,
                confirmation_finisher=finish,
            )
            bound_profile: str | None = None
            if request_action in {"verify", "apply_verify"}:
                bound_snapshot = _get_control_plane(
                    _safe_resource_path(str(kwargs["change_set_id"]), "change-sets")
                )
                bound_profile = _snapshot_verification_profile(bound_snapshot)
            result = broker.execute(
                BrokerActionRequest(
                    context=context,
                    action=str(kwargs["action"]),
                    change_set_id=str(kwargs["change_set_id"]),
                    plan_hash=str(kwargs["plan_hash"]),
                    patch_hash=str(kwargs["patch_hash"]),
                    scope_hash=str(kwargs["scope_hash"]),
                    project_id=str(kwargs["project_id"]),
                    verification_profile=bound_profile,
                )
            )
            return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return ToolResult.error(f"P5 action rejected: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "project_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "source_type": {
                "type": "string",
                "enum": ["user_feedback", "tool_result", "model_inference", "external_content"],
            },
            "skill_hash": {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"},
            "correction": {"type": "string", "minLength": 1, "maxLength": 4000},
            "actual_result": {"type": "string", "minLength": 1, "maxLength": 4000},
            "evidence_refs": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
            "batch_id": {"type": ["string", "null"], "maxLength": 128},
        },
        "required": [
            "task_id", "project_id", "source_type", "skill_hash", "correction", "actual_result",
        ],
        "additionalProperties": False,
    }
)
class SkillFeedbackTool(Tool):
    """Record bounded, redacted feedback for the single P8 trial Skill."""

    _scopes = {"core"}

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _skill_improvement_enabled()

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    @classmethod
    def create(cls, ctx: ToolContext) -> SkillFeedbackTool:
        return cls(Path(ctx.workspace).expanduser().resolve())

    @property
    def name(self) -> str:
        return "devpilot_skill_feedback"

    @property
    def description(self) -> str:
        return (
            "Record explicit feedback or a tool result for the bug-fix Skill. "
            "This creates evidence only; it never changes a Skill or grants authority."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            if not _skill_improvement_enabled():
                raise SkillImprovementError("P8 Skill improvement is disabled")
            try:
                from nanobot.agent.tools.context import current_request_context

                request_context = current_request_context()
            except ImportError:
                request_context = None
            if request_context is None or not request_context.sender_id:
                raise SkillImprovementError("feedback requires a trusted request context")
            request_workspace = request_context.workspace or self.workspace
            trusted_attributes = request_context.attributes
            trusted_task_id = trusted_attributes.get("devpilot_task_id")
            trusted_project_id = trusted_attributes.get("devpilot_project_id")
            if not isinstance(trusted_task_id, str) or not isinstance(
                trusted_project_id, str
            ):
                raise SkillImprovementError("feedback is outside a trusted bug-fix task")
            if str(kwargs["task_id"]) != trusted_task_id:
                raise SkillImprovementError("task_id does not match the trusted task")
            if str(kwargs["project_id"]) != trusted_project_id:
                raise SkillImprovementError("project_id does not match the trusted project")
            source_type = str(kwargs["source_type"])
            if source_type == "user_feedback":
                raise SkillImprovementError(
                    "user feedback must be recorded by the native user command"
                )
            trusted_refs: set[str] = set()
            if source_type == "tool_result":
                trusted_refs = {
                    value
                    for value in trusted_attributes.get("devpilot_evidence_refs", [])
                    if isinstance(value, str)
                }
                requested_refs = {
                    value for value in kwargs.get("evidence_refs", []) if isinstance(value, str)
                }
                if not requested_refs or not requested_refs <= trusted_refs:
                    raise SkillImprovementError("tool result is not trusted evidence")
            correction = (
                f"Trusted tool result references: {', '.join(sorted(trusted_refs))}"
                if source_type == "tool_result"
                else str(kwargs["correction"])
            )
            actual_result = (
                f"Trusted tool result references: {', '.join(sorted(trusted_refs))}"
                if source_type == "tool_result"
                else str(kwargs["actual_result"])
            )
            evidence = SkillImprovementStore.from_environment(request_workspace).record_feedback(
                task_id=str(kwargs["task_id"]),
                project_id=str(kwargs["project_id"]),
                workspace=request_workspace,
                user_id=request_context.sender_id,
                session_id=request_context.session_key or request_context.chat_id,
                source_type=source_type,
                skill_id="bug-fix",
                skill_hash=str(kwargs["skill_hash"]),
                correction=correction,
                actual_result=actual_result,
                evidence_refs=kwargs.get("evidence_refs", ()),
                batch_id=kwargs.get("batch_id"),
            )
            return json.dumps(
                {
                    "status": "recorded",
                    "evidence": evidence.to_dict(),
                    "next_step": (
                        "Only verified evidence can form a candidate; model inference and external "
                        "content remain pending verification."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        except (KeyError, TypeError, ValueError, OSError, SkillImprovementError) as exc:
            return ToolResult.error(f"P8 feedback rejected: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "baseline_content": {"type": "string", "minLength": 1},
            "candidate_content": {"type": "string", "minLength": 1},
            "evidence_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string"},
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
            "expected_behavior": {"type": "string", "minLength": 1, "maxLength": 4000},
            "test_set_version": {"type": "string", "minLength": 1, "maxLength": 128},
            "batch_id": {"type": ["string", "null"], "maxLength": 128},
        },
        "required": [
            "project_id", "baseline_content", "candidate_content", "evidence_ids",
            "reason", "expected_behavior", "test_set_version",
        ],
        "additionalProperties": False,
    }
)
class SkillCandidateTool(Tool):
    """Create an inactive bug-fix Skill candidate in private storage."""

    _scopes = {"core", "memory"}

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _skill_improvement_enabled()

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()

    @classmethod
    def create(cls, ctx: ToolContext) -> SkillCandidateTool:
        return cls(Path(ctx.workspace).expanduser().resolve())

    @property
    def name(self) -> str:
        return "devpilot_skill_candidate"

    @property
    def description(self) -> str:
        return (
            "Create a pending candidate for the bug-fix Skill from verified evidence. "
            "The candidate is private, is not loaded by nanobot, and cannot activate itself."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            if not _skill_improvement_enabled():
                raise SkillImprovementError("P8 Skill improvement is disabled")
            try:
                from nanobot.agent.tools.context import current_request_context

                request_context = current_request_context()
            except ImportError:
                request_context = None
            if request_context is None or not request_context.sender_id:
                raise SkillImprovementError("candidate requires a trusted request context")
            trusted_project_id = request_context.attributes.get("devpilot_project_id")
            trusted_task_id = request_context.attributes.get("devpilot_task_id")
            if not isinstance(trusted_project_id, str) or not isinstance(
                trusted_task_id, str
            ):
                raise SkillImprovementError("candidate is outside a trusted bug-fix task")
            if str(kwargs["project_id"]) != trusted_project_id:
                raise SkillImprovementError("project_id does not match the trusted project")
            requested_batch_id = kwargs.get("batch_id")
            scheduled_batch_id = request_context.attributes.get("devpilot_batch_id")
            scheduled_workspace = request_context.attributes.get("devpilot_workspace")
            request_workspace = (
                Path(scheduled_workspace).expanduser().resolve()
                if isinstance(scheduled_batch_id, str)
                and scheduled_batch_id
                and isinstance(scheduled_workspace, str)
                and scheduled_workspace.strip()
                else request_context.workspace or self.workspace
            )
            store = SkillImprovementStore.from_environment(request_workspace)
            if isinstance(scheduled_batch_id, str) and scheduled_batch_id:
                if requested_batch_id != scheduled_batch_id:
                    raise SkillImprovementError(
                        "scheduled candidate must use the trusted batch"
                    )
                allowed_evidence = {
                    value
                    for value in request_context.attributes.get(
                        "devpilot_batch_evidence_ids", []
                    )
                    if isinstance(value, str)
                }
                requested_evidence = {
                    value for value in kwargs["evidence_ids"] if isinstance(value, str)
                }
                if requested_evidence != allowed_evidence:
                    raise SkillImprovementError(
                        "scheduled candidate evidence does not match the claimed batch"
                    )
                owner = request_context.attributes.get("devpilot_batch_owner")
                if not isinstance(owner, str) or not owner:
                    raise SkillImprovementError("scheduled candidate lease owner is missing")
                estimated_tokens = max(
                    1,
                    (
                        len(str(kwargs["baseline_content"]))
                        + len(str(kwargs["candidate_content"]))
                        + len(str(kwargs["reason"]))
                        + len(str(kwargs["expected_behavior"]))
                    )
                    // 4,
                )
                store.reserve_scheduled_candidate_attempt(
                    scheduled_batch_id,
                    lease_owner=owner,
                    estimated_tokens=estimated_tokens,
                )
            candidate = store.create_candidate(
                user_id=request_context.sender_id,
                project_id=str(kwargs["project_id"]),
                skill_path="skills/bug-fix/SKILL.md",
                baseline_content=str(kwargs["baseline_content"]),
                candidate_content=str(kwargs["candidate_content"]),
                evidence_ids=kwargs["evidence_ids"],
                reason=str(kwargs["reason"]),
                expected_behavior=str(kwargs["expected_behavior"]),
                test_set_version=str(kwargs["test_set_version"]),
                batch_id=requested_batch_id,
                workspace=request_workspace,
            )
            if isinstance(scheduled_batch_id, str) and scheduled_batch_id:
                store.register_scheduled_candidate(
                    scheduled_batch_id,
                    lease_owner=str(request_context.attributes["devpilot_batch_owner"]),
                    candidate_id=candidate.candidate_id,
                )
            return json.dumps(
                {
                    "status": candidate.status,
                    "candidate_id": candidate.candidate_id,
                    "skill_id": candidate.skill_id,
                    "base_version_hash": candidate.base_version_hash,
                    "candidate_hash": candidate.candidate_hash,
                    "evidence_ids": list(candidate.evidence_ids),
                    "next_step": (
                        "Run the fixed P8-02 comparison; do not load or activate "
                        "this candidate yet."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        except (KeyError, TypeError, ValueError, OSError, SkillImprovementError) as exc:
            return ToolResult.error(f"P8 candidate rejected: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_path": {
                "type": "string",
                "description": (
                    "Absolute project directory, or a path relative to the nanobot workspace."
                ),
                "minLength": 1,
            },
            "project_id": {"type": "string", "minLength": 1},
            "patches": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": _PATCH_ITEM_SCHEMA,
            },
        },
        "required": ["project_path", "patches"],
        "additionalProperties": False,
    }
)
class ProposeChangeSetTool(Tool):
    """Submit a bounded patch proposal; never approves or applies it."""

    _scopes = {"core"}

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace or Path.cwd()
        self._request_context: Any | None = None

    def set_context(self, context: Any) -> None:
        """Keep the registry's authoritative request object for this one call."""
        self._request_context = context

    @classmethod
    def create(cls, ctx: ToolContext) -> ProposeChangeSetTool:
        return cls(Path(ctx.workspace).expanduser().resolve())

    @property
    def name(self) -> str:
        return "devpilot_propose_changeset"

    @property
    def description(self) -> str:
        return (
            "Create a bounded ChangeSet preview through the local DevPilot control plane. "
            "This tool is proposal-only and cannot approve, apply, execute, or rollback changes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "minLength": 1},
                "project_id": {"type": "string", "minLength": 1},
                "patches": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": _PATCH_ITEM_SCHEMA,
                },
            },
            "required": ["project_path", "patches"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return True

    @property
    def exclusive(self) -> bool:
        """Proposal delivery mutates only the current request's private metadata."""
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        request_context = self._request_context
        try:
            project_path = str(kwargs["project_path"])
            patches = kwargs["patches"]
            if not isinstance(patches, list):
                raise TypeError("patches must be a list")
            if _is_trusted_full_access(request_context):
                return json.dumps(
                    _full_access_preview_and_execute(
                        project_path=project_path,
                        patches=patches,
                        project_id=(
                            str(kwargs["project_id"])
                            if kwargs.get("project_id") is not None
                            else None
                        ),
                        verification_profile="python-syntax",
                        request_context=request_context,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            payload: dict[str, Any] = {
                "project_path": project_path,
                "patches": _normalize_patch_hashes(patches),
            }
            if kwargs.get("project_id") is not None:
                payload["project_id"] = str(kwargs["project_id"])
            result = _post_preview(payload)
            _record_current_turn_proposal(result, request_context=request_context)
            result["requires_user_confirmation"] = True
            result["next_step"] = (
                "在 nanobot 对话中展示这份 Diff，并停止等待用户决定；"
                "执行必须用 action=apply_verify（Apply 后验证）；仅应用、验证或撤销后，"
                "使用这份完整 tuple 调用 "
                "devpilot_confirmed_action；不要调用旧审批工具，也不要请求或传播 token。"
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P2B proposal rejected: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P2B proposal rejected: {type(exc).__name__}")
        finally:
            self._request_context = None


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "minLength": 1},
            "patches": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": _PATCH_ITEM_SCHEMA,
            },
            "evidence_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "description": (
                    "Use only repo-analysis run_id values returned by "
                    "devpilot_inspect_project and extended by search/read in "
                    "this proposal. Never pass project_id as evidence."
                ),
            },
            "verification_profile": {
                "type": "string",
                "enum": ["python-syntax", "python-regression", "python-run", "node-syntax"],
            },
        },
        "required": ["project_path", "patches", "evidence_ids"],
        "additionalProperties": False,
    }
)
class FeatureDeliveryProposeTool(Tool):
    """Create a FeatureDeliveryWorkflow proposal from bound read-only evidence."""

    _scopes = {"core"}

    def __init__(self) -> None:
        self._request_context: Any | None = None

    def set_context(self, context: Any) -> None:
        """Keep the registry's authoritative request object for this one call."""
        self._request_context = context

    @property
    def name(self) -> str:
        return "devpilot_feature_delivery_propose"

    @property
    def description(self) -> str:
        return (
            "Create a feature-delivery proposal using previously collected evidence. "
            "This is proposal-only and never requests approval or writes project files."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def exclusive(self) -> bool:
        """Proposal delivery mutates only the current request's private metadata."""
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        request_context = self._request_context
        try:
            if _is_trusted_full_access(request_context):
                patches = kwargs["patches"]
                if not isinstance(patches, list):
                    raise TypeError("patches must be a list")
                return json.dumps(
                    _full_access_preview_and_execute(
                        project_path=str(kwargs["project_path"]),
                        patches=patches,
                        verification_profile=(
                            str(kwargs["verification_profile"])
                            if kwargs.get("verification_profile") is not None
                            else "python-syntax"
                        ),
                        request_context=request_context,
                        skill_id="feature-delivery",
                        improvement_summary="Feature delivery verification failed",
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            payload = {
                "project_path": str(kwargs["project_path"]),
                "patches": kwargs["patches"],
                "evidence_ids": kwargs["evidence_ids"],
            }
            request_attributes = getattr(request_context, "attributes", {})
            task_id = (
                request_attributes.get("devpilot_task_id")
                if isinstance(request_attributes, Mapping)
                else None
            )
            if isinstance(task_id, str) and task_id:
                payload["task_id"] = task_id
            if kwargs.get("verification_profile") is not None:
                payload["verification_profile"] = str(kwargs["verification_profile"])
            result = _post_feature_proposal(payload)
            _record_current_turn_proposal(result, request_context=request_context)
            result["requires_user_confirmation"] = True
            result["next_step"] = (
                "展示精确 Diff 并停止等待用户；执行必须用 action=apply_verify（Apply 后验证）；"
                "仅应用、验证或撤销后，"
                "使用这份完整 tuple 调用 devpilot_confirmed_action，不要调用旧审批工具。"
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P4 feature proposal rejected: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P4 feature proposal rejected: {type(exc).__name__}")
        finally:
            self._request_context = None


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "minLength": 1},
            "patches": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": _PATCH_ITEM_SCHEMA,
            },
            "analysis_evidence_ids": {"type": "array", "minItems": 1, "maxItems": 32},
            "failure_evidence_ids": {"type": "array", "minItems": 1, "maxItems": 32},
            "symptom": {"type": "string", "minLength": 1, "maxLength": 4000},
            "expected_behavior": {"type": "string", "minLength": 1, "maxLength": 4000},
            "non_goals": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
            "allowed_files": {"type": "array", "minItems": 1, "maxItems": 32},
            "verification_profile": {
                "type": "string",
                "enum": [
                    "python-syntax",
                    "python-regression",
                    "python-run",
                    "node-syntax",
                    "react-static",
                ],
            },
        },
        "required": [
            "project_path",
            "patches",
            "analysis_evidence_ids",
            "failure_evidence_ids",
            "symptom",
            "expected_behavior",
            "allowed_files",
        ],
        "additionalProperties": False,
    }
)
class BugFixProposeTool(Tool):
    """Create a bug-fix proposal bound to saved failure evidence."""

    _scopes = {"core"}

    def __init__(self) -> None:
        self._request_context: Any | None = None

    def set_context(self, context: Any) -> None:
        """Keep the registry's authoritative request object for this one call."""
        self._request_context = context

    @property
    def name(self) -> str:
        return "devpilot_bug_fix_propose"

    @property
    def description(self) -> str:
        return (
            "Create a bounded bug-fix proposal from saved failure evidence. "
            "This is proposal-only and never approves, applies, executes, or rolls back changes."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def exclusive(self) -> bool:
        """Proposal delivery mutates only the current request's private metadata."""
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        request_context = self._request_context
        try:
            if _is_trusted_full_access(request_context):
                patches = kwargs["patches"]
                if not isinstance(patches, list):
                    raise TypeError("patches must be a list")
                return json.dumps(
                    _full_access_preview_and_execute(
                        project_path=str(kwargs["project_path"]),
                        patches=patches,
                        verification_profile=(
                            str(kwargs["verification_profile"])
                            if kwargs.get("verification_profile") is not None
                            else "python-syntax"
                        ),
                        request_context=request_context,
                        skill_id="bug-fix",
                        improvement_summary=str(kwargs["symptom"]),
                        improvement_expected_behavior=str(kwargs["expected_behavior"]),
                        failure_evidence_refs=kwargs["failure_evidence_ids"],
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            payload: dict[str, Any] = {
                "project_path": str(kwargs["project_path"]),
                "patches": kwargs["patches"],
                "analysis_evidence_ids": kwargs["analysis_evidence_ids"],
                "failure_evidence_ids": kwargs["failure_evidence_ids"],
                "symptom": str(kwargs["symptom"]),
                "expected_behavior": str(kwargs["expected_behavior"]),
                "non_goals": kwargs.get("non_goals", []),
                "allowed_files": kwargs["allowed_files"],
            }
            request_attributes = getattr(request_context, "attributes", {})
            task_id = (
                request_attributes.get("devpilot_task_id")
                if isinstance(request_attributes, Mapping)
                else None
            )
            if isinstance(task_id, str) and task_id:
                payload["task_id"] = task_id
            if kwargs.get("verification_profile") is not None:
                payload["verification_profile"] = str(kwargs["verification_profile"])
            result = _post_bug_fix_proposal(payload)
            _record_current_turn_proposal(result, request_context=request_context)
            result["next_step"] = (
                "展示失败证据、精确 Diff 和 stop conditions，并停止等待用户；"
                "执行必须用 action=apply_verify（Apply 后验证）；仅应用、验证或撤销后，"
                "使用这份完整 tuple 调用 "
                "devpilot_confirmed_action，不要调用旧审批工具。"
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P4 bug-fix proposal rejected: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P4 bug-fix proposal rejected: {type(exc).__name__}")
        finally:
            self._request_context = None


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "minLength": 1},
            "patches": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": _PATCH_ITEM_SCHEMA,
            },
            "analysis_evidence_ids": {"type": "array", "minItems": 1, "maxItems": 32},
            "failure_evidence_ids": {"type": "array", "minItems": 1, "maxItems": 32},
            "symptom": {"type": "string", "minLength": 1, "maxLength": 4000},
            "expected_behavior": {"type": "string", "minLength": 1, "maxLength": 4000},
            "non_goals": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
            "allowed_files": {"type": "array", "minItems": 1, "maxItems": 32},
            "verification_profile": {
                "type": "string",
                "enum": ["python-regression", "python-run", "node-syntax"],
            },
        },
        "required": [
            "project_path",
            "patches",
            "analysis_evidence_ids",
            "failure_evidence_ids",
            "symptom",
            "expected_behavior",
            "allowed_files",
        ],
        "additionalProperties": False,
    }
)
class TestRepairProposeTool(Tool):
    """Create a bounded test-repair proposal from saved test failure evidence."""

    _scopes = {"core"}

    def __init__(self) -> None:
        self._request_context: Any | None = None

    def set_context(self, context: Any) -> None:
        """Keep the registry's authoritative request object for this one call."""
        self._request_context = context

    @property
    def name(self) -> str:
        return "devpilot_test_repair_propose"

    @property
    def description(self) -> str:
        return (
            "Create a bounded Python or Node test-repair proposal from saved failure evidence. "
            "This is proposal-only and never approves, applies, executes, or rolls back changes."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def exclusive(self) -> bool:
        """Proposal delivery mutates only the current request's private metadata."""
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        request_context = self._request_context
        try:
            if _is_trusted_full_access(request_context):
                patches = kwargs["patches"]
                if not isinstance(patches, list):
                    raise TypeError("patches must be a list")
                return json.dumps(
                    _full_access_preview_and_execute(
                        project_path=str(kwargs["project_path"]),
                        patches=patches,
                        verification_profile=(
                            str(kwargs["verification_profile"])
                            if kwargs.get("verification_profile") is not None
                            else "python-syntax"
                        ),
                        request_context=request_context,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            payload: dict[str, Any] = {
                "project_path": str(kwargs["project_path"]),
                "patches": kwargs["patches"],
                "analysis_evidence_ids": kwargs["analysis_evidence_ids"],
                "failure_evidence_ids": kwargs["failure_evidence_ids"],
                "symptom": str(kwargs["symptom"]),
                "expected_behavior": str(kwargs["expected_behavior"]),
                "non_goals": kwargs.get("non_goals", []),
                "allowed_files": kwargs["allowed_files"],
            }
            if kwargs.get("verification_profile") is not None:
                payload["verification_profile"] = str(kwargs["verification_profile"])
            result = _post_test_repair_proposal(payload)
            _record_current_turn_proposal(result, request_context=request_context)
            result["next_step"] = (
                "展示精确 Diff、当前轮次和剩余预算，并停止等待用户；"
                "每轮执行必须用 action=apply_verify（Apply 后验证）；仅应用或验证，"
                "再使用这份完整 tuple 调用 "
                "devpilot_confirmed_action；不要调用旧审批工具。"
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P4 test-repair proposal rejected: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P4 test-repair proposal rejected: {type(exc).__name__}")
        finally:
            self._request_context = None


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "change_set_id": {"type": "string", "minLength": 1},
            "capability": {
                "type": "string",
                "enum": ["r1.apply", "r2.verify", "r1.rollback"],
            },
            "user_confirmation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "description": "Natural-language confirmation for the requested capability.",
            },
        },
        "required": ["change_set_id", "capability", "user_confirmation"],
        "additionalProperties": False,
    }
)
class RequestApprovalTool(Tool):
    """Request operator approval without exposing or minting a capability token."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_request_approval"

    @property
    def description(self) -> str:
        return (
            "Record a user-confirmed approval request for an exact ChangeSet. "
            "The control plane operator must approve separately; no token is returned."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            capability = str(kwargs["capability"])
            confirmation = str(kwargs["user_confirmation"])
            if not capability_confirmation_matches(confirmation, capability):
                return ToolResult.error(
                    "P4 approval request rejected: confirmation does not match capability"
                )
            result = _request_approval(
                str(kwargs["change_set_id"]),
                {
                    "capability": capability,
                    "user_confirmation": confirmation,
                },
            )
            return json.dumps(_model_result(result), ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P4 approval request rejected: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P4 approval request rejected: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "change_set_id": {
                "type": "string",
                "description": "The exact ChangeSet ID returned by devpilot_propose_changeset.",
                "minLength": 1,
            },
            "verification_profile": {
                "type": "string",
                "enum": ["python-syntax", "python-run", "node-syntax"],
                "description": (
                    "Server-allowlisted verification profile; use python-run for self-tests."
                ),
            },
            "approval_token": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "description": "Opaque r1.apply token issued by the control plane user approval.",
            },
            "user_confirmation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "description": "Natural-language confirmation for this exact ChangeSet.",
            },
        },
        "required": [
            "change_set_id",
            "verification_profile",
            "approval_token",
            "user_confirmation",
        ],
        "additionalProperties": False,
    }
)
class ExecuteChangeSetTool(Tool):
    """Legacy/test-only token adapter; not registered as a nanobot entry point."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_execute_changeset"

    @property
    def description(self) -> str:
        return (
            "Apply an existing DevPilot ChangeSet after explicit user confirmation and an "
            "externally issued r1.apply approval token. This tool never calls the approval "
            "endpoint and never verifies automatically."
        )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            change_set_id = str(kwargs["change_set_id"])
            # Validate the identifier before checking conversational intent so
            # malformed IDs fail locally and never get hidden by a confirmation
            # error.  This remains a read-only boundary check.
            _change_set_path(change_set_id, "apply")
            confirmation = str(kwargs["user_confirmation"])
            if not confirmation_matches(confirmation, "apply"):
                return ToolResult.error(
                    "P2B execution rejected: explicit user confirmation required"
                )
            result = _apply_changeset(change_set_id, str(kwargs["approval_token"]))
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P2B execution failed: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P2B execution failed: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "change_set_id": {
                "type": "string",
                "description": "The exact ChangeSet ID returned by devpilot_propose_changeset.",
                "minLength": 1,
            },
            "verification_profile": {
                "type": "string",
                "enum": ["python-syntax", "python-run", "node-syntax"],
            },
            "approval_token": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "description": "Opaque r2.verify token issued after Apply by the control plane.",
            },
            "user_confirmation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "description": "Natural-language confirmation for verification.",
            },
        },
        "required": [
            "change_set_id",
            "verification_profile",
            "approval_token",
            "user_confirmation",
        ],
        "additionalProperties": False,
    }
)
class VerifyChangeSetTool(Tool):
    """Legacy/test-only token adapter; not registered as a nanobot entry point."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_verify_changeset"

    @property
    def description(self) -> str:
        return (
            "Verify an applied ChangeSet after explicit user confirmation and an externally "
            "issued r2.verify token; never mint approval or accept an arbitrary command."
        )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            confirmation = str(kwargs["user_confirmation"])
            if not (
                capability_confirmation_matches(confirmation, "r2.verify")
                or confirmation_matches(confirmation, "apply")
            ):
                return ToolResult.error(
                    "P2B verification rejected: explicit user confirmation required"
                )
            result = _verify_changeset(
                str(kwargs["change_set_id"]),
                str(kwargs["verification_profile"]),
                str(kwargs["approval_token"]),
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P2B verification failed: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P2B verification failed: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "change_set_id": {"type": "string", "minLength": 1},
            "approval_token": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "description": "Opaque r1.rollback token issued by the control plane.",
            },
            "user_confirmation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "description": "Natural-language confirmation for rollback.",
            },
        },
        "required": ["change_set_id", "approval_token", "user_confirmation"],
        "additionalProperties": False,
    }
)
class RollbackChangeSetTool(Tool):
    """Legacy/test-only token adapter; not registered as a nanobot entry point."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "devpilot_rollback_changeset"

    @property
    def description(self) -> str:
        return (
            "Rollback an applied DevPilot ChangeSet only after the user explicitly confirms "
            "rollback in chat and supplies an externally issued r1.rollback token. "
            "Never call the approval endpoint automatically."
        )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            confirmation = str(kwargs["user_confirmation"])
            if not confirmation_matches(confirmation, "rollback"):
                return ToolResult.error(
                    "P2B rollback rejected: explicit user confirmation required"
                )
            change_set_id = str(kwargs["change_set_id"])
            result = _post_control_plane(
                _change_set_path(change_set_id, "rollback"),
                authorization_token=str(kwargs["approval_token"]),
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P2B rollback failed: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P2B rollback failed: {type(exc).__name__}")
