"""Trusted, in-process plugin lifecycle for H3.

The manager is deliberately small: H2 admits only developer-owned, read-only
Tool implementations from already registered manifests.  It overlays the
existing registry for one session/project instead of replacing nanobot's
startup loader or creating a second agent loop.
"""

# The managed proxy intentionally mirrors the legacy Tool interface, including
# its class-level config_key and the manager's private lease seam.
# pyright: reportIncompatibleVariableOverride=false
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from nanobot.agent.plugins.catalog import PluginCatalog, PluginRecord
from nanobot.agent.plugins.context import ManagedPluginContext, SubscribeImpl
from nanobot.agent.plugins.contracts import (
    DynamicPluginPolicy,
    PluginContractError,
    PluginErrorCode,
    PluginExecutionMode,
    PluginKind,
    PluginLifecycle,
    PluginManifest,
    PluginOperationResult,
    PluginScope,
    PluginSource,
)
from nanobot.agent.plugins.store import PluginStateStore, SqlitePluginStateStore
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.agent.tools.registry import ToolRegistry

ScopeKey = tuple[PluginScope, str]
RegistrationKey = tuple[str, str, str, str]
RouteKey = tuple[ScopeKey, str, str, int]


@dataclass(frozen=True, slots=True)
class _RegisteredPlugin:
    record: PluginRecord
    artifact_root: Path
    registration_key: RegistrationKey


@dataclass(slots=True)
class _ActiveRoute:
    registration: _RegisteredPlugin
    generation: int
    tool: ManagedPluginTool
    context: ManagedPluginContext
    leases: int = 0
    lease_zero: asyncio.Event | None = None
    cleanup_started: bool = False

    @property
    def route_key(self) -> RouteKey:
        manifest = self.registration.record.manifest
        return (
            self._scope_key(),
            manifest.plugin_id,
            manifest.version,
            self.generation,
        )

    def _scope_key(self) -> ScopeKey:
        manifest = self.registration.record.manifest
        binding = self.registration.registration_key[3]
        return (manifest.scope, binding)


@dataclass(slots=True)
class _Operation:
    operation_id: str
    action: str
    plugin_id: str
    version: str | None
    state: str
    result: PluginOperationResult
    event: asyncio.Event
    scope_key: ScopeKey


class _PluginLoadError(RuntimeError):
    """Carry the managed context out of a failed staged import."""

    def __init__(self, cause: BaseException, context: ManagedPluginContext) -> None:
        super().__init__(str(cause))
        self.context = context


