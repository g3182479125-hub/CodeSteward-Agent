"""Contracts for the managed, runtime-extensible agent plugins."""

from nanobot.agent.plugins.catalog import PluginCatalog, PluginRecord
from nanobot.agent.plugins.context import PluginContext, PluginTaskHandle
from nanobot.agent.plugins.contracts import (
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

__all__ = [
    "PluginCandidateStatus",
    "PluginCatalog",
    "PluginContext",
    "PluginContractError",
    "PluginErrorCode",
    "PluginExecutionMode",
    "PluginKind",
    "PluginLifecycle",
    "PluginManifest",
    "PluginOperationResult",
    "PluginRecord",
    "PluginScope",
    "PluginSource",
    "PluginTaskHandle",
]
