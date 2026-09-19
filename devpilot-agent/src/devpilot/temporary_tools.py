"""Contracts for model-authored, session-scoped temporary tools.

This module deliberately contains no code loader and no execution side effect.
T1 only defines the immutable records and lifecycle guard that later phases use
to connect candidate validation to ``DynamicPluginRegistry``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_MANIFEST_FIELDS = frozenset(
    {
        "tool_id",
        "version",
        "session_id",
        "run_id",
        "project_id",
        "name",
        "description",
        "parameter_schema",
        "output_schema",
        "entry_module",
        "entry_class",
        "source_hash",
        "declared_capabilities",
        "created_at",
        "expires_at",
        "scope",
    }
)


class TemporaryToolState(StrEnum):
    """Persisted states for one temporary tool candidate."""

    GAP_DETECTED = "gap_detected"
    GENERATED = "generated"
    VALIDATING = "validating"
    VALIDATED = "validated"
    REJECTED = "rejected"
    ACTIVE = "active"
    RETIRING = "retiring"
    DISPOSED = "disposed"
    PERSISTENCE_REQUESTED = "persistence_requested"
    PERSISTED = "persisted"


_TRANSITIONS: dict[TemporaryToolState, frozenset[TemporaryToolState]] = {
    TemporaryToolState.GAP_DETECTED: frozenset({TemporaryToolState.GENERATED}),
    TemporaryToolState.GENERATED: frozenset(
        {TemporaryToolState.VALIDATING, TemporaryToolState.REJECTED}
    ),
    TemporaryToolState.VALIDATING: frozenset(
        {TemporaryToolState.VALIDATED, TemporaryToolState.REJECTED}
    ),
    TemporaryToolState.VALIDATED: frozenset({TemporaryToolState.ACTIVE}),
    TemporaryToolState.ACTIVE: frozenset(
        {TemporaryToolState.RETIRING, TemporaryToolState.PERSISTENCE_REQUESTED}
    ),
    TemporaryToolState.RETIRING: frozenset(
        {TemporaryToolState.DISPOSED, TemporaryToolState.REJECTED}
    ),
    TemporaryToolState.PERSISTENCE_REQUESTED: frozenset(
        {TemporaryToolState.PERSISTED, TemporaryToolState.DISPOSED}
    ),
    TemporaryToolState.DISPOSED: frozenset(),
    TemporaryToolState.PERSISTED: frozenset(),
    TemporaryToolState.REJECTED: frozenset(),
}


class TemporaryToolContractError(ValueError):
    """Raised when a model-authored temporary-tool record is invalid."""


def _required_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TemporaryToolContractError(f"{field_name} must be a safe lowercase identifier")
    return value


def _required_text(value: object, field_name: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise TemporaryToolContractError(f"{field_name} must be non-empty text")
    return value


def _required_binding(value: object, field_name: str, *, max_length: int = 256) -> str:
    """Validate opaque session/run bindings without imposing identifier syntax."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or any(ord(char) < 0x20 for char in value)
    ):
        raise TemporaryToolContractError(f"{field_name} must be non-empty binding text")
    return value


