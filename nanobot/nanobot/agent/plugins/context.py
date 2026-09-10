"""The narrow resource-ownership interface given to a managed plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from nanobot.agent.tools.base import Tool

Disposer = Callable[[], Awaitable[None] | None]
Subscriber = Callable[..., Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class PluginTaskHandle:
    """A host-tracked task plus its idempotent cleanup seam."""

    task: asyncio.Task[Any]
    dispose: Disposer


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

    def on_dispose(self, disposer: Disposer) -> None:
        """Register cleanup that runs when the plugin instance is released."""
        ...
