"""Read-only H1 catalog and scope checks for managed plugin records."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from nanobot.agent.plugins.contracts import (
    PluginCandidateStatus,
    PluginContractError,
    PluginErrorCode,
    PluginExecutionMode,
    PluginLifecycle,
    PluginManifest,
    PluginScope,
    PluginSource,
)


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """Metadata visible to the future manager; it does not hold live objects."""

    manifest: PluginManifest
    source: PluginSource
    session_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    lifecycle: PluginLifecycle = PluginLifecycle.INACTIVE
    candidate_status: PluginCandidateStatus | None = None
    online_lifecycle: bool = False

    def __post_init__(self) -> None:
        if self.manifest.scope is PluginScope.SESSION:
            if not self.session_id or self.project_id is not None:
                raise PluginContractError(
                    PluginErrorCode.SCOPE_MISMATCH,
                    "session plugins require only a session_id",
                )
        elif self.manifest.scope is PluginScope.PROJECT:
            if not self.project_id or self.session_id is not None:
                raise PluginContractError(
                    PluginErrorCode.SCOPE_MISMATCH,
                    "project plugins require only a project_id",
                )
        elif self.session_id is not None or self.project_id is not None:
            raise PluginContractError(
                PluginErrorCode.SCOPE_MISMATCH,
                "global plugins cannot have a session or project binding",
            )
        if self.source is PluginSource.MODEL_CANDIDATE:
            if self.candidate_status is None:
                raise PluginContractError(
                    PluginErrorCode.INVALID_CANDIDATE_STATE,
                    "model candidates require an independent candidate status",
                )
            if self.lifecycle is PluginLifecycle.ACTIVE:
                raise PluginContractError(
                    PluginErrorCode.MODEL_CANDIDATE_NOT_ACTIVE,
                    "model candidates cannot be active in the H1 catalog",
                )
            if self.manifest.execution_mode is not PluginExecutionMode.FC_PROXY:
                raise PluginContractError(
                    PluginErrorCode.MODEL_CANDIDATE_NOT_FC_PROXY,
                    "model candidates must use the FC proxy execution mode",
                )
        elif self.source is PluginSource.TEMPORARY:
            if not self.run_id:
                raise PluginContractError(
                    PluginErrorCode.SCOPE_MISMATCH,
                    "temporary tools require a Run binding",
                )
            if self.manifest.scope is not PluginScope.SESSION:
                raise PluginContractError(
                    PluginErrorCode.UNSUPPORTED_SCOPE,
                    "temporary tools must be session-scoped",
                )
        elif self.run_id is not None:
            raise PluginContractError(
                PluginErrorCode.SCOPE_MISMATCH,
                "only temporary tools may carry a Run binding",
            )
        elif self.candidate_status is not None:
            raise PluginContractError(
                PluginErrorCode.INVALID_CANDIDATE_STATE,
                "only model candidates may carry a candidate status",
            )


class PluginCatalog:
    """An isolated catalog for one session/project visibility scope.

    H1 intentionally keeps this in memory.  It is a contract seam and a
    compatibility probe, not a second persistence store; H2+ will connect the
    manager to the existing storage layer.
    """

    def __init__(
        self,
        *,
        session_id: str,
        reserved_capabilities: Collection[str],
        project_id: str | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self._session_id = session_id
        self._project_id = project_id
        self._reserved_capabilities = frozenset(reserved_capabilities)
        self._records: dict[tuple[str, str], PluginRecord] = {}

    @classmethod
    def from_registry(
        cls,
        *,
        session_id: str,
        registry: ToolNameSource,
        project_id: str | None = None,
        reserved_capabilities: Iterable[str] = (),
    ) -> PluginCatalog:
        """Build a catalog using the live registry as the core-name authority."""
        tool_names = frozenset(registry.tool_names)
        reserved = set(reserved_capabilities)
        reserved.update(tool_names)
        reserved.update(f"tool:{name}" for name in tool_names)
        return cls(
            session_id=session_id,
            project_id=project_id,
            reserved_capabilities=reserved,
        )

    def register(self, record: PluginRecord) -> None:
        """Register metadata after checking scope and stable-name ownership."""
        self._check_scope(record)
        if record.manifest.scope is PluginScope.GLOBAL and record.source is not PluginSource.DEVELOPER:
            raise PluginContractError(
                code=PluginErrorCode.GLOBAL_SOURCE_REJECTED,
                message="global plugins require an explicit developer source",
            )
        conflicts = self._reserved_capabilities.intersection(record.manifest.provides)
        if conflicts:
            conflict = sorted(conflicts)[0]
            raise PluginContractError(
                code=PluginErrorCode.RESERVED_CAPABILITY,
                message=f"plugin capability conflicts with a reserved name: {conflict}",
                field="provides",
            )
        key = (record.manifest.plugin_id, record.manifest.version)
        if key in self._records:
            raise PluginContractError(
                code=PluginErrorCode.DUPLICATE_VERSION,
                message="plugin version is already registered in this scope",
            )
        self._records[key] = record

    def list(self) -> tuple[PluginRecord, ...]:
        """Return stable records for this catalog only."""
        return tuple(self._records[key] for key in sorted(self._records))

    def list_view(self) -> tuple[dict[str, Any], ...]:
        """Return the safe, read-only H1 list representation."""
        return tuple(self._record_view(record) for record in self.list())

    def status(self, plugin_id: str, version: str) -> dict[str, Any] | None:
        """Return one record's safe status, or ``None`` when not visible here."""
        key = (plugin_id, version)
        record = self._records.get(key)
        if record is None:
            return None
        return self._record_view(record)

    def _check_scope(self, record: PluginRecord) -> None:
        manifest = record.manifest
        if manifest.scope is PluginScope.SESSION and record.session_id != self._session_id:
            raise PluginContractError(
                code=PluginErrorCode.SCOPE_MISMATCH,
                message="plugin session scope does not match the current session",
            )
        if manifest.scope is PluginScope.PROJECT and record.project_id != self._project_id:
            raise PluginContractError(
                code=PluginErrorCode.SCOPE_MISMATCH,
                message="plugin project scope does not match the current project",
            )
        if manifest.scope is PluginScope.PROJECT and self._project_id is None:
            raise PluginContractError(
                code=PluginErrorCode.SCOPE_MISMATCH,
                message="project-scoped plugin is not visible without a project",
            )

    @staticmethod
    def _record_view(record: PluginRecord) -> dict[str, Any]:
        manifest = record.manifest
        return {
            "plugin_id": manifest.plugin_id,
            "version": manifest.version,
            "source": record.source.value,
            "scope": manifest.scope.value,
            "lifecycle": record.lifecycle.value,
            "online_lifecycle": record.online_lifecycle,
            "content_hash": manifest.content_hash,
            **(
                {"candidate_status": record.candidate_status.value}
                if record.candidate_status is not None
                else {}
            ),
        }


class ToolNameSource(Protocol):
    @property
    def tool_names(self) -> Collection[str]:
        """Return the host's currently registered core tool names."""
        ...