def _schema(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TemporaryToolContractError(f"{field_name} must be an object")
    # Keep this contract structural in T1. Full JSON-Schema validation belongs
    # to T3, but mutable caller-owned mappings must not enter the record.
    return dict(value)


def source_sha256(source: str) -> str:
    """Return the canonical UTF-8 SHA-256 used by the temporary manifest."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolCapabilityGap:
    """A session-local explanation of why existing tools are insufficient."""

    session_id: str
    run_id: str
    project_id: str
    reason: str
    requested_capability: str
    existing_tools: tuple[str, ...] = ()
    suggested_tool_id: str | None = None

    def __post_init__(self) -> None:
        _required_id(self.session_id, "session_id")
        _required_id(self.run_id, "run_id")
        _required_id(self.project_id, "project_id")
        _required_text(self.reason, "reason")
        _required_text(self.requested_capability, "requested_capability")
        if any(_ID.fullmatch(name) is None for name in self.existing_tools):
            raise TemporaryToolContractError("existing_tools contains an invalid tool name")
        if self.suggested_tool_id is not None:
            _required_id(self.suggested_tool_id, "suggested_tool_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "reason": self.reason,
            "requested_capability": self.requested_capability,
            "existing_tools": list(self.existing_tools),
            "suggested_tool_id": self.suggested_tool_id,
        }


@dataclass(frozen=True, slots=True)
class TemporaryToolManifest:
    """Immutable identity and loading contract for one temporary Tool."""

    tool_id: str
    version: str
    session_id: str
    run_id: str
    project_id: str
    name: str
    description: str
    parameter_schema: dict[str, Any]
    output_schema: dict[str, Any]
    entry_module: str
    entry_class: str
    source_hash: str
    declared_capabilities: tuple[str, ...]
    created_at: int
    expires_at: int
    scope: str = "session"

    def __post_init__(self) -> None:
        _required_id(self.tool_id, "tool_id")
        for field_name in ("session_id", "run_id", "project_id"):
            _required_binding(getattr(self, field_name), field_name)
        _required_text(self.version, "version", max_length=128)
        _required_text(self.name, "name", max_length=128)
        _required_text(self.description, "description")
        if not _SHA256.fullmatch(self.source_hash):
            raise TemporaryToolContractError("source_hash must be a lowercase SHA-256")
        if self.scope != "session":
            raise TemporaryToolContractError("temporary tools must be session-scoped")
        if not self.entry_module or "\\" in self.entry_module or ":" in self.entry_module:
            raise TemporaryToolContractError("entry_module must use a relative POSIX path")
        entry_path = PurePosixPath(self.entry_module)
        if entry_path.is_absolute() or ".." in entry_path.parts:
            raise TemporaryToolContractError("entry_module must stay inside the artifact")
        if _SYMBOL.fullmatch(self.entry_class) is None:
            raise TemporaryToolContractError("entry_class must be a Python class name")
        if self.created_at < 0 or self.expires_at <= self.created_at:
            raise TemporaryToolContractError("temporary tool expiry must be after creation")
        if any(not isinstance(item, str) or not item for item in self.declared_capabilities):
            raise TemporaryToolContractError("declared_capabilities must contain text values")

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "version": self.version,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "parameter_schema": dict(self.parameter_schema),
            "output_schema": dict(self.output_schema),
            "entry_module": self.entry_module,
            "entry_class": self.entry_class,
            "source_hash": self.source_hash,
            "declared_capabilities": list(self.declared_capabilities),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> TemporaryToolManifest:
        unknown = set(raw) - _ALLOWED_MANIFEST_FIELDS
        if unknown:
            raise TemporaryToolContractError(
                "unknown temporary manifest fields: " + ",".join(sorted(unknown))
            )
        capabilities = raw.get("declared_capabilities", ())
        if not isinstance(capabilities, (list, tuple)):
            raise TemporaryToolContractError("declared_capabilities must be a list")
        created_at = raw.get("created_at")
        expires_at = raw.get("expires_at")
        if not isinstance(created_at, int) or isinstance(created_at, bool):
            raise TemporaryToolContractError("created_at must be an integer")
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            raise TemporaryToolContractError("expires_at must be an integer")
        return cls(
            tool_id=_required_id(raw.get("tool_id"), "tool_id"),
            version=_required_text(raw.get("version"), "version", max_length=128),
            session_id=_required_binding(raw.get("session_id"), "session_id"),
            run_id=_required_binding(raw.get("run_id"), "run_id"),
            project_id=_required_binding(raw.get("project_id"), "project_id"),
            name=_required_text(raw.get("name"), "name", max_length=128),
            description=_required_text(raw.get("description"), "description"),
            parameter_schema=_schema(raw.get("parameter_schema"), "parameter_schema"),
            output_schema=_schema(raw.get("output_schema"), "output_schema"),
            entry_module=_required_text(raw.get("entry_module"), "entry_module", max_length=256),
            entry_class=_required_text(raw.get("entry_class"), "entry_class", max_length=128),
            source_hash=_required_text(raw.get("source_hash"), "source_hash", max_length=64),
            declared_capabilities=tuple(capabilities),
            created_at=created_at,
            expires_at=expires_at,
            scope=(
                raw.get("scope", "session")
                if isinstance(raw.get("scope", "session"), str)
                else ""
            ),
        )


@dataclass(slots=True)
class TemporaryToolLifecycle:
    """Small transition guard shared by persistence and the future loader."""

    state: TemporaryToolState
    history: list[TemporaryToolState] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append(self.state)

    def transition(self, target: TemporaryToolState) -> None:
        if target not in _TRANSITIONS[self.state]:
            raise TemporaryToolContractError(
                f"invalid temporary tool transition: {self.state.value} -> {target.value}"
            )
        self.state = target
        self.history.append(target)

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "history": [item.value for item in self.history],
        }


def temporary_tools_allowed(access_mode: str, *, source_channel: str | None) -> bool:
    """Return whether model-authored temporary tools may be created for a turn."""
    return access_mode == "full" and source_channel == "websocket"
