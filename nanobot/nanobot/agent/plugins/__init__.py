"""Contracts for the managed, runtime-extensible agent plugins."""

from nanobot.agent.plugins.catalog import PluginCatalog, PluginRecord
from nanobot.agent.plugins.context import ManagedPluginContext, PluginContext, PluginTaskHandle
from nanobot.agent.plugins.contracts import (
    DynamicPluginPolicy,
    PluginCandidateStatus,
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
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.plugins.store import PluginStateStore, SqlitePluginStateStore

__all__ = [
    "PluginCandidateStatus",
    "PluginCatalog",
    "PluginContext",
    "ManagedPluginContext",
    "PluginContractError",
    "DynamicPluginPolicy",
    "PluginErrorCode",
    "PluginExecutionMode",
    "PluginKind",
    "PluginLifecycle",
    "PluginManifest",
    "PluginManager",
    "PluginOperationResult",
    "PluginRecord",
    "PluginScope",
    "PluginSource",
    "PluginTaskHandle",
    "PluginStateStore",
    "SqlitePluginStateStore",
]
