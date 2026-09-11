"""Small, serializable contracts for runtime-extensible plugins.

This module deliberately contains no loader or lifecycle side effects.  H1 only
defines the data that a future manager will validate and route.  The existing
entry-point loader remains the compatibility path until a later H node wires in
the managed lifecycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast


class _ValueEnum(str, Enum):
    """String-valued enum with JSON-friendly values."""

    def __str__(self) -> str:
        return self.value


class PluginKind(_ValueEnum):
    TOOL = "tool"
    SERVICE = "service"
    PROMPT = "prompt"
    SKILL = "skill"
    LOOP = "loop"


class PluginScope(_ValueEnum):
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


class PluginExecutionMode(_ValueEnum):
    IN_PROCESS = "in_process"
    FC_PROXY = "fc_proxy"


class PluginSource(_ValueEnum):
    BUILTIN = "builtin"
    ENTRY_POINT = "entry_point"
    DEVELOPER = "developer"
    MODEL_CANDIDATE = "model_candidate"


class PluginLifecycle(_ValueEnum):
    INACTIVE = "inactive"
    PREPARING = "preparing"
    WAITING = "waiting"
    ACTIVE = "active"
    DRAINING = "draining"
    FAILED = "failed"


class PluginCandidateStatus(_ValueEnum):
    DEFINED = "defined"
    TESTED = "tested"
    REJECTED = "rejected"


class PluginErrorCode(_ValueEnum):
    INVALID_MANIFEST = "invalid_manifest"
    UNKNOWN_MANIFEST_FIELD = "unknown_manifest_field"
    INVALID_PLUGIN_ID = "invalid_plugin_id"
    INVALID_VERSION = "invalid_version"
    INVALID_CONTENT_HASH = "invalid_content_hash"
    INVALID_KIND = "invalid_kind"
    INVALID_SCOPE = "invalid_scope"
    INVALID_ENTRY = "invalid_entry"
    ENTRY_OUTSIDE_ARTIFACT = "entry_outside_artifact"
    INVALID_LIST_FIELD = "invalid_list_field"
    RESERVED_CAPABILITY = "reserved_capability"
    DUPLICATE_VERSION = "duplicate_version"
    SCOPE_MISMATCH = "scope_mismatch"
    GLOBAL_SOURCE_REJECTED = "global_source_rejected"
    MODEL_CANDIDATE_NOT_ACTIVE = "model_candidate_not_active"
    MODEL_CANDIDATE_NOT_FC_PROXY = "model_candidate_not_fc_proxy"
    INVALID_CANDIDATE_STATE = "invalid_candidate_state"
    PLUGIN_NOT_FOUND = "plugin_not_found"
    PLUGIN_KIND_UNSUPPORTED = "plugin_kind_unsupported"
    PLUGIN_SOURCE_UNTRUSTED = "plugin_source_untrusted"
    PLUGIN_NOT_PURE = "plugin_not_pure"
    PLUGIN_NOT_AUTHORIZED = "plugin_not_authorized"
    PLUGIN_ALREADY_ACTIVE = "plugin_already_active"
    PLUGIN_NOT_ACTIVE = "plugin_not_active"
    PLUGIN_LOAD_FAILED = "plugin_load_failed"
    TOOL_VERSION_RETIRED = "tool_version_retired"
    PLUGIN_STOP_TIMEOUT = "plugin_stop_timeout"
    PLUGIN_UPDATE_FAILED = "plugin_update_failed"
    PLUGIN_REMOVE_REJECTED = "plugin_remove_rejected"
    PLUGIN_CLEANUP_FAILED = "plugin_cleanup_failed"
    PLUGIN_OPERATION_CONFLICT = "plugin_operation_conflict"
    UNSUPPORTED_SCOPE = "unsupported_scope"
    PLUGIN_LIMIT_REACHED = "plugin_limit_reached"
    PLUGIN_DEPENDENCY_UNAVAILABLE = "plugin_dependency_unavailable"
    PLUGIN_DEPENDENCY_CYCLE = "plugin_dependency_cycle"
    PLUGIN_DEPENDENCY_CONFLICT = "plugin_dependency_conflict"


@dataclass(frozen=True)
class DynamicPluginPolicy:
    """Explicit host policy for dynamic plugin management in one profile."""

    enabled: bool = False
    allowed_kinds: frozenset[str] = frozenset()
    allowed_capabilities: frozenset[str] = frozenset()
    allowed_actions: frozenset[str] = frozenset()
    allowed_scopes: frozenset[str] = frozenset()
    max_active_plugins: int = 0

    def allows(self, *, kind: str, capabilities: set[str], action: str, scope: str) -> bool:
        """Return whether the policy admits one host-validated operation."""
        if not self.enabled or kind not in self.allowed_kinds or scope not in self.allowed_scopes:
            return False
        if action not in self.allowed_actions:
            return False
        return capabilities.issubset(self.allowed_capabilities)


class PluginContractError(ValueError):
    """A safe, machine-readable validation failure."""

    def __init__(
        self,
        code: PluginErrorCode,
        message: str,
        *,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


class PluginOperationResult:
    """Stable result shape for future manager operations.

    H1 exposes this shape without implementing mutating operations.  Messages
    are safe summaries; callers should use ``details`` for structured context,
    never concatenate exception text containing credentials or local paths.
    """

    __slots__ = (
        "ok",
        "code",
        "message",
        "plugin_id",
        "version",
        "operation_id",
        "details",
    )

    def __init__(
        self,
        *,
        ok: bool,
        message: str,
        code: PluginErrorCode | None = None,
        plugin_id: str | None = None,
        version: str | None = None,
        operation_id: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> None:
        if ok and code is not None:
            raise ValueError("successful plugin operations cannot have an error code")
        if not ok and code is None:
            raise ValueError("failed plugin operations require an error code")
        self.ok = ok
        self.code = code
        self.message = message
        self.plugin_id = plugin_id
        self.version = version
        self.operation_id = operation_id
        self.details = tuple(sorted((details or {}).items()))

    @classmethod
    def success(
        cls,
        message: str,
        *,
        plugin_id: str | None = None,
        version: str | None = None,
        operation_id: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> PluginOperationResult:
        return cls(
            ok=True,
            message=message,
            plugin_id=plugin_id,
            version=version,
            operation_id=operation_id,
            details=details,
        )

    @classmethod
    def failure(
        cls,
        code: PluginErrorCode,
        message: str,
        *,
        plugin_id: str | None = None,
        version: str | None = None,
        operation_id: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> PluginOperationResult:
        return cls(
            ok=False,
            code=code,
            message=message,
            plugin_id=plugin_id,
            version=version,
            operation_id=operation_id,
            details=details,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible result without exception or path details."""
        result: dict[str, Any] = {
            "ok": self.ok,
            "message": self.message,
        }
        if self.code is not None:
            result["code"] = self.code.value
        for name in ("plugin_id", "version", "operation_id"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.details:
            result["details"] = dict(self.details)
        return result

    @property
    def details_dict(self) -> dict[str, str]:
        """Return operation details as a fresh mapping for command adapters."""
        return dict(self.details)


_MANIFEST_FIELDS = frozenset(
    {
        "plugin_id",
        "version",
        "content_hash",
        "kind",
        "api_version",
        "scope",
        "entry",
        "requires",
        "provides",
        "requested_capabilities",
        "execution_mode",
    }
)
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_API_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
_INTERFACE_ID = re.compile(
    r"^[a-z][a-z0-9._-]{0,31}:[A-Za-z0-9][A-Za-z0-9._/-]{0,63}"
    r"@[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$"
)


def _required_string(data: Mapping[str, Any], name: str, code: PluginErrorCode) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise PluginContractError(code, f"manifest field '{name}' must be a non-empty string", field=name)
    return value


def _string_list(data: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise PluginContractError(
            PluginErrorCode.INVALID_LIST_FIELD,
            f"manifest field '{name}' must be a list of strings",
            field=name,
        )
    items = tuple(cast(Sequence[Any], value))
    if any(not isinstance(item, str) or not item for item in items):
        raise PluginContractError(
            PluginErrorCode.INVALID_LIST_FIELD,
            f"manifest field '{name}' must contain non-empty strings",
            field=name,
        )
    if len(set(items)) != len(items):
        raise PluginContractError(
            PluginErrorCode.INVALID_LIST_FIELD,
            f"manifest field '{name}' must not contain duplicates",
            field=name,
        )
    return cast(tuple[str, ...], items)


def _interface_list(
    data: Mapping[str, Any],
    name: str,
    *,
    allow_legacy_tool_ids: bool = False,
) -> tuple[str, ...]:
    """Validate H4 interfaces while retaining H1's legacy tool IDs."""
    items = _string_list(data, name)
    invalid = next(
        (
            item
            for item in items
            if not (
                allow_legacy_tool_ids
                and item.startswith("tool:")
            )
            and _INTERFACE_ID.fullmatch(item) is None
        ),
        None,
    )
    if invalid is not None:
        raise PluginContractError(
            PluginErrorCode.INVALID_LIST_FIELD,
            f"manifest field '{name}' must use versioned interface IDs",
            field=name,
        )
    return items


def _validate_relative_entry(entry: str) -> None:
    path = Path(entry)
    if (
        any(ord(char) < 32 or ord(char) == 127 for char in entry)
        or
        path.is_absolute()
        or entry.startswith(("/", "\\"))
        or "://" in entry
        or any(part == ".." for part in path.parts)
        or "\\" in entry
    ):
        raise PluginContractError(
            PluginErrorCode.INVALID_ENTRY,
            "manifest entry must be a relative forward-slash path inside the artifact",
            field="entry",
        )


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """The immutable identity and declared capabilities of one plugin version."""

    plugin_id: str
    version: str
    content_hash: str
    kind: PluginKind
    api_version: str
    scope: PluginScope
    entry: str
    requires: tuple[str, ...]
    provides: tuple[str, ...]
    requested_capabilities: tuple[str, ...]
    execution_mode: PluginExecutionMode

    @classmethod
    def from_mapping(cls, raw: Any) -> PluginManifest:
        """Parse exactly the H1 manifest fields and reject unknown input."""
        if not isinstance(raw, Mapping):
            raise PluginContractError(
                PluginErrorCode.INVALID_MANIFEST,
                "plugin manifest must be an object",
            )
        data = cast(Mapping[str, Any], raw)
        unknown = sorted(set(data) - _MANIFEST_FIELDS)
        if unknown:
            raise PluginContractError(
                PluginErrorCode.UNKNOWN_MANIFEST_FIELD,
                f"unknown manifest field: {unknown[0]}",
                field=unknown[0],
            )

        plugin_id = _required_string(data, "plugin_id", PluginErrorCode.INVALID_PLUGIN_ID)
        if not _PLUGIN_ID.fullmatch(plugin_id):
            raise PluginContractError(
                PluginErrorCode.INVALID_PLUGIN_ID,
                "plugin_id must start with a letter and contain at most 64 safe characters",
                field="plugin_id",
            )
        version = _required_string(data, "version", PluginErrorCode.INVALID_VERSION)
        if not _VERSION.fullmatch(version):
            raise PluginContractError(
                PluginErrorCode.INVALID_VERSION,
                "version contains unsupported characters",
                field="version",
            )
        content_hash = _required_string(
            data,
            "content_hash",
            PluginErrorCode.INVALID_CONTENT_HASH,
        ).lower()
        if not _CONTENT_HASH.fullmatch(content_hash):
            raise PluginContractError(
                PluginErrorCode.INVALID_CONTENT_HASH,
                "content_hash must be a 64-character lowercase hexadecimal digest",
                field="content_hash",
            )
        kind_value = _required_string(data, "kind", PluginErrorCode.INVALID_KIND)
        try:
            kind = PluginKind(kind_value)
        except ValueError as exc:
            raise PluginContractError(
                PluginErrorCode.INVALID_KIND,
                f"unsupported plugin kind: {kind_value}",
                field="kind",
            ) from exc
        api_version = _required_string(data, "api_version", PluginErrorCode.INVALID_MANIFEST)
        if not _API_VERSION.fullmatch(api_version):
            raise PluginContractError(
                PluginErrorCode.INVALID_MANIFEST,
                "api_version contains unsupported characters",
                field="api_version",
            )
        scope_value = _required_string(data, "scope", PluginErrorCode.INVALID_SCOPE)
        try:
            scope = PluginScope(scope_value)
        except ValueError as exc:
            raise PluginContractError(
                PluginErrorCode.INVALID_SCOPE,
                f"unsupported plugin scope: {scope_value}",
                field="scope",
            ) from exc
        entry = _required_string(data, "entry", PluginErrorCode.INVALID_ENTRY)
        _validate_relative_entry(entry)
        execution_value = _required_string(
            data,
            "execution_mode",
            PluginErrorCode.INVALID_MANIFEST,
        )
        try:
            execution_mode = PluginExecutionMode(execution_value)
        except ValueError as exc:
            raise PluginContractError(
                PluginErrorCode.INVALID_MANIFEST,
                f"unsupported execution mode: {execution_value}",
                field="execution_mode",
            ) from exc

        return cls(
            plugin_id=plugin_id,
            version=version,
            content_hash=content_hash,
            kind=kind,
            api_version=api_version,
            scope=scope,
            entry=entry,
            requires=_interface_list(data, "requires"),
            provides=_interface_list(data, "provides", allow_legacy_tool_ids=True),
            requested_capabilities=_string_list(data, "requested_capabilities"),
            execution_mode=execution_mode,
        )

    def resolve_entry(self, artifact_root: str | Path) -> Path:
        """Resolve an entry only when it stays inside its version artifact."""
        root = Path(artifact_root).resolve()
        candidate = (root / self.entry).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise PluginContractError(
                PluginErrorCode.ENTRY_OUTSIDE_ARTIFACT,
                "manifest entry resolves outside its artifact",
                field="entry",
            ) from exc
        return candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "kind": self.kind.value,
            "api_version": self.api_version,
            "scope": self.scope.value,
            "entry": self.entry,
            "requires": list(self.requires),
            "provides": list(self.provides),
            "requested_capabilities": list(self.requested_capabilities),
            "execution_mode": self.execution_mode.value,
        }
