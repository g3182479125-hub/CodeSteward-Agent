"""nanobot entry-point adapters for DevPilot analysis and chat-confirmed execution."""

# The package remains importable in DevPilot's own test environment.  When loaded
# by nanobot, the real Tool base class and schema decorator are used.
# pyright: reportMissingImports=false, reportMissingTypeStubs=false, reportAssignmentType=false
# pyright: reportUnknownVariableType=false, reportUnknownParameterType=false
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUntypedClassDecorator=false, reportUntypedBaseClass=false, reportGeneralTypeIssues=false

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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

from .code_access import CodeAccessError, CodeAccessService
from .inspector import MAX_FILE_BYTES, MAX_FILES, MAX_METADATA_BYTES
from .paths import AllowedRoots, PathBoundaryError
from .services import ProjectAnalysisService
from .storage import ProjectStore

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
    re.compile(r"(?i)deepseek_api_key\s*[=:]\s*\S+"),
)
_PATH_DENIAL_CODES = frozenset(
    {
        "PATH_INVALID",
        "PATH_NOT_RELATIVE",
        "PATH_OUTSIDE_PROJECT",
        "WINDOWS_PATH_ALIAS",
        "TARGET_NOT_FILE",
    }
)


def _configured_roots(workspace: Path) -> AllowedRoots:
    raw = os.environ.get("DEVPILOT_ALLOWED_ROOTS", "")
    values: list[str | Path] = [item for item in raw.split(os.pathsep) if item]
    return AllowedRoots.from_paths(values or [workspace])


def _data_dir() -> Path:
    return Path(os.environ.get("DEVPILOT_DATA_DIR", "./local-data")).expanduser().resolve()


def _service(workspace: Path) -> ProjectAnalysisService:
    store = ProjectStore(_data_dir() / "devpilot.db")
    store.initialize()
    return ProjectAnalysisService(allowed_roots=_configured_roots(workspace), store=store)


