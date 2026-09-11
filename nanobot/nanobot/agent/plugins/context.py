"""The narrow resource-ownership interface given to a managed plugin.

The protocol is intentionally small, but H3 also needs a concrete host-side
implementation.  ``ManagedPluginContext`` keeps all resources behind a single
owner boundary: registrations are staged while a version is preparing, become
live only after the route commit, and are released in reverse order when that
generation drains.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Protocol

from nanobot.agent.tools.base import Tool

Disposer = Callable[[], Awaitable[None] | None]
Subscriber = Callable[..., Awaitable[None] | None]
SubscribeImpl = Callable[[str, Subscriber], Disposer]


@dataclass(slots=True)
class _OwnedResource:
    """One lazily activated resource and its idempotent cleanup seam."""

    activate: Callable[[], None]
    dispose: Disposer
    active: bool = False
    kind: str = "registrations"


@dataclass(frozen=True, slots=True)
class PluginTaskHandle:
    """A host-tracked task plus its idempotent cleanup seam."""

    task: asyncio.Task[Any]
    dispose: Disposer


class ManagedPluginContext:
    """Host-owned implementation of :class:`PluginContext`.

    ``subscribe`` and ``provide_service`` are described before the route is
    committed, but their host effects are deferred until :meth:`activate`.
    Tasks are created as gated tasks and cannot run plugin code before the same
    activation point.  Cleanup is retryable: successful resources are removed
    from the ownership list while failed disposers remain visible to the
    manager for a later retry.
    """

    def __init__(
        self,
        *,
        legacy_context: Any | None = None,
        subscribe_impl: SubscribeImpl | None = None,
        service_registry: dict[str, Any] | None = None,
        allow_service_replacement: bool = False,
        replaceable_service_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._legacy_context = legacy_context
        self._subscribe_impl = subscribe_impl
        self._service_registry = service_registry if service_registry is not None else {}
        self._allow_service_replacement = allow_service_replacement
        self._replaceable_service_ids = replaceable_service_ids
        self._resources: list[_OwnedResource] = []
        self._active_event = asyncio.Event()
        self._active = False
        self._disposed = False
        self._cleanup_lock = asyncio.Lock()
        self._owned_tools: list[Tool] = []

    @property
    def has_resources(self) -> bool:
        """Whether this context needs asynchronous release work."""
        return bool(self._resources)

    @property
    def resource_counts(self) -> dict[str, int]:
        """Return safe counters used by lifecycle tests and status output."""
        counts = {"registrations": 0, "listeners": 0, "tasks": 0, "services": 0}
        for resource in self._resources:
            kind = getattr(resource, "kind", "registrations")
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    @property
    def is_active(self) -> bool:
        return self._active

    def register_tool(self, tool: Tool) -> Disposer:
        self._owned_tools.append(tool)

        def dispose() -> None:
            if tool in self._owned_tools:
                self._owned_tools.remove(tool)

        resource = _OwnedResource(lambda: None, dispose, active=True)
        self._resources.append(resource)
        return self._idempotent(dispose)

    def subscribe(self, event_name: str, callback: Subscriber) -> Disposer:
        host_disposer: Disposer | None = None
        disposed = False

        def activate() -> None:
            nonlocal host_disposer
            if self._subscribe_impl is not None:
                host_disposer = self._subscribe_impl(event_name, callback)

        async def dispose_async() -> None:
            nonlocal host_disposer, disposed
            if disposed:
                return
            if host_disposer is not None:
                result = host_disposer()
                if isawaitable(result):
                    await result
                host_disposer = None
            disposed = True

        resource = _OwnedResource(activate, dispose_async, kind="listeners")
        self._resources.append(resource)
        return self._idempotent(dispose_async)

    def spawn_task(self, awaitable: Awaitable[Any]) -> PluginTaskHandle:
        loop = asyncio.get_running_loop()
        started = False

        async def gated() -> Any:
            nonlocal started
            await self._active_event.wait()
            if not self._active:
                return None
            started = True
            return await awaitable

        task = loop.create_task(gated())
        disposed = False

        async def dispose_task() -> None:
            nonlocal disposed
            if disposed:
                return
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if not started:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
            disposed = True

        resource = _OwnedResource(lambda: None, dispose_task, active=True, kind="tasks")
        self._resources.append(resource)
        return PluginTaskHandle(task=task, dispose=self._idempotent(dispose_task))

    def provide_service(self, service_id: str, value: Any) -> Disposer:
        missing = object()
        previous: Any = missing
        owned = False

        def activate() -> None:
            nonlocal owned, previous
            previous = self._service_registry.get(service_id, missing)
            if (
                previous is not missing
                and not (
                    self._allow_service_replacement
                    and service_id in self._replaceable_service_ids
                )
            ):
                raise RuntimeError(f"service already provided: {service_id}")
            self._service_registry[service_id] = value
            owned = True

        def dispose() -> None:
            nonlocal owned, previous
            if owned and self._service_registry.get(service_id) is value:
                if previous is missing:
                    self._service_registry.pop(service_id, None)
                else:
                    self._service_registry[service_id] = previous
            owned = False

        resource = _OwnedResource(activate, dispose, kind="services")
        self._resources.append(resource)
        return self._idempotent(dispose)

    def get_service(self, service_id: str) -> Any:
        """Read a service already activated in this plugin's scope."""
        try:
            return self._service_registry[service_id]
        except KeyError as exc:
            raise RuntimeError(f"required service is unavailable: {service_id}") from exc

    def on_dispose(self, disposer: Disposer) -> None:
        resource = _OwnedResource(lambda: None, disposer, active=True)
        self._resources.append(resource)

    def activate(self) -> None:
        """Open all staged resources after the route has committed."""
        if self._disposed:
            raise RuntimeError("plugin context is already disposed")
        if self._active:
            return
        self._active = True
        try:
            for resource in self._resources:
                resource.activate()
                resource.active = True
        except BaseException:
            self._active = False
            self._active_event.clear()
            # Leave successfully activated resources owned and marked active.
            # Their async disposers must run through ``cleanup`` so a partial
            # activation cannot leak a host subscription or task.
            raise
        self._active_event.set()

    async def cleanup(self) -> None:
        """Release owned resources in reverse order; failed releases remain."""
        async with self._cleanup_lock:
            if self._disposed:
                return
            self._active = False
            self._active_event.clear()
            remaining: list[_OwnedResource] = []
            errors: list[BaseException] = []
            for resource in reversed(self._resources):
                if not resource.active and resource.kind not in {"tasks", "registrations"}:
                    continue
                try:
                    result = resource.dispose()
                    if isawaitable(result):
                        await result
                    resource.active = False
                except BaseException as exc:
                    remaining.append(resource)
                    errors.append(exc)
            self._resources = list(reversed(remaining))
            if errors:
                raise RuntimeError("one or more plugin resources failed to clean up") from errors[0]
            self._disposed = True

    @staticmethod
    def _idempotent(disposer: Disposer) -> Disposer:
        called = False

        async def wrapped() -> None:
            nonlocal called
            if called:
                return
            result = disposer()
            if isawaitable(result):
                await result
            called = True

        return wrapped

    def __getattr__(self, name: str) -> Any:
        """Keep legacy ``Tool.create(ToolContext)`` plugins source-compatible."""
        if self._legacy_context is not None:
            return getattr(self._legacy_context, name)
        raise AttributeError(name)


class PluginContext(Protocol):
    """Host-owned seam for registrations and resources.

    Implementations arrive in H2/H3.  Every registration returns an owned
    disposer, so a future manager can release resources in reverse order and
    never ask plugin code to mutate another plugin's state.
    """

    def register_tool(self, tool: Tool) -> Disposer:
        """Register a tool owned by the current plugin instance."""
        ...

    def subscribe(self, event_name: str, callback: Subscriber) -> Disposer:
        """Register an event listener owned by the current plugin instance."""
        ...

    def spawn_task(self, awaitable: Awaitable[Any]) -> PluginTaskHandle:
        """Start a host-tracked task and return its owned cleanup handle."""
        ...

    def provide_service(self, service_id: str, value: Any) -> Disposer:
        """Publish a scoped service and return its owned disposer."""
        ...

    def get_service(self, service_id: str) -> Any:
        """Read a service activated in the current plugin scope."""
        ...

    def on_dispose(self, disposer: Disposer) -> None:
        """Register cleanup that runs when the plugin instance is released."""
        ...