class PluginManager:
    """Own registered plugin versions and live scoped routing."""

    def __init__(
        self,
        base_registry: ToolRegistry,
        *,
        tool_context: ToolContext | None = None,
        drain_timeout_s: float = 30.0,
        subscribe_impl: SubscribeImpl | None = None,
        service_registry: dict[str, Any] | None = None,
        state_path: str | Path | None = None,
        state_store: PluginStateStore | None = None,
    ) -> None:
        self._base_registry = base_registry
        self._tool_context = tool_context
        self._drain_timeout_s = max(0.0, drain_timeout_s)
        self._subscribe_impl = subscribe_impl
        self._service_registry_seed = dict(service_registry or {})
        self._service_registries: dict[ScopeKey, dict[str, Any]] = {}
        self._state_store = state_store
        if self._state_store is None and state_path is not None:
            self._state_store = SqlitePluginStateStore(state_path)
        self._registered: dict[RegistrationKey, _RegisteredPlugin] = {}
        self._lifecycles: dict[RegistrationKey, PluginLifecycle] = {}
        self._routes: dict[ScopeKey, dict[str, _ActiveRoute]] = {}
        self._retired_routes: dict[RouteKey, _ActiveRoute] = {}
        self._authorized: set[ScopeKey] = set()
        self._operations: dict[str, _Operation] = {}
        self._retired_operations: dict[RouteKey, _Operation] = {}
        self._drain_tasks: set[asyncio.Task[Any]] = set()
        self._management_busy: set[ScopeKey] = set()
        self._restored_routes: dict[tuple[ScopeKey, str], tuple[_RegisteredPlugin, int]] = {}
        self._restored_retired: dict[RouteKey, dict[str, Any]] = {}
        self._waiting_routes: dict[ScopeKey, set[RegistrationKey]] = {}
        self._dependency_busy: set[ScopeKey] = set()
        self._dependency_switching: dict[ScopeKey, set[RouteKey]] = {}
        self._staging_operations: dict[str, ManagedPluginContext] = {}
        self._next_generation = 0
        self._restore_state()

    @staticmethod
    def project_id_for_path(path: str | Path) -> str:
        """Return the stable, non-sensitive project binding used by commands."""
        resolved = Path(path).resolve()
        return "local-" + hashlib.sha256(str(resolved).encode()).hexdigest()[:16]

    def _save_state(self) -> None:
        """Persist version/route/operation metadata atomically when configured."""
        if self._state_store is None:
            return
        retired_rows: list[dict[str, Any]] = []
        for ((scope, binding), plugin_id, version, generation), route in self._retired_routes.items():
            retired_rows.append(
                {
                    "scope": scope.value,
                    "binding": binding,
                    "plugin_id": plugin_id,
                    "version": version,
                    "generation": generation,
                    "leases": route.leases,
                    "cleanup_started": route.cleanup_started,
                    "operation_id": (
                        self._retired_operations[route.route_key].operation_id
                        if route.route_key in self._retired_operations
                        else None
                    ),
                }
            )
        for (scope_key, plugin_id, version, generation), metadata in self._restored_retired.items():
            if (scope_key, plugin_id, version, generation) in self._retired_routes:
                continue
            retired_rows.append(
                {
                    "scope": scope_key[0].value,
                    "binding": scope_key[1],
                    "plugin_id": plugin_id,
                    "version": version,
                    "generation": generation,
                    "leases": int(metadata.get("leases", 0)),
                    "cleanup_started": bool(metadata.get("cleanup_started", False)),
                    "operation_id": metadata.get("operation_id"),
                }
            )
        data = {
            "version": 1,
            "plugins": [
                {
                    "manifest": item.record.manifest.to_dict(),
                    "source": item.record.source.value,
                    "session_id": item.record.session_id,
                    "project_id": item.record.project_id,
                    "online_lifecycle": item.record.online_lifecycle,
                    "artifact_root": str(item.artifact_root),
                    "lifecycle": self._lifecycles[key].value,
                }
                for key, item in self._registered.items()
            ],
            "active_routes": [
                {
                    "scope": scope.value,
                    "binding": binding,
                    "plugin_id": plugin_id,
                    "version": route.registration.record.manifest.version,
                    "generation": route.generation,
                }
                for (scope, binding), routes in self._routes.items()
                for plugin_id, route in routes.items()
            ],
            "waiting_routes": [
                {
                    "scope": scope.value,
                    "binding": binding,
                    "plugin_id": registration.record.manifest.plugin_id,
                    "version": registration.record.manifest.version,
                }
                for (scope, binding), keys in self._waiting_routes.items()
                for key in keys
                if (registration := self._registered.get(key)) is not None
            ],
            "retired_routes": retired_rows,
            "operations": [self._operation_to_dict(operation) for operation in self._operations.values()],
        }
        self._state_store.save(data)

    @staticmethod
    def _operation_to_dict(operation: _Operation) -> dict[str, Any]:
        result = operation.result.to_dict()
        return {
            "operation_id": operation.operation_id,
            "action": operation.action,
            "plugin_id": operation.plugin_id,
            "version": operation.version,
            "state": operation.state,
            "result": result,
            "scope": operation.scope_key[0].value,
            "binding": operation.scope_key[1],
        }

    def _restore_state(self) -> None:
        """Restore metadata without importing plugin code during manager startup."""
        if self._state_store is None:
            return
        try:
            data = self._state_store.load()
            if data is None:
                return
            for raw in data.get("plugins", []):
                manifest = PluginManifest.from_mapping(raw["manifest"])
                record = PluginRecord(
                    manifest,
                    PluginSource(raw["source"]),
                    session_id=raw.get("session_id"),
                    project_id=raw.get("project_id"),
                    online_lifecycle=bool(raw.get("online_lifecycle", False)),
                )
                key = self._registration_key(record)
                root = Path(raw["artifact_root"]).resolve()
                self._registered[key] = _RegisteredPlugin(record, root, key)
                try:
                    entry = manifest.resolve_entry(root)
                    healthy = entry.is_file() and hashlib.sha256(entry.read_bytes()).hexdigest() == manifest.content_hash
                except (OSError, ValueError, PluginContractError):
                    healthy = False
                lifecycle = PluginLifecycle(raw.get("lifecycle", PluginLifecycle.INACTIVE.value))
                self._lifecycles[key] = lifecycle if healthy else PluginLifecycle.FAILED
            for raw in data.get("active_routes", []):
                scope = PluginScope(raw["scope"])
                scope_key = (scope, raw["binding"])
                plugin_id = raw["plugin_id"]
                registration = next(
                    (
                        item
                        for item in self._registered.values()
                        if item.record.manifest.plugin_id == plugin_id
                        and item.record.manifest.version == raw["version"]
                        and self._registration_key(item.record)[3] == raw["binding"]
                    ),
                    None,
                )
                if registration is not None and self._lifecycles[registration.registration_key] is not PluginLifecycle.FAILED:
                    generation = int(raw["generation"])
                    self._restored_routes[(scope_key, plugin_id)] = (registration, generation)
                    self._next_generation = max(self._next_generation, generation)
            for raw in data.get("retired_routes", []):
                route_key = (
                    (PluginScope(raw["scope"]), raw["binding"]),
                    raw["plugin_id"],
                    raw["version"],
                    int(raw["generation"]),
                )
                self._restored_retired[route_key] = {
                    "leases": int(raw.get("leases", 0)),
                    "cleanup_started": bool(raw.get("cleanup_started", False)),
                    "operation_id": raw.get("operation_id"),
                }
            for raw in data.get("waiting_routes", []):
                scope_key = (PluginScope(raw["scope"]), raw["binding"])
                registration = next(
                    (
                        item
                        for item in self._registered.values()
                        if item.record.manifest.plugin_id == raw["plugin_id"]
                        and item.record.manifest.version == raw["version"]
                        and self._registration_key(item.record)[3] == raw["binding"]
                    ),
                    None,
                )
                if registration is not None:
                    self._waiting_routes.setdefault(scope_key, set()).add(
                        registration.registration_key
                    )
            for raw in data.get("operations", []):
                scope_key = (PluginScope(raw["scope"]), raw["binding"])
                raw_result = raw.get("result", {})
                code_value = raw_result.get("code")
                code = PluginErrorCode(code_value) if code_value else None
                if code is None:
                    result = PluginOperationResult.success(
                        raw_result.get("message", "operation restored after restart"),
                        plugin_id=raw.get("plugin_id"),
                        version=raw.get("version"),
                        operation_id=raw["operation_id"],
                        details=raw_result.get("details"),
                    )
                else:
                    result = PluginOperationResult.failure(
                        code,
                        raw_result.get("message", "operation restored after restart"),
                        plugin_id=raw.get("plugin_id"),
                        version=raw.get("version"),
                        operation_id=raw["operation_id"],
                        details=raw_result.get("details"),
                    )
                operation = _Operation(
                    operation_id=raw["operation_id"],
                    action=raw["action"],
                    plugin_id=raw["plugin_id"],
                    version=raw.get("version"),
                    state=raw.get("state", "interrupted"),
                    result=result,
                    event=asyncio.Event(),
                    scope_key=scope_key,
                )
                operation.event.set()
                self._operations[operation.operation_id] = operation
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # Plugin state is recoverable metadata.  Keep the file intact and
            # start empty rather than risking an unsafe or partial activation.
            self._registered.clear()
            self._lifecycles.clear()
            self._restored_routes.clear()
            self._restored_retired.clear()
            self._waiting_routes.clear()
            self._dependency_busy.clear()
            self._dependency_switching.clear()
            self._operations.clear()
            self._staging_operations.clear()

    def register_version(
        self,
        record: PluginRecord,
        artifact_root: str | Path,
    ) -> PluginOperationResult:
        """Register a verified developer manifest without importing its code."""
        manifest = record.manifest
        if record.source is not PluginSource.DEVELOPER:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_SOURCE_UNTRUSTED,
                "only developer-owned plugin versions may enter the H2 registry",
                plugin_id=manifest.plugin_id,
                version=manifest.version,
            )
        if manifest.kind is not PluginKind.TOOL:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_KIND_UNSUPPORTED,
                "H2 only loads tool plugins",
                plugin_id=manifest.plugin_id,
                version=manifest.version,
            )
        if manifest.scope is PluginScope.GLOBAL:
            return PluginOperationResult.failure(
                PluginErrorCode.UNSUPPORTED_SCOPE,
                "H2 only loads session or project plugins",
                plugin_id=manifest.plugin_id,
                version=manifest.version,
            )
        registration_key = self._registration_key(record)
        if registration_key in self._registered:
            return PluginOperationResult.failure(
                PluginErrorCode.DUPLICATE_VERSION,
                "plugin version is already registered",
                plugin_id=manifest.plugin_id,
                version=manifest.version,
            )
        try:
            root = Path(artifact_root).resolve()
            entry = manifest.resolve_entry(root)
            if not entry.is_file():
                raise OSError("entry is not a file")
            digest = hashlib.sha256(entry.read_bytes()).hexdigest()
            if digest != manifest.content_hash:
                raise ValueError("entry content hash does not match the manifest")
            self._validate_h2_declaration(record)
            catalog = PluginCatalog.from_registry(
                session_id=record.session_id or "__project__",
                project_id=record.project_id,
                registry=self._base_registry,
            )
            catalog.register(record)
        except PluginContractError as exc:
            return PluginOperationResult.failure(
                exc.code,
                str(exc),
                plugin_id=manifest.plugin_id,
                version=manifest.version,
            )
        except (OSError, ValueError):
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_LOAD_FAILED,
                "plugin artifact failed registration checks",
                plugin_id=manifest.plugin_id,
                version=manifest.version,
            )

        registered = _RegisteredPlugin(record, root, registration_key)
        self._registered[registration_key] = registered
        self._lifecycles[registration_key] = PluginLifecycle.INACTIVE
        self._save_state()
        return PluginOperationResult.success(
            "plugin version registered",
            plugin_id=manifest.plugin_id,
            version=manifest.version,
            details={"state": PluginLifecycle.INACTIVE.value},
        )

    def authorize(self, session_id: str, project_id: str | None = None) -> None:
        """Record the explicit user authorization needed for dynamic routing."""
        if session_id:
            self._authorized.add((PluginScope.SESSION, session_id))
        if project_id:
            self._authorized.add((PluginScope.PROJECT, project_id))

    def is_authorized(self, session_id: str, project_id: str | None = None) -> bool:
        return (
            (PluginScope.SESSION, session_id) in self._authorized
            or (project_id is not None and (PluginScope.PROJECT, project_id) in self._authorized)
        )

    def list_view(
        self,
        session_id: str,
        *,
        project_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """List only versions visible to the requested session/project."""
        visible = [
            item
            for item in self._registered.values()
            if self._is_visible(item.record, session_id, project_id)
        ]
        return tuple(self._view(item) for item in sorted(visible, key=self._sort_key))

    def load(
        self,
        plugin_id: str,
        version: str,
        *,
        session_id: str,
        project_id: str | None = None,
        policy: DynamicPluginPolicy | None = None,
    ) -> PluginOperationResult:
        """Load one exact registered version into the current scope."""
        registration = self._find_visible(plugin_id, version, session_id, project_id)
        if registration is None:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_NOT_FOUND,
                "the requested registered plugin version is not visible here",
                plugin_id=plugin_id,
                version=version,
            )
        manifest = registration.record.manifest
        scope_key = self._scope_key(manifest.scope, session_id, project_id)
        if scope_key is None or scope_key not in self._authorized:
            return self._failure_not_authorized(plugin_id, version)
        if policy is not None and not self._policy_allows(policy, manifest, "load"):
            return self._failure_not_authorized(plugin_id, version)
        if scope_key in self._dependency_switching:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_DEPENDENCY_SWITCHING,
                "plugin loading is paused while this dependency composition switches",
                plugin_id=plugin_id,
                version=version,
                details={"state": PluginLifecycle.DRAINING.value},
            )
        self._restore_route_if_needed(scope_key, plugin_id)
        if (
            policy is not None
            and policy.max_active_plugins > 0
            and len(self._active_routes(session_id, project_id)) >= policy.max_active_plugins
        ):
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_LIMIT_REACHED,
                "the active dynamic plugin limit for this scope has been reached",
                plugin_id=plugin_id,
                version=version,
            )
        # Do not materialize an empty scope while dependency checks are still
        # pending. A rejected load must not leave observable route state.
        active = self._routes.get(scope_key, {}).get(plugin_id)
        if active is not None:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_ALREADY_ACTIVE,
                "a version of this plugin is already active in this scope",
                plugin_id=plugin_id,
                version=version,
            )
        cycle = self._dependency_cycle(scope_key, registration.registration_key)
        if cycle:
            self._lifecycles[registration.registration_key] = PluginLifecycle.FAILED
            self._save_state()
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_DEPENDENCY_CYCLE,
                "plugin dependency cycle rejected",
                plugin_id=plugin_id,
                version=version,
                details={"cycle": " -> ".join(cycle)},
            )
        missing = self._missing_dependencies(registration, scope_key)
        if missing:
            self._waiting_routes.setdefault(scope_key, set()).add(registration.registration_key)
            self._lifecycles[registration.registration_key] = PluginLifecycle.WAITING
            self._save_state()
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_DEPENDENCY_UNAVAILABLE,
                "plugin is waiting for required interfaces",
                plugin_id=plugin_id,
                version=version,
                details={
                    "state": PluginLifecycle.WAITING.value,
                    "missing": ",".join(missing),
                },
            )
        conflicts = self._provided_interface_conflicts(registration, scope_key)
        if conflicts:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_DEPENDENCY_CONFLICT,
                "another active plugin already provides an interface in this scope",
                plugin_id=plugin_id,
                version=version,
                details={"conflicts": ",".join(conflicts)},
            )
        plugin_context: ManagedPluginContext | None = None
        try:
            tool, plugin_context = self._load_tool(registration, scope_key=scope_key)
            plugin_context.activate()
        except Exception as exc:
            if isinstance(exc, _PluginLoadError):
                plugin_context = exc.context
            self._lifecycles[registration.registration_key] = PluginLifecycle.FAILED
            return self._staging_failure_operation(
                action="load",
                plugin_id=plugin_id,
                version=version,
                scope_key=scope_key,
                context=plugin_context,
                code=PluginErrorCode.PLUGIN_LOAD_FAILED,
                message="plugin initialization failed; the existing registry is unchanged",
                details={"route_commit": "not_committed"},
            )

        self._next_generation += 1
        route_key = (scope_key, plugin_id, version, self._next_generation)
        proxy = ManagedPluginTool(self, route_key, tool)
        route = _ActiveRoute(registration, self._next_generation, proxy, plugin_context)
        route.lease_zero = asyncio.Event()
        route.lease_zero.set()
        self._routes.setdefault(scope_key, {})[plugin_id] = route
        self._lifecycles[registration.registration_key] = PluginLifecycle.ACTIVE
        operation = self._new_operation(
            action="load",
            plugin_id=plugin_id,
            version=version,
            scope_key=scope_key,
            state=PluginLifecycle.ACTIVE.value,
            result=PluginOperationResult.success(
                "plugin loaded for the current scope",
                plugin_id=plugin_id,
                version=version,
                details={
                    "state": PluginLifecycle.ACTIVE.value,
                    "generation": str(route.generation),
                    "tool": tool.name,
                },
            ),
        )
        operation.event.set()
        self._save_state()
        self._activate_waiting(scope_key)
        return operation.result

    async def update(
        self,
        plugin_id: str,
        version: str,
        *,
        session_id: str,
        project_id: str | None = None,
        operation_id: str | None = None,
        expected_generation: int | None = None,
        policy: DynamicPluginPolicy | None = None,
    ) -> PluginOperationResult:
        """Stage and atomically switch to a registered version.

        The new module is imported under its immutable version namespace.  The
        old route is removed from new dispatch before its leases and owned
        resources are drained, so an in-flight call can finish without mixing
        parameters or implementation versions.
        """
        if operation_id is not None and (prior := self._operations.get(operation_id)) is not None:
            if not self._operation_visible(prior, session_id, project_id):
                return PluginOperationResult.failure(
                    PluginErrorCode.PLUGIN_NOT_FOUND,
                    "plugin operation was not found in this scope",
                    operation_id=operation_id,
                )
            return self._reuse_operation(
                operation_id,
                action="update",
                plugin_id=plugin_id,
                version=version,
                scope_key=prior.scope_key,
            ) or prior.result
        registration = self._find_visible(plugin_id, version, session_id, project_id)
        if registration is None:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_NOT_FOUND,
                "the requested registered plugin version is not visible here",
                plugin_id=plugin_id,
                version=version,
            )
        manifest = registration.record.manifest
        scope_key = self._scope_key(manifest.scope, session_id, project_id)
        if scope_key is None or scope_key not in self._authorized:
            return self._failure_not_authorized(plugin_id, version)
        if policy is not None and not self._policy_allows(policy, manifest, "update"):
            return self._failure_not_authorized(plugin_id, version)
        if operation_id is not None:
            existing = self._reuse_operation(
                operation_id,
                action="update",
                plugin_id=plugin_id,
                version=version,
                scope_key=scope_key,
            )
            if existing is not None:
                return existing
        self._restore_route_if_needed(scope_key, plugin_id)
        if scope_key in self._management_busy:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_OPERATION_CONFLICT,
                "another plugin management operation is already preparing this scope",
                plugin_id=plugin_id,
                version=version,
            )
        # Do not materialize an empty scope while dependency checks are still
        # pending.  A rejected load must not leave observable route state.
        active = self._routes.get(scope_key, {}).get(plugin_id)
        if active is None:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_NOT_ACTIVE,
                "update requires an active version in this scope",
                plugin_id=plugin_id,
                version=version,
            )
        if expected_generation is not None and active.generation != expected_generation:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_OPERATION_CONFLICT,
                "the active plugin generation changed before update",
                plugin_id=plugin_id,
                version=version,
                details={"expected_generation": str(expected_generation), "actual_generation": str(active.generation)},
            )
        cycle = self._dependency_cycle(scope_key, registration.registration_key)
        if cycle:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_DEPENDENCY_CYCLE,
                "plugin dependency cycle rejected",
                plugin_id=plugin_id,
                version=version,
                details={"cycle": " -> ".join(cycle)},
            )
        missing = self._missing_dependencies(registration, scope_key, exclude=active)
        if missing:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_DEPENDENCY_UNAVAILABLE,
                "updated plugin is waiting for required interfaces; existing route is unchanged",
                plugin_id=plugin_id,
                version=version,
                details={"state": PluginLifecycle.WAITING.value, "missing": ",".join(missing)},
            )
        conflicts = self._provided_interface_conflicts(registration, scope_key, exclude=active)
        if conflicts:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_DEPENDENCY_CONFLICT,
                "another active plugin already provides an interface in this scope",
                plugin_id=plugin_id,
                version=version,
                details={"conflicts": ",".join(conflicts)},
            )
        affected_dependents = self._active_dependents(scope_key, active)
        old_provides = set(active.registration.record.manifest.provides)
        new_provides = set(manifest.provides)
        missing_for_dependents = tuple(
            sorted(
                {
                    required
                    for dependent in affected_dependents[1:]
                    for required in dependent.registration.record.manifest.requires
                    if required in old_provides and required not in new_provides
                }
            )
        )
        if missing_for_dependents:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_DEPENDENCY_UNAVAILABLE,
                "provider update would break active dependent plugins; existing composition is unchanged",
                plugin_id=plugin_id,
                version=version,
                details={
                    "state": PluginLifecycle.ACTIVE.value,
                    "missing": ",".join(missing_for_dependents),
                },
            )
        if active.registration.registration_key == registration.registration_key:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_ALREADY_ACTIVE,
                "the requested plugin version is already active in this scope",
                plugin_id=plugin_id,
                version=version,
            )

        affected_routes = self._active_dependents(scope_key, active)
        switches_service = any(
            not interface_id.startswith("tool:")
            for interface_id in active.registration.record.manifest.provides
        )
        dependency_keys = {route.route_key for route in affected_routes}
        if switches_service:
            # Reserve the complete composition before the first await in
            # staging, so a concurrent load cannot join the old version.
            self._dependency_switching.setdefault(scope_key, set()).update(dependency_keys)
        self._management_busy.add(scope_key)
        try:
            # Let a concurrently scheduled update/stop observe the reservation
            # before staging can commit.  The manager is owned by nanobot's
            # single event loop, so this short yield makes the scope reservation
            # an explicit concurrency boundary without holding a global lock.
            await asyncio.sleep(0)
            self._lifecycles[registration.registration_key] = PluginLifecycle.PREPARING
            try:
                tool, plugin_context = self._load_tool(
                    registration,
                    scope_key=scope_key,
                    allow_service_replacement=True,
                    replaceable_service_ids=frozenset(
                        interface_id
                        for interface_id in active.registration.record.manifest.provides
                        if not interface_id.startswith("tool:")
                    ),
                )
            except _PluginLoadError as exc:
                self._lifecycles[registration.registration_key] = PluginLifecycle.FAILED
                return self._staging_failure_operation(
                    action="update",
                    plugin_id=plugin_id,
                    version=version,
                    scope_key=scope_key,
                    operation_id=operation_id,
                    context=exc.context,
                    code=PluginErrorCode.PLUGIN_UPDATE_FAILED,
                    message="new plugin version failed staging; the existing route is unchanged",
                    details={
                        "route_commit": "not_committed",
                        "old_version": active.registration.record.manifest.version,
                    },
                )

            if switches_service and not await self._wait_for_routes_idle(affected_routes):
                self._lifecycles[registration.registration_key] = PluginLifecycle.FAILED
                return self._staging_failure_operation(
                    action="update",
                    plugin_id=plugin_id,
                    version=version,
                    scope_key=scope_key,
                    operation_id=operation_id,
                    context=plugin_context,
                    code=PluginErrorCode.PLUGIN_STOP_TIMEOUT,
                    message="provider update was not committed while dependent calls were in flight",
                    details={
                        "route_commit": "not_committed",
                        "dependency_switch": "deferred",
                        "old_version": active.registration.record.manifest.version,
                    },
                )

            self._next_generation += 1
            new_route_key = (scope_key, plugin_id, version, self._next_generation)
            new_route = _ActiveRoute(
                registration,
                self._next_generation,
                ManagedPluginTool(self, new_route_key, tool),
                plugin_context,
            )
            new_route.lease_zero = asyncio.Event()
            new_route.lease_zero.set()
            self._routes[scope_key][plugin_id] = new_route
            try:
                plugin_context.activate()
            except Exception as activation_error:
                self._routes[scope_key][plugin_id] = active
                self._lifecycles[registration.registration_key] = PluginLifecycle.FAILED
                cleanup_error: Exception | None = None
                try:
                    await self._cleanup_staging_context(plugin_context)
                except Exception as exc:
                    cleanup_error = exc
                result_code = (
                    PluginErrorCode.PLUGIN_CLEANUP_FAILED
                    if cleanup_error is not None
                    else PluginErrorCode.PLUGIN_UPDATE_FAILED
                )
                operation = self._new_operation(
                    action="update",
                    plugin_id=plugin_id,
                    version=version,
                    scope_key=scope_key,
                    state="failed",
                    operation_id=operation_id,
                    result=PluginOperationResult.failure(
                        result_code,
                        (
                            "plugin activation was rolled back and staging cleanup failed"
                            if cleanup_error is not None
                            else "new plugin version failed activation; the existing route is unchanged"
                        ),
                        plugin_id=plugin_id,
                        version=version,
                        details={
                            "route_commit": "rolled_back",
                            "old_version": active.registration.record.manifest.version,
                            "staging_cleanup": "failed" if cleanup_error is not None else "complete",
                            "activation_error": type(activation_error).__name__,
                            "original_code": PluginErrorCode.PLUGIN_UPDATE_FAILED.value,
                        },
                    ),
                )
                if cleanup_error is not None:
                    self._staging_operations[operation.operation_id] = plugin_context
                operation.event.set()
                self._save_state()
                return operation.result

            self._retire_route(scope_key, active)
            self._lifecycles[active.registration.registration_key] = PluginLifecycle.DRAINING
            self._lifecycles[registration.registration_key] = PluginLifecycle.ACTIVE
            operation = self._new_operation(
                action="update",
                plugin_id=plugin_id,
                version=version,
                scope_key=scope_key,
                state=PluginLifecycle.DRAINING.value,
                operation_id=operation_id,
                persist=False,
                result=PluginOperationResult.success(
                    "plugin route updated; the previous version is draining",
                    plugin_id=plugin_id,
                    version=version,
                    details={
                        "state": PluginLifecycle.DRAINING.value,
                        "route_commit": "committed",
                        "old_version": active.registration.record.manifest.version,
                        "old_generation": str(active.generation),
                        "generation": str(new_route.generation),
                        "old_cleanup": "pending",
                        "dependency_switch": "atomic",
                        "dependency_lease": "group",
                        "affected_dependents": str(max(0, len(affected_routes) - 1)),
                    },
                ),
            )
            operation.event.clear()
            self._retired_operations[active.route_key] = operation
            self._start_retirement(operation, active)
            self._save_state()
            self._activate_waiting(scope_key)
            return operation.result
        finally:
            blocked = self._dependency_switching.get(scope_key)
            if blocked is not None:
                blocked.difference_update(dependency_keys)
                if not blocked:
                    self._dependency_switching.pop(scope_key, None)
            self._management_busy.discard(scope_key)

    def remove(
        self,
        plugin_id: str,
        version: str,
        *,
        session_id: str,
        project_id: str | None = None,
        operation_id: str | None = None,
        expected_generation: int | None = None,
        policy: DynamicPluginPolicy | None = None,
    ) -> PluginOperationResult:
        """Remove an inactive metadata record without deleting its artifact."""
        if operation_id is not None and (prior := self._operations.get(operation_id)) is not None:
            if not self._operation_visible(prior, session_id, project_id):
                return PluginOperationResult.failure(
                    PluginErrorCode.PLUGIN_NOT_FOUND,
                    "plugin operation was not found in this scope",
                    operation_id=operation_id,
                )
            return self._reuse_operation(
                operation_id,
                action="remove",
                plugin_id=plugin_id,
                version=version,
                scope_key=prior.scope_key,
            ) or prior.result
        registration = self._find_visible(plugin_id, version, session_id, project_id)
        if registration is None:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_NOT_FOUND,
                "the requested registered plugin version is not visible here",
                plugin_id=plugin_id,
                version=version,
            )
        manifest = registration.record.manifest
        scope_key = self._scope_key(manifest.scope, session_id, project_id)
        if scope_key is None or scope_key not in self._authorized:
            return self._failure_not_authorized(plugin_id, version)
        if policy is not None and not self._policy_allows(policy, manifest, "remove"):
            return self._failure_not_authorized(plugin_id, version)
        self._restore_route_if_needed(scope_key, plugin_id)
        active = self._routes.get(scope_key, {}).get(plugin_id)
        if active is not None and active.registration.registration_key == registration.registration_key:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_REMOVE_REJECTED,
                "stop the active plugin version before removing its metadata",
                plugin_id=plugin_id,
                version=version,
                details={"reason": "active_route"},
            )
        if (
            expected_generation is not None
            and active is not None
            and active.generation != expected_generation
        ):
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_OPERATION_CONFLICT,
                "the active plugin generation changed before remove",
                plugin_id=plugin_id,
                version=version,
                details={
                    "expected_generation": str(expected_generation),
                    "actual_generation": str(active.generation),
                },
            )
        if any(
            retired.registration.registration_key == registration.registration_key
            for retired in self._retired_routes.values()
        ) or any(
            route_key[0] == scope_key
            and route_key[1] == plugin_id
            and route_key[2] == version
            for route_key in self._restored_retired
        ):
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_REMOVE_REJECTED,
                "plugin version still has a draining or cleanup reference",
                plugin_id=plugin_id,
                version=version,
                details={"reason": "retired_reference"},
            )
        if scope_key in self._management_busy:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_OPERATION_CONFLICT,
                "another plugin management operation is already preparing this scope",
                plugin_id=plugin_id,
                version=version,
            )
        key = registration.registration_key
        self._registered.pop(key, None)
        self._lifecycles.pop(key, None)
        operation = self._new_operation(
            action="remove",
            plugin_id=plugin_id,
            version=version,
            scope_key=scope_key,
            state=PluginLifecycle.INACTIVE.value,
            operation_id=operation_id,
            result=PluginOperationResult.success(
                "plugin version removed; its artifact was retained",
                plugin_id=plugin_id,
                version=version,
                details={"artifact": "retained", "state": PluginLifecycle.INACTIVE.value},
            ),
        )
        operation.event.set()
        self._save_state()
        return operation.result

    async def retry_cleanup(
        self,
        operation_id: str,
        *,
        session_id: str,
        project_id: str | None = None,
    ) -> PluginOperationResult:
        """Retry only the owned resources left by a failed retirement."""
        operation = self._operations.get(operation_id)
        if operation is None or not self._operation_visible(operation, session_id, project_id):
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_NOT_FOUND,
                "plugin operation was not found in this scope",
                operation_id=operation_id,
            )
        staging = self._staging_operations.get(operation_id)
        if staging is not None:
            try:
                await staging.cleanup()
            except Exception:
                operation.state = "cleanup_failed"
                operation.result = PluginOperationResult.failure(
                    PluginErrorCode.PLUGIN_CLEANUP_FAILED,
                    "staging resources still need cleanup",
                    plugin_id=operation.plugin_id,
                    version=operation.version,
                    operation_id=operation_id,
                    details={
                        "route_commit": "rolled_back",
                        "staging_cleanup": "failed",
                        "original_code": (
                            operation.result.details_dict.get(
                                "original_code", PluginErrorCode.PLUGIN_UPDATE_FAILED.value
                            )
                        ),
                    },
                )
                operation.event.set()
                self._save_state()
                return operation.result
            self._staging_operations.pop(operation_id, None)
            previous_details = operation.result.details_dict
            raw_original_code = previous_details.get("original_code")
            try:
                original_code = PluginErrorCode(raw_original_code) if raw_original_code else None
            except ValueError:
                original_code = None
            original_code = original_code or PluginErrorCode.PLUGIN_UPDATE_FAILED
            original_message = previous_details.get("original_message", operation.result.message)
            operation.state = "failed"
            operation.result = PluginOperationResult.failure(
                original_code,
                f"{original_message}; staging cleanup is complete",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation_id,
                details={"route_commit": "rolled_back", "staging_cleanup": "complete"},
            )
            operation.event.set()
            self._save_state()
            return operation.result
        retired = next(
            (route for route, item in self._retired_operations.items() if item is operation),
            None,
        )
        if retired is None:
            restored = next(
                (
                    route_key
                    for route_key, metadata in self._restored_retired.items()
                    if metadata.get("operation_id") == operation_id
                ),
                None,
            )
            if restored is not None:
                metadata = self._restored_retired[restored]
                if int(metadata.get("leases", 0)) > 0:
                    return PluginOperationResult.failure(
                        PluginErrorCode.PLUGIN_CLEANUP_FAILED,
                        "the interrupted plugin call still has an unresolved lease",
                        plugin_id=operation.plugin_id,
                        version=operation.version,
                        operation_id=operation_id,
                        details={"recovery": "manual_lease_reconciliation_required"},
                    )
                self._restored_retired.pop(restored, None)
                for key, registration in self._registered.items():
                    manifest = registration.record.manifest
                    if (
                        manifest.plugin_id == operation.plugin_id
                        and manifest.version == operation.version
                        and self._registration_key(registration.record)[3] == restored[0][1]
                    ):
                        self._lifecycles[key] = PluginLifecycle.INACTIVE
                        break
                operation.state = PluginLifecycle.INACTIVE.value
                operation.result = PluginOperationResult.success(
                    "plugin cleanup reconciled after process restart",
                    plugin_id=operation.plugin_id,
                    version=operation.version,
                    operation_id=operation_id,
                    details={
                        "state": PluginLifecycle.INACTIVE.value,
                        "old_cleanup": "recovered_after_restart",
                        "route_commit": "committed",
                    },
                )
                operation.event.set()
                self._save_state()
                return operation.result
        if retired is None:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_CLEANUP_FAILED,
                "the operation has no retryable cleanup reference",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation_id,
            )
        route = self._retired_routes.get(retired)
        if route is None or route.leases:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_CLEANUP_FAILED,
                "plugin resources are still draining",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation_id,
            )
        operation.state = PluginLifecycle.DRAINING.value
        operation.result = PluginOperationResult.success(
            "plugin cleanup retry started",
            plugin_id=operation.plugin_id,
            version=operation.version,
            operation_id=operation.operation_id,
            details={"state": PluginLifecycle.DRAINING.value, "old_cleanup": "retrying"},
        )
        operation.event.clear()
        self._start_retirement(operation, route)
        self._save_state()
        return operation.result

    def stop(
        self,
        plugin_id: str,
        *,
        session_id: str,
        project_id: str | None = None,
        operation_id: str | None = None,
        expected_generation: int | None = None,
        policy: DynamicPluginPolicy | None = None,
    ) -> PluginOperationResult:
        """Retire a route immediately and drain only its existing leases."""
        if operation_id is not None and (prior := self._operations.get(operation_id)) is not None:
            if not self._operation_visible(prior, session_id, project_id):
                return PluginOperationResult.failure(
                    PluginErrorCode.PLUGIN_NOT_FOUND,
                    "plugin operation was not found in this scope",
                    operation_id=operation_id,
                )
            return self._reuse_operation(
                operation_id,
                action="stop",
                plugin_id=plugin_id,
                version=prior.version,
                scope_key=prior.scope_key,
            ) or prior.result
        route, scope_key = self._find_active(plugin_id, session_id, project_id)
        if route is None or scope_key is None:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_NOT_ACTIVE,
                "plugin is not active in this scope",
                plugin_id=plugin_id,
            )
        manifest = route.registration.record.manifest
        if scope_key not in self._authorized:
            return self._failure_not_authorized(plugin_id, manifest.version)
        if policy is not None and not self._policy_allows(policy, manifest, "stop"):
            return self._failure_not_authorized(plugin_id, manifest.version)
        if operation_id is not None:
            existing = self._reuse_operation(
                operation_id,
                action="stop",
                plugin_id=plugin_id,
                version=manifest.version,
                scope_key=scope_key,
            )
            if existing is not None:
                return existing
        if scope_key in self._management_busy:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_OPERATION_CONFLICT,
                "another plugin management operation is already preparing this scope",
                plugin_id=plugin_id,
                version=manifest.version,
            )
        if expected_generation is not None and route.generation != expected_generation:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_OPERATION_CONFLICT,
                "the active plugin generation changed before stop",
                plugin_id=plugin_id,
                version=manifest.version,
                details={"expected_generation": str(expected_generation), "actual_generation": str(route.generation)},
            )
        self._routes[scope_key].pop(plugin_id, None)
        self._retire_route(scope_key, route)
        self._suspend_dependents(scope_key, route)
        self._lifecycles[route.registration.registration_key] = PluginLifecycle.DRAINING
        operation = self._new_operation(
            action="stop",
            plugin_id=plugin_id,
            version=manifest.version,
            scope_key=scope_key,
            state=PluginLifecycle.DRAINING.value,
            operation_id=operation_id,
            persist=False,
            result=PluginOperationResult.success(
                "plugin route retired; existing calls are draining",
                plugin_id=plugin_id,
                version=manifest.version,
                details={
                    "state": PluginLifecycle.DRAINING.value,
                    "generation": str(route.generation),
                    "leases": str(route.leases),
                },
            ),
        )
        self._retired_operations[route.route_key] = operation
        self._start_retirement(operation, route)
        self._save_state()
        self._activate_waiting(scope_key)
        return operation.result

    async def wait_operation(
        self,
        operation_id: str,
        *,
        session_id: str,
        project_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for an H2 operation without blocking message consumption."""
        operation = self._operations.get(operation_id)
        if operation is None or not self._operation_visible(operation, session_id, project_id):
            return {"ok": False, "code": "operation_not_found", "operation_id": operation_id}
        try:
            if not operation.event.is_set():
                if timeout is None:
                    await operation.event.wait()
                else:
                    await asyncio.wait_for(operation.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self._operation_view(operation)

    def operation_status(
        self,
        operation_id: str,
        *,
        session_id: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        operation = self._operations.get(operation_id)
        if operation is None or not self._operation_visible(operation, session_id, project_id):
            return None
        return self._operation_view(operation)

    @staticmethod
    def _operation_visible(
        operation: _Operation,
        session_id: str,
        project_id: str | None,
    ) -> bool:
        scope, binding = operation.scope_key
        return (scope is PluginScope.SESSION and binding == session_id) or (
            scope is PluginScope.PROJECT and project_id is not None and binding == project_id
        )

    @staticmethod
    def _operation_view(operation: _Operation) -> dict[str, Any]:
        result = operation.result.to_dict()
        result.update({"action": operation.action, "state": operation.state})
        return result

    def tools_for_session(
        self,
        base_tools: ToolRegistry,
        session_id: str,
        *,
        project_id: str | None = None,
        policy: DynamicPluginPolicy | None = None,
    ) -> ToolRegistry:
        """Return a live dynamic overlay, or the untouched base view."""
        base_tools = self._unwrap_dynamic_view(base_tools)
        if (
            (policy is not None and not policy.enabled)
            or not self.is_authorized(session_id, project_id)
        ):
            return base_tools
        return DynamicPluginRegistry(self, base_tools, session_id, project_id, policy=policy)

    def live_tools_for_session(
        self,
        base_tools: ToolRegistry,
        session_id: str,
        *,
        project_id: str | None = None,
        policy: DynamicPluginPolicy | None = None,
    ) -> ToolRegistry:
        """Return a live overlay that can activate after an inline command."""
        required_methods = ("has", "restricted_to", "get_runtime_context_providers")
        if any(not callable(getattr(base_tools, name, None)) for name in required_methods):
            return base_tools
        return DynamicPluginRegistry(
            self,
            self._unwrap_dynamic_view(base_tools),
            session_id,
            project_id,
            policy=policy,
        )

    @staticmethod
    def _unwrap_dynamic_view(base_tools: ToolRegistry) -> ToolRegistry:
        while isinstance(base_tools, DynamicPluginRegistry):
            base_tools = base_tools._base
        return base_tools

    def _load_tool(
        self,
        registration: _RegisteredPlugin,
        *,
        scope_key: ScopeKey,
        allow_service_replacement: bool = False,
        replaceable_service_ids: frozenset[str] = frozenset(),
    ) -> tuple[Tool, ManagedPluginContext]:
        manifest = registration.record.manifest
        plugin_context = ManagedPluginContext(
            legacy_context=self._tool_context,
            subscribe_impl=self._subscribe_impl,
            service_registry=self._service_registry_for_scope(scope_key),
            allow_service_replacement=allow_service_replacement,
            replaceable_service_ids=replaceable_service_ids,
        )
        module_name = (
            "_nanobot_plugin_"
            + manifest.plugin_id.replace("-", "_")
            + "_"
            + manifest.version.replace(".", "_")
            + "_"
            + manifest.content_hash[:12]
        )
        try:
            entry = manifest.resolve_entry(registration.artifact_root)
            if not entry.is_file():
                raise OSError("plugin entry is not a file")
            if hashlib.sha256(entry.read_bytes()).hexdigest() != manifest.content_hash:
                raise ValueError("plugin entry changed after registration")
            module_spec = importlib.util.spec_from_file_location(module_name, entry)
            if module_spec is None or module_spec.loader is None:
                raise ImportError("plugin module could not be created")
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            candidates = [
                value
                for value in vars(module).values()
                if isinstance(value, type)
                and issubclass(value, Tool)
                and value is not Tool
                and value.__module__ == module_name
                and not getattr(value, "__abstractmethods__", None)
            ]
            if len(candidates) != 1:
                raise TypeError("a H3 plugin entry must define exactly one concrete Tool")
            tool_cls = candidates[0]
            # H2 keeps the legacy ``ToolContext`` factory annotation, while
            # H3 supplies the compatible managed context at runtime.
            tool = tool_cls.create(cast(ToolContext, cast(object, plugin_context)))
            self._validate_loaded_tool(manifest.provides, tool)
            return tool, plugin_context
        except BaseException as exc:
            raise _PluginLoadError(exc, plugin_context) from exc

    @staticmethod
    def _validate_h2_declaration(record: PluginRecord) -> None:
        manifest = record.manifest
        if manifest.execution_mode is not PluginExecutionMode.IN_PROCESS:
            raise PluginContractError(
                PluginErrorCode.PLUGIN_LOAD_FAILED,
                "H2 developer tools must use in-process execution",
            )
        if set(manifest.requested_capabilities) != {"read_only"}:
            raise PluginContractError(
                PluginErrorCode.PLUGIN_NOT_PURE,
                "H2 only admits the read_only capability",
            )

    @staticmethod
    def _validate_loaded_tool(provides: Collection[str], tool: Tool) -> None:
        declared = [name.removeprefix("tool:") for name in provides if name.startswith("tool:")]
        if len(declared) != 1 or declared[0] != tool.name:
            raise ValueError("manifest tool capability does not match the loaded tool")
        if not tool.read_only or tool.exclusive or not tool.concurrency_safe:
            raise ValueError("H2 plugin must be a non-exclusive read-only tool")

    @staticmethod
    def _registration_key(record: PluginRecord) -> RegistrationKey:
        manifest = record.manifest
        binding = record.session_id if manifest.scope is PluginScope.SESSION else record.project_id
        return (manifest.plugin_id, manifest.version, manifest.scope.value, binding or "")

    @staticmethod
    def _sort_key(item: _RegisteredPlugin) -> tuple[str, str, str, str]:
        return item.registration_key

    @staticmethod
    def _is_visible(record: PluginRecord, session_id: str, project_id: str | None) -> bool:
        manifest = record.manifest
        return (
            manifest.scope is PluginScope.SESSION and record.session_id == session_id
        ) or (
            manifest.scope is PluginScope.PROJECT
            and project_id is not None
            and record.project_id == project_id
        )

    @staticmethod
    def _scope_key(
        scope: PluginScope,
        session_id: str,
        project_id: str | None,
    ) -> ScopeKey | None:
        if scope is PluginScope.SESSION and session_id:
            return (scope, session_id)
        if scope is PluginScope.PROJECT and project_id:
            return (scope, project_id)
        return None

    def _registrations_for_scope(self, scope_key: ScopeKey) -> tuple[_RegisteredPlugin, ...]:
        scope, binding = scope_key
        return tuple(
            item
            for item in self._registered.values()
            if item.record.manifest.scope is scope
            and self._registration_key(item.record)[3] == binding
        )

    def _service_registry_for_scope(self, scope_key: ScopeKey) -> dict[str, Any]:
        """Return the service binding table owned by one session/project."""
        registry = self._service_registries.get(scope_key)
        if registry is None:
            registry = dict(self._service_registry_seed)
            self._service_registries[scope_key] = registry
        return registry

    def _active_providers(
        self,
        scope_key: ScopeKey,
        *,
        exclude: _ActiveRoute | None = None,
    ) -> dict[str, _ActiveRoute]:
        providers: dict[str, _ActiveRoute] = {}
        for route in self._routes.get(scope_key, {}).values():
            if route is exclude:
                continue
            for interface_id in route.registration.record.manifest.provides:
                providers[interface_id] = route
        return providers

    def _active_dependents(
        self,
        scope_key: ScopeKey,
        provider: _ActiveRoute,
    ) -> tuple[_ActiveRoute, ...]:
        """Return a provider and every active consumer in its dependency subgraph."""
        affected = [provider]
        seen = {provider.route_key}
        pending = [provider]
        while pending:
            current = pending.pop(0)
            provided = set(current.registration.record.manifest.provides)
            for dependent in tuple(self._routes.get(scope_key, {}).values()):
                if dependent.route_key in seen:
                    continue
                required = set(dependent.registration.record.manifest.requires)
                if not provided.intersection(required):
                    continue
                seen.add(dependent.route_key)
                affected.append(dependent)
                pending.append(dependent)
        return tuple(affected)

    async def _wait_for_routes_idle(self, routes: tuple[_ActiveRoute, ...]) -> bool:
        """Keep one dependency composition together while in-flight calls drain."""
        waits = tuple(
            route.lease_zero.wait()
            for route in routes
            if route.leases > 0 and route.lease_zero is not None
        )
        if not waits:
            return True
        try:
            await asyncio.wait_for(asyncio.gather(*waits), timeout=self._drain_timeout_s)
        except asyncio.TimeoutError:
            return False
        return True

    def _missing_dependencies(
        self,
        registration: _RegisteredPlugin,
        scope_key: ScopeKey,
        *,
        exclude: _ActiveRoute | None = None,
    ) -> tuple[str, ...]:
        providers = self._active_providers(scope_key, exclude=exclude)
        return tuple(
            required
            for required in registration.record.manifest.requires
            if required not in providers
        )

    def _provided_interface_conflicts(
        self,
        registration: _RegisteredPlugin,
        scope_key: ScopeKey,
        *,
        exclude: _ActiveRoute | None = None,
    ) -> tuple[str, ...]:
        providers = self._active_providers(scope_key, exclude=exclude)
        return tuple(
            interface_id
            for interface_id in registration.record.manifest.provides
            if interface_id in providers
        )

    def _dependency_cycle(
        self,
        scope_key: ScopeKey,
        target: RegistrationKey,
    ) -> tuple[str, ...]:
        registrations = {item.registration_key: item for item in self._registrations_for_scope(scope_key)}
        if target not in registrations:
            return ()
        providers: dict[str, list[RegistrationKey]] = {}
        for key, item in registrations.items():
            for interface_id in item.record.manifest.provides:
                providers.setdefault(interface_id, []).append(key)
        visiting: list[RegistrationKey] = []
        active: set[RegistrationKey] = set()

        def visit(key: RegistrationKey) -> tuple[str, ...]:
            if key in active:
                start = visiting.index(key)
                return tuple(item[0] for item in visiting[start:] + [key])
            if key in visiting:
                return ()
            visiting.append(key)
            active.add(key)
            item = registrations[key]
            for required in item.record.manifest.requires:
                for provider_key in providers.get(required, []):
                    cycle = visit(provider_key)
                    if cycle:
                        return cycle
            active.remove(key)
            visiting.pop()
            return ()

        return visit(target)

    def _activate_waiting(self, scope_key: ScopeKey) -> None:
        """Activate waiting consumers in dependency order after a provider appears."""
        if scope_key in self._dependency_busy:
            return
        self._dependency_busy.add(scope_key)
        try:
            progressed = True
            while progressed:
                progressed = False
                waiting = tuple(self._waiting_routes.get(scope_key, set()))
                for registration_key in waiting:
                    registration = self._registered.get(registration_key)
                    if registration is None:
                        self._waiting_routes[scope_key].discard(registration_key)
                        continue
                    if self._missing_dependencies(registration, scope_key):
                        continue
                    if self._provided_interface_conflicts(registration, scope_key):
                        self._lifecycles[registration_key] = PluginLifecycle.FAILED
                        self._waiting_routes[scope_key].discard(registration_key)
                        continue
                    plugin_context: ManagedPluginContext | None = None
                    try:
                        tool, plugin_context = self._load_tool(registration, scope_key=scope_key)
                        plugin_context.activate()
                    except Exception as exc:
                        if isinstance(exc, _PluginLoadError):
                            plugin_context = exc.context
                        self._lifecycles[registration_key] = PluginLifecycle.FAILED
                        self._waiting_routes[scope_key].discard(registration_key)
                        self._staging_failure_operation(
                            action="load",
                            plugin_id=registration.record.manifest.plugin_id,
                            version=registration.record.manifest.version,
                            scope_key=scope_key,
                            context=plugin_context,
                            code=PluginErrorCode.PLUGIN_LOAD_FAILED,
                            message="waiting plugin activation failed",
                            details={"route_commit": "not_committed", "recovery": "dependency"},
                        )
                        continue
                    self._next_generation += 1
                    route_key = (
                        scope_key,
                        registration.record.manifest.plugin_id,
                        registration.record.manifest.version,
                        self._next_generation,
                    )
                    route = _ActiveRoute(
                        registration,
                        self._next_generation,
                        ManagedPluginTool(self, route_key, tool),
                        plugin_context,
                    )
                    route.lease_zero = asyncio.Event()
                    route.lease_zero.set()
                    self._routes.setdefault(scope_key, {})[route_key[1]] = route
                    self._waiting_routes[scope_key].discard(registration_key)
                    self._lifecycles[registration_key] = PluginLifecycle.ACTIVE
                    operation = self._new_operation(
                        action="load",
                        plugin_id=route_key[1],
                        version=route_key[2],
                        scope_key=scope_key,
                        state=PluginLifecycle.ACTIVE.value,
                        result=PluginOperationResult.success(
                            "waiting plugin activated after dependencies became available",
                            plugin_id=route_key[1],
                            version=route_key[2],
                            details={
                                "state": PluginLifecycle.ACTIVE.value,
                                "generation": str(route.generation),
                                "dependency_activation": "true",
                            },
                        ),
                    )
                    operation.event.set()
                    self._save_state()
                    progressed = True
        finally:
            self._dependency_busy.discard(scope_key)

    def _suspend_dependents(self, scope_key: ScopeKey, provider: _ActiveRoute) -> None:
        # Walk the affected dependency subgraph, not just direct consumers.
        # This prevents a second-level consumer from continuing to receive
        # calls through a provider that has already been withdrawn.
        pending = [provider]
        seen: set[RouteKey] = set()
        while pending:
            current = pending.pop(0)
            if current.route_key in seen:
                continue
            seen.add(current.route_key)
            provided = set(current.registration.record.manifest.provides)
            for plugin_id, dependent in tuple(self._routes.get(scope_key, {}).items()):
                if dependent.route_key in seen or dependent is current:
                    continue
                required = set(dependent.registration.record.manifest.requires)
                if not provided.intersection(required):
                    continue
                self._routes[scope_key].pop(plugin_id, None)
                self._retire_route(scope_key, dependent)
                self._waiting_routes.setdefault(scope_key, set()).add(
                    dependent.registration.registration_key
                )
                self._lifecycles[dependent.registration.registration_key] = PluginLifecycle.WAITING
                operation = self._new_operation(
                    action="dependency_stop",
                    plugin_id=plugin_id,
                    version=dependent.registration.record.manifest.version,
                    scope_key=scope_key,
                    state=PluginLifecycle.DRAINING.value,
                    persist=False,
                    result=PluginOperationResult.success(
                        "dependent plugin is waiting for its provider",
                        plugin_id=plugin_id,
                        version=dependent.registration.record.manifest.version,
                        details={
                            "state": PluginLifecycle.DRAINING.value,
                            "dependency": "provider_stopped",
                        },
                    ),
                )
                self._retired_operations[dependent.route_key] = operation
                self._start_retirement(operation, dependent)
                pending.append(dependent)

    def _find_visible(
        self,
        plugin_id: str,
        version: str,
        session_id: str,
        project_id: str | None,
    ) -> _RegisteredPlugin | None:
        matches = [
            item
            for item in self._registered.values()
            if item.record.manifest.plugin_id == plugin_id
            and item.record.manifest.version == version
            and self._is_visible(item.record, session_id, project_id)
        ]
        return sorted(matches, key=self._sort_key)[0] if matches else None

    def _find_active(
        self,
        plugin_id: str,
        session_id: str,
        project_id: str | None,
    ) -> tuple[_ActiveRoute | None, ScopeKey | None]:
        session_key = (PluginScope.SESSION, session_id)
        self._restore_route_if_needed(session_key, plugin_id)
        route = self._routes.get(session_key, {}).get(plugin_id)
        if route is not None:
            return route, session_key
        if project_id is not None:
            project_key = (PluginScope.PROJECT, project_id)
            self._restore_route_if_needed(project_key, plugin_id)
            route = self._routes.get(project_key, {}).get(plugin_id)
            if route is not None:
                return route, project_key
        return None, None

    def _active_routes(
        self,
        session_id: str,
        project_id: str | None,
    ) -> tuple[_ActiveRoute, ...]:
        routes: dict[str, _ActiveRoute] = {}
        for scope_key in ((PluginScope.PROJECT, project_id), (PluginScope.SESSION, session_id)):
            if scope_key[1] is None:
                continue
            if scope_key not in self._authorized:
                continue
            for plugin_id in tuple(self._restored_routes_for_scope(cast(ScopeKey, scope_key))):
                self._restore_route_if_needed(cast(ScopeKey, scope_key), plugin_id)
            for plugin_id, route in self._routes.get(cast(ScopeKey, scope_key), {}).items():
                routes[plugin_id] = route
        return tuple(routes[name] for name in sorted(routes))

    def _restored_routes_for_scope(self, scope_key: ScopeKey) -> tuple[str, ...]:
        return tuple(
            plugin_id
            for (candidate_scope, plugin_id) in self._restored_routes
            if candidate_scope == scope_key
        )

    def _restore_route_if_needed(self, scope_key: ScopeKey, plugin_id: str) -> None:
        pending = self._restored_routes.get((scope_key, plugin_id))
        if pending is None or plugin_id in self._routes.get(scope_key, {}):
            return
        registration, generation = pending
        plugin_context: ManagedPluginContext | None = None
        try:
            tool, plugin_context = self._load_tool(registration, scope_key=scope_key)
            plugin_context.activate()
        except Exception as exc:
            if isinstance(exc, _PluginLoadError):
                plugin_context = exc.context
            self._lifecycles[registration.registration_key] = PluginLifecycle.FAILED
            self._restored_routes.pop((scope_key, plugin_id), None)
            self._staging_failure_operation(
                action="restore",
                plugin_id=plugin_id,
                version=registration.record.manifest.version,
                scope_key=scope_key,
                context=plugin_context,
                code=PluginErrorCode.PLUGIN_LOAD_FAILED,
                message="plugin restoration failed; the route remains inactive",
                details={"route_commit": "not_committed", "recovery": "restart"},
            )
            self._save_state()
            return
        route_key = (scope_key, plugin_id, registration.record.manifest.version, generation)
        route = _ActiveRoute(
            registration,
            generation,
            ManagedPluginTool(self, route_key, tool),
            plugin_context,
        )
        route.lease_zero = asyncio.Event()
        route.lease_zero.set()
        self._routes.setdefault(scope_key, {})[plugin_id] = route
        self._restored_routes.pop((scope_key, plugin_id), None)
        self._lifecycles[registration.registration_key] = PluginLifecycle.ACTIVE
        self._save_state()

    def _active_proxy(
        self,
        session_id: str,
        project_id: str | None,
        tool_name: str,
    ) -> ManagedPluginTool | None:
        return next(
            (route.tool for route in self._active_routes(session_id, project_id) if route.tool.name == tool_name),
            None,
        )

    def _view(self, registration: _RegisteredPlugin) -> dict[str, Any]:
        record = registration.record
        manifest = record.manifest
        active = next(
            (
                route
                for routes in self._routes.values()
                for route in routes.values()
                if route.registration.registration_key == registration.registration_key
            ),
            None,
        )
        restored = next(
            (
                generation
                for (scope_key, plugin_id), (_item, generation) in self._restored_routes.items()
                if plugin_id == manifest.plugin_id
                and scope_key[0] is manifest.scope
                and scope_key[1] == (record.session_id or record.project_id)
                and _item.registration_key == registration.registration_key
            ),
            None,
        )
        result: dict[str, Any] = {
            "plugin_id": manifest.plugin_id,
            "version": manifest.version,
            "source": record.source.value,
            "scope": manifest.scope.value,
            "lifecycle": self._lifecycles[registration.registration_key].value,
            "online_lifecycle": True,
            "content_hash": manifest.content_hash,
        }
        if active is not None:
            result.update({"generation": active.generation, "tool": active.tool.name})
        elif restored is not None:
            result.update({"generation": restored, "tool": next((name.removeprefix("tool:") for name in manifest.provides if name.startswith("tool:")), "")})
        return result

    @staticmethod
    def _policy_allows(
        policy: DynamicPluginPolicy,
        manifest: Any,
        action: str,
    ) -> bool:
        return policy.allows(
            kind=manifest.kind.value,
            capabilities=set(manifest.requested_capabilities),
            action=action,
            scope=manifest.scope.value,
        )

    @staticmethod
    def _failure_not_authorized(plugin_id: str, version: str) -> PluginOperationResult:
        return PluginOperationResult.failure(
            PluginErrorCode.PLUGIN_NOT_AUTHORIZED,
            "dynamic plugin management is not authorized for this scope",
            plugin_id=plugin_id,
            version=version,
        )

    def _reuse_operation(
        self,
        operation_id: str,
        *,
        action: str,
        plugin_id: str,
        version: str | None,
        scope_key: ScopeKey,
    ) -> PluginOperationResult | None:
        """Return a prior result for a duplicate request, or a tuple conflict."""
        operation = self._operations.get(operation_id)
        if operation is None:
            return None
        if (
            operation.action != action
            or operation.plugin_id != plugin_id
            or operation.version != version
            or operation.scope_key != scope_key
        ):
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_OPERATION_CONFLICT,
                "operation_id is already bound to a different plugin action",
                plugin_id=plugin_id,
                version=version,
                operation_id=operation_id,
            )
        return operation.result

    def _new_operation(
        self,
        *,
        action: str,
        plugin_id: str,
        version: str | None,
        scope_key: ScopeKey,
        state: str,
        operation_id: str | None = None,
        persist: bool = True,
        result: PluginOperationResult,
    ) -> _Operation:
        operation_id = operation_id or str(uuid.uuid4())
        operation = _Operation(
            operation_id=operation_id,
            action=action,
            plugin_id=plugin_id,
            version=version,
            state=state,
            result=PluginOperationResult(
                ok=result.ok,
                code=result.code,
                message=result.message,
                plugin_id=result.plugin_id,
                version=result.version,
                operation_id=operation_id,
                details=result.details_dict,
            ),
            event=asyncio.Event(),
            scope_key=scope_key,
        )
        self._operations[operation_id] = operation
        if persist:
            self._save_state()
        return operation

    def _retire_route(self, scope_key: ScopeKey, route: _ActiveRoute) -> None:
        """Keep an old generation addressable until its cleanup is complete."""
        self._retired_routes[route.route_key] = route
        route.tool.mark_retired()
        self._routes.setdefault(scope_key, {})

    def _start_retirement(self, operation: _Operation, route: _ActiveRoute) -> None:
        if route.cleanup_started:
            return
        if route.leases == 0:
            self._begin_retirement(operation, route)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._drain(operation, route))
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    def _begin_retirement(self, operation: _Operation, route: _ActiveRoute) -> None:
        if route.cleanup_started:
            return
        route.cleanup_started = True
        if not route.context.has_resources:
            self._complete_retirement(operation, route)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            route.cleanup_started = False
            return
        task = loop.create_task(self._cleanup_retired(operation, route))
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    async def _cleanup_retired(self, operation: _Operation, route: _ActiveRoute) -> None:
        try:
            await route.context.cleanup()
        except Exception:
            route.cleanup_started = False
            self._lifecycles[route.registration.registration_key] = PluginLifecycle.FAILED
            operation.state = "cleanup_failed"
            operation.result = PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_CLEANUP_FAILED,
                "plugin route retired but owned resources still need cleanup",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation.operation_id,
                details={
                    "route_commit": "committed",
                    "old_cleanup": "failed",
                    "retry_available": "true",
                },
            )
            operation.event.set()
            self._save_state()
            return
        self._complete_retirement(operation, route)

    async def _cleanup_staging_context(self, context: ManagedPluginContext) -> None:
        await context.cleanup()

    def _staging_failure_operation(
        self,
        *,
        action: str,
        plugin_id: str,
        version: str,
        scope_key: ScopeKey,
        context: ManagedPluginContext | None,
        code: PluginErrorCode,
        message: str,
        details: dict[str, str],
        operation_id: str | None = None,
    ) -> PluginOperationResult:
        details = dict(details)
        details["original_code"] = code.value
        details["original_message"] = message
        details["staging_cleanup"] = "pending" if context is not None else "not_required"
        operation = self._new_operation(
            action=action,
            plugin_id=plugin_id,
            version=version,
            scope_key=scope_key,
            state="failed",
            operation_id=operation_id,
            result=PluginOperationResult.failure(
                code,
                message,
                plugin_id=plugin_id,
                version=version,
                details=details,
            ),
        )
        if context is None:
            operation.event.set()
        else:
            self._staging_operations[operation.operation_id] = context
            self._schedule_failed_staging_cleanup(operation, context, code, message)
        self._save_state()
        return operation.result

    def _schedule_failed_staging_cleanup(
        self,
        operation: _Operation,
        context: ManagedPluginContext,
        code: PluginErrorCode,
        message: str,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._finish_failed_staging_cleanup(operation, context, code, message)
        )
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)
        task.add_done_callback(self._consume_background_error)

    async def _finish_failed_staging_cleanup(
        self,
        operation: _Operation,
        context: ManagedPluginContext,
        code: PluginErrorCode,
        message: str,
    ) -> None:
        try:
            await context.cleanup()
        except Exception:
            details = operation.result.details_dict
            operation.result = PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_CLEANUP_FAILED,
                "staged plugin resources still need cleanup",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation.operation_id,
                details={
                    "route_commit": "not_committed",
                    "staging_cleanup": "failed",
                    "original_code": details.get(
                        "original_code", PluginErrorCode.PLUGIN_UPDATE_FAILED.value
                    ),
                    "original_message": details.get(
                        "original_message", "staged plugin operation failed"
                    ),
                },
            )
        else:
            self._staging_operations.pop(operation.operation_id, None)
            details = dict(operation.result.details_dict)
            details["staging_cleanup"] = "complete"
            operation.result = PluginOperationResult.failure(
                code,
                message,
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation.operation_id,
                details=details,
            )
        operation.event.set()
        self._save_state()

    def _schedule_context_cleanup(self, context: ManagedPluginContext) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._cleanup_staging_context(context))
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)
        task.add_done_callback(self._consume_background_error)

    @staticmethod
    def _consume_background_error(task: asyncio.Task[Any]) -> None:
        """Keep a best-effort cleanup failure from becoming an unhandled task warning."""
        try:
            task.result()
        except BaseException:
            return

    async def _drain(self, operation: _Operation, route: _ActiveRoute) -> None:
        assert route.lease_zero is not None
        try:
            await asyncio.wait_for(route.lease_zero.wait(), timeout=self._drain_timeout_s)
        except asyncio.TimeoutError:
            operation.state = PluginLifecycle.DRAINING.value
            operation.result = PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_STOP_TIMEOUT,
                "plugin is still draining; no new calls use this version",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation.operation_id,
                details={"state": PluginLifecycle.DRAINING.value, "leases": str(route.leases)},
            )
            operation.event.set()
            self._save_state()
            return
        self._begin_retirement(operation, route)

    def _finish_stop(self, operation: _Operation, route: _ActiveRoute) -> None:
        """Compatibility alias for the H2 stop seam."""
        self._complete_retirement(operation, route)

    def _complete_retirement(self, operation: _Operation, route: _ActiveRoute) -> None:
        self._retired_routes.pop(route.route_key, None)
        self._retired_operations.pop(route.route_key, None)
        self._restored_retired.pop(route.route_key, None)
        route.cleanup_started = False
        current_route = self._routes.get(route.route_key[0], {}).get(
            route.registration.record.manifest.plugin_id
        )
        waiting = route.registration.registration_key in self._waiting_routes.get(
            route.route_key[0], set()
        )
        self._lifecycles[route.registration.registration_key] = (
            PluginLifecycle.WAITING
            if waiting
            else (
                PluginLifecycle.ACTIVE
                if (
                    current_route is not None
                    and current_route.registration.registration_key
                    == route.registration.registration_key
                )
                else PluginLifecycle.INACTIVE
            )
        )
        if operation.action == "update":
            operation.state = PluginLifecycle.ACTIVE.value
            operation.result = PluginOperationResult.success(
                "plugin update completed and the previous version was cleaned up",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation.operation_id,
                details={
                    "state": PluginLifecycle.ACTIVE.value,
                    "route_commit": "committed",
                    "old_cleanup": "complete",
                    "generation": str(current_route.generation if current_route is not None else route.generation),
                },
            )
        elif operation.action == "dependency_stop":
            operation.state = PluginLifecycle.WAITING.value
            operation.result = PluginOperationResult.success(
                "dependent plugin is waiting for its provider",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation.operation_id,
                details={
                    "state": PluginLifecycle.WAITING.value,
                    "dependency": "provider_stopped",
                    "generation": str(route.generation),
                },
            )
        else:
            operation.state = PluginLifecycle.INACTIVE.value
            operation.result = PluginOperationResult.success(
                "plugin stopped for the current scope",
                plugin_id=operation.plugin_id,
                version=operation.version,
                operation_id=operation.operation_id,
                details={"state": PluginLifecycle.INACTIVE.value, "generation": str(route.generation)},
            )
        operation.event.set()
        self._save_state()

    def _is_dependency_switching(self, route_key: RouteKey) -> bool:
        return route_key in self._dependency_switching.get(route_key[0], set())

    def _dependency_group(self, route_key: RouteKey) -> tuple[RouteKey, ...] | None:
        scope_key = route_key[0]
        route = self._routes.get(scope_key, {}).get(route_key[1])
        if route is None or route.route_key != route_key:
            return None
        group = [route]
        seen = {route.route_key}
        pending = [route]
        while pending:
            current = pending.pop(0)
            for required in current.registration.record.manifest.requires:
                provider = self._active_providers(scope_key).get(required)
                if provider is None:
                    return None
                if provider.route_key not in seen:
                    seen.add(provider.route_key)
                    group.append(provider)
                    pending.append(provider)
        return tuple(item.route_key for item in group)

    def _begin_call(self, route_key: RouteKey) -> tuple[RouteKey, ...] | None:
        scope_key, plugin_id, version, generation = route_key
        if route_key in self._dependency_switching.get(scope_key, set()):
            return None
        route = self._routes.get(scope_key, {}).get(plugin_id)
        if route is None or route.generation != generation:
            return None
        if route.registration.record.manifest.version != version:
            return None
        group = self._dependency_group(route_key)
        if group is None:
            return None
        routes: list[_ActiveRoute] = []
        for member_key in group:
            member = self._routes.get(member_key[0], {}).get(member_key[1])
            if member is None or member.route_key != member_key or member.lease_zero is None:
                return None
            routes.append(member)
        for member in routes:
            lease_zero = member.lease_zero
            assert lease_zero is not None
            if member.leases == 0:
                lease_zero.clear()
            member.leases += 1
        return group

    def _end_call(self, lease: tuple[RouteKey, ...]) -> None:
        for route_key in lease:
            scope_key, plugin_id, _version, generation = route_key
            route = self._routes.get(scope_key, {}).get(plugin_id)
            retired = False
            if route is None or route.generation != generation:
                route = self._retired_routes.get(route_key)
                retired = route is not None
            if route is None:
                continue
            route.leases = max(0, route.leases - 1)
            if route.leases == 0 and route.lease_zero is not None:
                route.lease_zero.set()
                if retired:
                    operation = self._retired_operations.get(route_key)
                    if operation is not None:
                        self._start_retirement(operation, route)


class ManagedPluginTool(Tool):
    """A tool proxy that acquires a version lease immediately before execute."""

    _plugin_discoverable = False

    def __init__(self, manager: PluginManager, route_key: RouteKey, wrapped: Tool) -> None:
        self._manager = manager
        self._route_key = route_key
        self._wrapped = wrapped
        self._retired = False

    @property
    def is_retired(self) -> bool:
        return self._retired

    def mark_retired(self) -> None:
        self._retired = True

    @property
    def name(self) -> str:
        return self._wrapped.name

    @property
    def description(self) -> str:
        return self._wrapped.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._wrapped.parameters

    @property
    def read_only(self) -> bool:
        return self._wrapped.read_only

    @property
    def exclusive(self) -> bool:
        return self._wrapped.exclusive

    @property
    def concurrency_safe(self) -> bool:
        return self._wrapped.concurrency_safe

    @property
    def config_key(self) -> str:
        return getattr(self._wrapped, "config_key", "")

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._wrapped.cast_params(params)

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        return self._wrapped.validate_params(params)

    def runtime_context_provider(self):
        return self._wrapped.runtime_context_provider()

    def set_context(self, ctx: Any) -> None:
        setter = getattr(self._wrapped, "set_context", None)
        if callable(setter):
            setter(ctx)

    async def execute(self, **kwargs: Any) -> Any:
        if self._retired:
            return ToolResult.error(
                "TOOL_VERSION_RETIRED: this plugin version was stopped before dispatch"
            )
        if self._manager._is_dependency_switching(self._route_key):
            return ToolResult.error(
                "PLUGIN_DEPENDENCY_SWITCHING: this dependency composition is switching"
            )
        lease = self._manager._begin_call(self._route_key)
        if lease is None:
            return ToolResult.error(
                "TOOL_VERSION_RETIRED: this plugin version was stopped before dispatch"
            )
        try:
            request = current_request_context()
            if request is not None:
                self.set_context(request)
            return await self._wrapped.execute(**kwargs)
        finally:
            self._manager._end_call(lease)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


class DynamicPluginRegistry(ToolRegistry):
    """A live overlay over one existing restricted ToolRegistry view."""

    def __init__(
        self,
        manager: PluginManager,
        base: ToolRegistry,
        session_id: str,
        project_id: str | None,
        *,
        policy: DynamicPluginPolicy | None = None,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._base = base
        self._session_id = session_id
        self._project_id = project_id
        self._policy = policy
        self._known: dict[str, ManagedPluginTool] = {}

    def _visible_routes(self) -> tuple[_ActiveRoute, ...]:
        if not self._manager.is_authorized(self._session_id, self._project_id):
            return ()
        routes = self._manager._active_routes(self._session_id, self._project_id)
        if self._policy is None:
            return routes
        return tuple(
            route
            for route in routes
            if self._manager._policy_allows(
                self._policy,
                route.registration.record.manifest,
                "load",
            )
        )

    @property
    def tool_names(self) -> list[str]:
        names = list(self._base.tool_names)
        for route in self._visible_routes():
            if route.tool.name not in names:
                names.append(route.tool.name)
                self._known[route.tool.name] = route.tool
        return names

    def get(self, name: str) -> Tool | None:
        if (tool := self._base.get(name)) is not None:
            return tool
        for route in self._visible_routes():
            if route.tool.name == name:
                self._known[name] = route.tool
                return route.tool
        # A view may have handed this proxy to the caller before stop/remove.
        # Keep it callable long enough to surface the stable retirement error;
        # do not keep arbitrary inactive tools reachable through the overlay.
        known = self._known.get(name)
        if known is not None and known.is_retired:
            return known
        self._known.pop(name, None)
        return None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def get_definitions(self) -> list[dict[str, Any]]:
        definitions = list(self._base.get_definitions())
        dynamic = [
            route.tool.to_schema()
            for route in self._visible_routes()
            if route.tool.name not in self._base.tool_names
        ]
        for route in self._visible_routes():
            self._known[route.tool.name] = route.tool
        return self._sort_definitions(definitions + dynamic)

    def get_runtime_context_providers(self) -> list[Any]:
        providers = list(self._base.get_runtime_context_providers())
        providers.extend(
            provider
            for route in self._visible_routes()
            if (provider := route.tool.runtime_context_provider()) is not None
        )
        return providers

    def prepare_call(self, name: str, params: Any):
        if self._base.has(name):
            return self._base.prepare_call(name, params)
        tool = self.get(name)
        if tool is None:
            return self._base.prepare_call(name, params)
        helper = ToolRegistry()
        helper.register(tool)
        return helper.prepare_call(name, params)

    async def execute(self, name: str, params: Any) -> Any:
        hint = "\n\n[Analyze the error above and try a different approach.]"
        tool, cast_params, error = self.prepare_call(name, params)
        if error:
            return ToolResult.error(str(error) + hint)
        if tool is None:
            # Preserve the base registry's execution seam for callers that
            # provide a custom prepare_call/execute pair.  This is also what
            # keeps an un-authorized live overlay transparent to core tools.
            return await self._base.execute(name, params)
        try:
            result = await tool.execute(**cast_params)
            if isinstance(result, ToolResult) and result.is_error:
                return ToolResult.error(str(result) + hint)
            return result
        except Exception as exc:
            return ToolResult.error(f"Error executing {name}: {exc}" + hint)

    def restricted_to(
        self,
        allowed_tools: Collection[str] | None = None,
        disabled_tools: Collection[str] = (),
    ) -> ToolRegistry:
        return DynamicPluginRegistry(
            self._manager,
            self._base.restricted_to(allowed_tools, disabled_tools),
            self._session_id,
            self._project_id,
            policy=self._policy,
        )

    def __len__(self) -> int:
        return len(self.tool_names)

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    @staticmethod
    def _sort_definitions(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = ToolRegistry._schema_name(schema)
            (mcp_tools if name.startswith("mcp_") else builtins).append(schema)
        builtins.sort(key=ToolRegistry._schema_name)
        mcp_tools.sort(key=ToolRegistry._schema_name)
        return builtins + mcp_tools