def _code_access_service(workspace: Path) -> CodeAccessService:
    store = ProjectStore(_data_dir() / "devpilot.db")
    store.initialize()
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
            outcome = _service(self.workspace).inspect(project_path)
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
            store = ProjectStore(_data_dir() / "devpilot.db")
            store.initialize()
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
            result = _code_access_service(self.workspace).search(
                str(kwargs["project_id"]),
                str(kwargs["query"]),
                glob=str(kwargs["glob"]) if kwargs.get("glob") is not None else None,
                limit=int(kwargs.get("limit", 50)),
                case_sensitive=bool(kwargs.get("case_sensitive", False)),
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
            result = _code_access_service(self.workspace).read(
                str(kwargs["project_id"]),
                str(kwargs["path"]),
                start_line=int(kwargs.get("start_line", 1)),
                end_line=(int(kwargs["end_line"]) if kwargs.get("end_line") is not None else None),
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
    result.pop("approval_token", None)
    return result


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
    return result


def _http_error_message(exc: HTTPError) -> str:
    """Expose bounded local API diagnostics without leaking response headers or tokens."""
    try:
        body = exc.read(4 * 1024).decode("utf-8", errors="replace").strip()
    except OSError:
        body = ""
    return f"HTTP {exc.code}: {body}" if body else f"HTTP {exc.code}"


def _post_control_plane(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    authorization_token: str | None = None,
) -> dict[str, Any]:
    """Call one local control-plane action and remove bearer-like secrets."""
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4())}
    if authorization_token:
        headers["Authorization"] = f"DevPilotGrant {authorization_token}"
    request = Request(
        f"{_control_plane_url()}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        response_body = response.read(256 * 1024 + 1)
    if len(response_body) > 256 * 1024:
        raise ValueError("control-plane response is too large")
    result = json.loads(response_body.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("control-plane response is invalid")
    return result


def _change_set_path(change_set_id: str, action: str) -> str:
    """Build a safe local control-plane path from an exact ChangeSet ID."""
    value = str(change_set_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid ChangeSet ID")
    if action not in {"approve", "apply", "verify", "rollback"}:
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
                "items": {
                    "type": "object",
                    "properties": {
                        "relative_path": {"type": "string", "minLength": 1},
                        "operation": {"type": "string", "enum": ["create", "update", "delete"]},
                        "content": {"type": ["string", "null"]},
                        "expected_sha256": {"type": ["string", "null"]},
                    },
                    "required": ["relative_path", "operation"],
                    "additionalProperties": False,
                },
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
                    "items": {"type": "object"},
                },
            },
            "required": ["project_path", "patches"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            project_path = str(kwargs["project_path"])
            patches = kwargs["patches"]
            if not isinstance(patches, list):
                raise TypeError("patches must be a list")
            payload: dict[str, Any] = {"project_path": project_path, "patches": patches}
            if kwargs.get("project_id") is not None:
                payload["project_id"] = str(kwargs["project_id"])
            result = _post_preview(payload)
            result["requires_user_confirmation"] = True
            result["next_step"] = (
                "在 nanobot 对话中展示这份 Diff，并停止等待用户决定；"
                "控制面用户批准后，再将外部签发的 r1.apply token 交给 "
                "devpilot_execute_changeset。Apply 后须为 r2.verify 单独签发审批，"
                "再调用 devpilot_verify_changeset。"
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P2B proposal rejected: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P2B proposal rejected: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "minLength": 1},
            "patches": {"type": "array", "minItems": 1, "maxItems": 12},
            "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 32},
            "verification_profile": {
                "type": "string",
                "enum": ["python-syntax", "python-run", "node-syntax"],
            },
        },
        "required": ["project_path", "patches", "evidence_ids"],
        "additionalProperties": False,
    }
)
class FeatureDeliveryProposeTool(Tool):
    """Create a FeatureDeliveryWorkflow proposal from bound read-only evidence."""

    _scopes = {"core"}

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

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            payload = {
                "project_path": str(kwargs["project_path"]),
                "patches": kwargs["patches"],
                "evidence_ids": kwargs["evidence_ids"],
            }
            if kwargs.get("verification_profile") is not None:
                payload["verification_profile"] = str(kwargs["verification_profile"])
            result = _post_feature_proposal(payload)
            result["next_step"] = (
                "展示精确 Diff 并停止等待用户；批准动作必须由控制面用户完成，"
                "Skill 不得签发或传播 approval token。"
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except HTTPError as exc:
            return ToolResult.error(f"P4 feature proposal rejected: {_http_error_message(exc)}")
        except (KeyError, TypeError, ValueError, OSError, URLError) as exc:
            return ToolResult.error(f"P4 feature proposal rejected: {type(exc).__name__}")


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
                "enum": ["执行", "确认执行", "confirmed"],
                "description": (
                    "Only supply this after the user explicitly confirmed this exact ChangeSet."
                ),
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
    """Apply one exact proposal using an externally issued approval token."""

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
            confirmation = str(kwargs["user_confirmation"])
            if confirmation not in {"执行", "确认执行", "confirmed"}:
                return ToolResult.error(
                    "P2B execution rejected: explicit user confirmation required"
                )
            result = _apply_changeset(
                str(kwargs["change_set_id"]), str(kwargs["approval_token"])
            )
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
                "enum": ["执行", "确认执行", "confirmed"],
                "description": "Only supply this after the user explicitly confirmed verification.",
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
    """Verify one applied ChangeSet using an externally issued capability."""

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
            if confirmation not in {"执行", "确认执行", "confirmed"}:
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
                "enum": ["撤销", "确认撤销", "confirmed"],
                "description": "Only supply this after the user explicitly confirmed rollback.",
            },
        },
        "required": ["change_set_id", "approval_token", "user_confirmation"],
        "additionalProperties": False,
    }
)
class RollbackChangeSetTool(Tool):
    """Rollback an applied ChangeSet using an externally issued approval token."""

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
            if confirmation not in {"撤销", "确认撤销", "confirmed"}:
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
