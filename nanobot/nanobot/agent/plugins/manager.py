"""Trusted, in-process plugin lifecycle for H2.

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
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from nanobot.agent.plugins.catalog import PluginCatalog, PluginRecord
from nanobot.agent.plugins.contracts import (
    DynamicPluginPolicy,
    PluginContractError,
    PluginErrorCode,
    PluginExecutionMode,
    PluginKind,
    PluginLifecycle,
    PluginOperationResult,
    PluginScope,
    PluginSource,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext

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
    leases: int = 0
    lease_zero: asyncio.Event | None = None

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


class PluginManager:
    """Own registered plugin versions and live scoped routing."""

    def __init__(
        self,
        base_registry: ToolRegistry,
        *,
        tool_context: ToolContext | None = None,
        drain_timeout_s: float = 30.0,
    ) -> None:
        self._base_registry = base_registry
        self._tool_context = tool_context
        self._drain_timeout_s = max(0.0, drain_timeout_s)
        self._registered: dict[RegistrationKey, _RegisteredPlugin] = {}
        self._lifecycles: dict[RegistrationKey, PluginLifecycle] = {}
        self._routes: dict[ScopeKey, dict[str, _ActiveRoute]] = {}
        self._retired_routes: dict[RouteKey, _ActiveRoute] = {}
        self._authorized: set[ScopeKey] = set()
        self._operations: dict[str, _Operation] = {}
        self._retired_operations: dict[RouteKey, _Operation] = {}
        self._drain_tasks: set[asyncio.Task[Any]] = set()
        self._next_generation = 0

    @staticmethod
    def project_id_for_path(path: str | Path) -> str:
        """Return the stable, non-sensitive project binding used by commands."""
        resolved = Path(path).resolve()
        return "local-" + hashlib.sha256(str(resolved).encode()).hexdigest()[:16]

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
        active = self._routes.setdefault(scope_key, {}).get(plugin_id)
        if active is not None:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_ALREADY_ACTIVE,
                "a version of this plugin is already active in this scope",
                plugin_id=plugin_id,
                version=version,
            )
        try:
            tool = self._load_tool(registration)
        except (OSError, RuntimeError, TypeError, ValueError, ImportError):
            self._lifecycles[registration.registration_key] = PluginLifecycle.FAILED
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_LOAD_FAILED,
                "plugin initialization failed; the existing registry is unchanged",
                plugin_id=plugin_id,
                version=version,
            )

        self._next_generation += 1
        route_key = (scope_key, plugin_id, version, self._next_generation)
        proxy = ManagedPluginTool(self, route_key, tool)
        route = _ActiveRoute(registration, self._next_generation, proxy)
        route.lease_zero = asyncio.Event()
        route.lease_zero.set()
        self._routes[scope_key][plugin_id] = route
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
        return operation.result

    def stop(
        self,
        plugin_id: str,
        *,
        session_id: str,
        project_id: str | None = None,
        policy: DynamicPluginPolicy | None = None,
    ) -> PluginOperationResult:
        """Retire a route immediately and drain only its existing leases."""
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
        self._routes[scope_key].pop(plugin_id, None)
        self._retired_routes[route.route_key] = route
        self._lifecycles[route.registration.registration_key] = PluginLifecycle.DRAINING
        operation = self._new_operation(
            action="stop",
            plugin_id=plugin_id,
            version=manifest.version,
            scope_key=scope_key,
            state=PluginLifecycle.DRAINING.value,
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
        if route.leases == 0:
            self._finish_stop(operation, route)
        else:
            try:
                task = asyncio.create_task(self._drain(operation, route))
            except RuntimeError:
                return operation.result
            self._drain_tasks.add(task)
            task.add_done_callback(self._drain_tasks.discard)
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

    def _load_tool(self, registration: _RegisteredPlugin) -> Tool:
        manifest = registration.record.manifest
        entry = manifest.resolve_entry(registration.artifact_root)
        if not entry.is_file():
            raise OSError("plugin entry is not a file")
        if hashlib.sha256(entry.read_bytes()).hexdigest() != manifest.content_hash:
            raise ValueError("plugin entry changed after registration")
        module_name = (
            "_nanobot_plugin_"
            + manifest.plugin_id.replace("-", "_")
            + "_"
            + manifest.version.replace(".", "_")
            + "_"
            + manifest.content_hash[:12]
        )
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
            raise TypeError("a H2 plugin entry must define exactly one concrete Tool")
        tool_cls = candidates[0]
        tool = tool_cls.create(self._tool_context) if self._tool_context is not None else tool_cls()
        self._validate_loaded_tool(manifest.provides, tool)
        return tool

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
        route = self._routes.get(session_key, {}).get(plugin_id)
        if route is not None:
            return route, session_key
        if project_id is not None:
            project_key = (PluginScope.PROJECT, project_id)
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
            for plugin_id, route in self._routes.get(cast(ScopeKey, scope_key), {}).items():
                routes[plugin_id] = route
        return tuple(routes[name] for name in sorted(routes))

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

    def _new_operation(
        self,
        *,
        action: str,
        plugin_id: str,
        version: str | None,
        scope_key: ScopeKey,
        state: str,
        result: PluginOperationResult,
    ) -> _Operation:
        operation_id = str(uuid.uuid4())
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
        return operation

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
            return
        self._finish_stop(operation, route)

    def _finish_stop(self, operation: _Operation, route: _ActiveRoute) -> None:
        self._retired_routes.pop(route.route_key, None)
        self._retired_operations.pop(route.route_key, None)
        current_route = self._routes.get(route.route_key[0], {}).get(
            route.registration.record.manifest.plugin_id
        )
        self._lifecycles[route.registration.registration_key] = (
            PluginLifecycle.ACTIVE if current_route is not None else PluginLifecycle.INACTIVE
        )
        operation.state = PluginLifecycle.INACTIVE.value
        operation.result = PluginOperationResult.success(
            "plugin stopped for the current scope",
            plugin_id=operation.plugin_id,
            version=operation.version,
            operation_id=operation.operation_id,
            details={"state": PluginLifecycle.INACTIVE.value, "generation": str(route.generation)},
        )
        operation.event.set()

    def _begin_call(self, route_key: RouteKey) -> bool:
        scope_key, plugin_id, version, generation = route_key
        route = self._routes.get(scope_key, {}).get(plugin_id)
        if route is None or route.generation != generation:
            return False
        if route.registration.record.manifest.version != version:
            return False
        if route.lease_zero is None:
            return False
        if route.leases == 0:
            route.lease_zero.clear()
        route.leases += 1
        return True

    def _end_call(self, route_key: RouteKey) -> None:
        scope_key, plugin_id, _version, generation = route_key
        route = self._routes.get(scope_key, {}).get(plugin_id)
        retired = False
        if route is None or route.generation != generation:
            route = self._retired_routes.get(route_key)
            retired = route is not None
        if route is None:
            return
        route.leases = max(0, route.leases - 1)
        if route.leases == 0 and route.lease_zero is not None:
            route.lease_zero.set()
            if retired:
                operation = self._retired_operations.get(route_key)
                if operation is not None:
                    self._finish_stop(operation, route)


class ManagedPluginTool(Tool):
    """A tool proxy that acquires a version lease immediately before execute."""

    _plugin_discoverable = False

    def __init__(self, manager: PluginManager, route_key: RouteKey, wrapped: Tool) -> None:
        self._manager = manager
        self._route_key = route_key
        self._wrapped = wrapped

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
        if not self._manager._begin_call(self._route_key):
            return ToolResult.error(
                "TOOL_VERSION_RETIRED: this plugin version was stopped before dispatch"
            )
        try:
            request = current_request_context()
            if request is not None:
                self.set_context(request)
            return await self._wrapped.execute(**kwargs)
        finally:
            self._manager._end_call(self._route_key)

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
        return self._known.get(name)

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
            return ToolResult.error(f"Error: Tool '{name}' not found." + hint)
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
