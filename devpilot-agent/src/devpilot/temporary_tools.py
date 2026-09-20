"""Contracts and artifact creation for model-authored temporary tools.

This module deliberately contains no code loader and never imports candidate
source. T1/T2 define immutable records, lifecycle guards, and the frozen
artifact written before FC validation connects it to ``DynamicPluginRegistry``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any

from nanobot.agent.plugins.catalog import PluginRecord
from nanobot.agent.plugins.contracts import (
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginSource,
)
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.security.workspace_access import current_workspace_scope

from .execution import (
    ExecutionBackend,
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
    ExecutionResult,
)
from .fc_sandbox import build_fc_backend_from_environment

_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
TEMPORARY_TOOL_VALIDATION_VERSION = "temporary-tool-validation/v1"
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

_TEMPORARY_PLUGIN_MANAGER: PluginManager | None = None
_TEMPORARY_CONFIRMATION_LOCK = RLock()
_CONSUMED_TEMPORARY_CONFIRMATIONS: set[str] = set()
# Full Access already authorizes ordinary reads from the selected workspace.
# Keep the confirmation gate for actual privacy sources and external effects;
# ``sensitive_read`` is the conservative static label for workspace I/O and is
# still required in the manifest, but it is not an extra prompt in Full Access.
_SENSITIVE_CAPABILITIES = frozenset(
    {
        "privacy_read",
        "privacy_write",
        "external_send",
        "privacy_external_send",
    }
)
_EXTERNAL_MODULES = frozenset(
    {
        "aiohttp",
        "ftplib",
        "http",
        "httpx",
        "requests",
        "smtplib",
        "socket",
        "subprocess",
        "urllib",
        "websocket",
    }
)
_PRIVACY_MODULES = frozenset({"browser_cookie3", "keyring", "winreg"})


def bind_temporary_tool_manager(manager: PluginManager) -> None:
    """Bind the live nanobot manager used by the runtime activation tool."""
    global _TEMPORARY_PLUGIN_MANAGER
    _TEMPORARY_PLUGIN_MANAGER = manager


class TemporaryToolState(StrEnum):
    """Persisted states for one temporary tool candidate."""

    GAP_DETECTED = "gap_detected"
    GENERATED = "generated"
    VALIDATING = "validating"
    VALIDATED = "validated"
    REJECTED = "rejected"
    ACTIVE = "active"
    RETIRING = "retiring"
    CLEANUP_PENDING = "cleanup_pending"
    DISPOSED = "disposed"
    PERSISTENCE_REQUESTED = "persistence_requested"
    PERSISTED = "persisted"


_TRANSITIONS: dict[TemporaryToolState, frozenset[TemporaryToolState]] = {
    TemporaryToolState.GAP_DETECTED: frozenset({TemporaryToolState.GENERATED}),
    TemporaryToolState.GENERATED: frozenset(
        {TemporaryToolState.VALIDATING, TemporaryToolState.REJECTED, TemporaryToolState.RETIRING}
    ),
    TemporaryToolState.VALIDATING: frozenset(
        {TemporaryToolState.VALIDATED, TemporaryToolState.REJECTED}
    ),
    TemporaryToolState.VALIDATED: frozenset(
        {
            TemporaryToolState.ACTIVE,
            TemporaryToolState.RETIRING,
            TemporaryToolState.REJECTED,
        }
    ),
    TemporaryToolState.ACTIVE: frozenset(
        {TemporaryToolState.RETIRING, TemporaryToolState.PERSISTENCE_REQUESTED}
    ),
    TemporaryToolState.RETIRING: frozenset(
        {
            TemporaryToolState.CLEANUP_PENDING,
            TemporaryToolState.DISPOSED,
            TemporaryToolState.REJECTED,
            TemporaryToolState.PERSISTENCE_REQUESTED,
        }
    ),
    TemporaryToolState.CLEANUP_PENDING: frozenset(
        {TemporaryToolState.DISPOSED, TemporaryToolState.PERSISTENCE_REQUESTED}
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


class TemporaryToolArtifactConflict(TemporaryToolContractError):
    """Raised when an immutable candidate path already contains other content."""


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


def _source_capability_requirements(tree: ast.AST) -> frozenset[str]:
    """Infer capabilities for direct I/O so a candidate cannot hide them.

    This is intentionally a conservative static contract.  It does not try to
    classify arbitrary business logic; it catches the standard Python escape
    hatches used to read local data or contact an external endpoint.  The
    manifest still declares the intended capability, and activation gates the
    sensitive declarations on a trusted one-shot confirmation.
    """

    required: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                alias.name.split(".", 1)[0]
                for alias in node.names
            )
            for root in names:
                if root in _EXTERNAL_MODULES:
                    required.add("external_send")
                if root in _PRIVACY_MODULES:
                    required.add("privacy_read")
                if root in {"pathlib", "shutil", "tempfile"}:
                    required.add("sensitive_read")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"open", "__import__"}:
                literal_path = (
                    node.args[0].value
                    if node.args and isinstance(node.args[0], ast.Constant)
                    else None
                )
                if (
                    node.func.id == "__import__"
                    and isinstance(literal_path, str)
                    and literal_path.split(".", 1)[0] in _EXTERNAL_MODULES
                ):
                    required.add("external_send")
                elif isinstance(literal_path, str) and (
                    Path(literal_path).is_absolute()
                    or literal_path.startswith("~")
                    or ":" in literal_path
                    or literal_path == ".."
                    or literal_path.startswith("../")
                    or "/.." in literal_path
                ):
                    required.add("privacy_read")
                else:
                    # A runtime-computed path has no statically provable
                    # workspace boundary, so it must use the privacy gate.
                    required.add(
                        "privacy_read" if not isinstance(literal_path, str) else "sensitive_read"
                    )
            elif node.func.id in {"system", "popen", "spawn", "execv", "execve"}:
                required.add("external_send")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"read_text", "read_bytes", "open"}:
                literals = [
                    child.value
                    for child in ast.walk(node.func.value)
                    if isinstance(child, ast.Constant)
                ]
                if any(
                    isinstance(value, str)
                    and (
                        Path(value).is_absolute()
                        or value.startswith("~")
                        or ":" in value
                        or value == ".."
                        or value.startswith("../")
                        or "/.." in value
                    )
                    for value in literals
                ):
                    required.add("privacy_read")
                elif not literals:
                    required.add("privacy_read")
        elif isinstance(node, ast.Attribute):
            if node.attr in {
                "environ",
                "getenv",
                "read_text",
                "read_bytes",
                "write_text",
                "write_bytes",
                "unlink",
                "rmtree",
                "urlopen",
                "request",
                "get",
                "post",
                "put",
                "patch",
                "send",
                "connect",
                "home",
            }:
                if node.attr in {
                    "urlopen",
                    "request",
                    "get",
                    "post",
                    "put",
                    "patch",
                    "send",
                    "connect",
                    "run",
                    "call",
                    "check_call",
                    "check_output",
                    "system",
                    "popen",
                    "spawn",
                    "execv",
                    "execve",
                }:
                    required.add("external_send")
                elif node.attr in {"write_text", "write_bytes", "unlink", "rmtree"}:
                    required.add("sensitive_write")
                elif node.attr in {"environ", "getenv"}:
                    # Environment variables are outside the selected project
                    # workspace and commonly contain tokens or credentials.
                    required.add("privacy_read")
                elif node.attr == "home":
                    required.add("privacy_read")
                else:
                    required.add("sensitive_read")
    return frozenset(required)


def validate_temporary_tool_source(
    source: str,
    entry_class: str,
    *,
    declared_capabilities: tuple[str, ...] | list[str] = (),
) -> None:
    """Check the Python shape without importing or executing model code.

    Capability and side-effect checks belong to the FC validation phase. T2 only
    rejects malformed source and ensures the declared class exposes the Tool
    protocol expected by the future loader.
    """
    if not isinstance(source, str) or not source.strip():
        raise TemporaryToolContractError("temporary tool source must be non-empty text")
    if len(source.encode("utf-8")) > 256 * 1024:
        raise TemporaryToolContractError("temporary tool source exceeds 256 KiB")
    if _SYMBOL.fullmatch(entry_class) is None:
        raise TemporaryToolContractError("entry_class must be a Python class name")
    try:
        tree = ast.parse(source, filename="candidate.py", mode="exec")
    except SyntaxError as exc:
        raise TemporaryToolContractError("temporary tool source has invalid Python syntax") from exc
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == entry_class
    ]
    if len(classes) != 1:
        raise TemporaryToolContractError("temporary tool source must define its entry class once")
    entry = classes[0]
    inherits_tool = any(
        (isinstance(base, ast.Name) and base.id == "Tool")
        or (isinstance(base, ast.Attribute) and base.attr == "Tool")
        for base in entry.bases
    )
    if not inherits_tool:
        raise TemporaryToolContractError("temporary tool entry class must inherit Tool")
    if not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "execute"
        for node in entry.body
    ):
        raise TemporaryToolContractError("temporary tool entry class must define execute")
    required_capabilities = _source_capability_requirements(tree)
    undeclared = required_capabilities.difference(declared_capabilities)
    if undeclared:
        raise TemporaryToolContractError(
            "temporary tool uses undeclared capabilities: " + ",".join(sorted(undeclared))
        )


@dataclass(frozen=True, slots=True)
class TemporaryToolArtifact:
    """The immutable on-disk candidate produced before FC validation."""

    root: Path
    manifest: TemporaryToolManifest

    @property
    def source_path(self) -> Path:
        return self.root / self.manifest.entry_module


class TemporaryToolArtifactStore:
    """Write and read session-scoped candidate artifacts without importing them."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()

    def root_for(self, manifest: TemporaryToolManifest) -> Path:
        session_hash = hashlib.sha256(manifest.session_id.encode("utf-8")).hexdigest()[:16]
        return (
            self.data_dir
            / "temporary-tools"
            / session_hash
            / manifest.run_id
            / manifest.tool_id
            / manifest.version
            / manifest.source_hash
        )

    def write(self, manifest: TemporaryToolManifest, source: str) -> TemporaryToolArtifact:
        validate_temporary_tool_source(
            source,
            manifest.entry_class,
            declared_capabilities=manifest.declared_capabilities,
        )
        if source_sha256(source) != manifest.source_hash:
            raise TemporaryToolContractError("temporary tool source hash does not match manifest")
        root = self.root_for(manifest)
        root.mkdir(parents=True, exist_ok=True)
        source_path = root / manifest.entry_module
        manifest_path = root / "manifest.json"
        existing_source = source_path.read_text(encoding="utf-8") if source_path.exists() else None
        existing_manifest = (
            manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else None
        )
        expected_source = source
        expected_manifest = json.dumps(
            manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if existing_source not in (None, expected_source) or existing_manifest not in (
            None,
            expected_manifest,
        ):
            raise TemporaryToolArtifactConflict(
                "temporary tool artifact already contains other content"
            )
        if existing_source is None:
            self._write_new(source_path, expected_source)
        if existing_manifest is None:
            self._write_new(manifest_path, expected_manifest)
        artifact = TemporaryToolArtifact(root=root, manifest=manifest)
        self.ensure_lifecycle(artifact)
        return artifact

    @staticmethod
    def lifecycle_path(artifact: TemporaryToolArtifact) -> Path:
        return artifact.root / "lifecycle.json"

    @staticmethod
    def validation_receipt_path(artifact: TemporaryToolArtifact) -> Path:
        return artifact.root / "validation.json"

    def write_validation_receipt(
        self, artifact: TemporaryToolArtifact, result: TemporaryToolValidationResult
    ) -> None:
        """Persist a bounded, source-free FC receipt beside the immutable candidate."""
        path = self.validation_receipt_path(artifact)
        payload = result.to_dict()
        receipt = payload.get("receipt")
        if isinstance(receipt, dict):
            # Contract validation output is diagnostic only. Keep it bounded and
            # avoid accidentally persisting a provider's unbounded response.
            for key in ("stdout", "stderr"):
                value = receipt.get(key)
                if isinstance(value, str) and len(value) > 4096:
                    value = value[:4096] + "...[truncated]"
                if isinstance(value, str):
                    receipt[key] = _redact_gap_text(value)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)

    def load_lifecycle(self, artifact: TemporaryToolArtifact) -> TemporaryToolLifecycle:
        path = self.lifecycle_path(artifact)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TemporaryToolContractError("temporary tool lifecycle is invalid") from exc
        return TemporaryToolLifecycle.from_mapping(raw)

    def ensure_lifecycle(self, artifact: TemporaryToolArtifact) -> TemporaryToolLifecycle:
        path = self.lifecycle_path(artifact)
        if path.is_file():
            return self.load_lifecycle(artifact)
        lifecycle = TemporaryToolLifecycle(TemporaryToolState.GENERATED)
        self._write_new(
            path,
            json.dumps(
                lifecycle.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return lifecycle

    def transition_lifecycle(
        self, artifact: TemporaryToolArtifact, target: TemporaryToolState
    ) -> TemporaryToolLifecycle:
        lifecycle = self.load_lifecycle(artifact)
        lifecycle.transition(target)
        self.lifecycle_path(artifact).write_text(
            json.dumps(
                lifecycle.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
            newline="\n",
        )
        return lifecycle

    def load_for_session(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_id: str,
        version: str,
        source_hash: str,
    ) -> TemporaryToolArtifact:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        root = (
            self.data_dir
            / "temporary-tools"
            / session_hash
            / run_id
            / tool_id
            / version
            / source_hash
        )
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise TemporaryToolContractError("temporary tool artifact was not found")
        try:
            manifest = TemporaryToolManifest.from_mapping(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TemporaryToolContractError("temporary tool manifest is invalid") from exc
        if (
            manifest.session_id != session_id
            or manifest.run_id != run_id
            or manifest.tool_id != tool_id
            or manifest.version != version
            or manifest.source_hash != source_hash
        ):
            raise TemporaryToolContractError("temporary tool artifact binding is invalid")
        source_path = root / manifest.entry_module
        if not source_path.is_file():
            raise TemporaryToolContractError("temporary tool entry was not found")
        source = source_path.read_text(encoding="utf-8")
        if source_sha256(source) != manifest.source_hash:
            raise TemporaryToolContractError("temporary tool artifact hash changed")
        artifact = TemporaryToolArtifact(root=root, manifest=manifest)
        self.ensure_lifecycle(artifact)
        return artifact

    def pending_for_session(self, session_id: str) -> tuple[TemporaryToolArtifact, ...]:
        """List exact generated snapshots awaiting the post-Run decision."""
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        root = self.data_dir / "temporary-tools" / session_hash
        if not root.is_dir():
            return ()
        found: list[TemporaryToolArtifact] = []
        for manifest_path in root.glob("**/manifest.json"):
            try:
                manifest = TemporaryToolManifest.from_mapping(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                )
                artifact = self.load_for_session(
                    session_id=session_id,
                    run_id=manifest.run_id,
                    tool_id=manifest.tool_id,
                    version=manifest.version,
                    source_hash=manifest.source_hash,
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                TemporaryToolContractError,
            ):
                continue
            if manifest.expires_at > int(time.time()):
                found.append(artifact)
            else:
                # Expired snapshots are no longer eligible for persistence.
                # Remove only the exact validated artifact root so stale
                # source code cannot accumulate or be revived later.
                try:
                    self.delete_artifact(artifact)
                except OSError:
                    # A later startup/recovery sweep can retry cleanup; an
                    # expired artifact must never become a pending candidate.
                    continue
        return tuple(sorted(found, key=lambda item: item.manifest.created_at, reverse=True))

    @staticmethod
    def delete_artifact(artifact: TemporaryToolArtifact) -> None:
        """Delete exactly one previously discovered candidate root."""
        import shutil

        shutil.rmtree(artifact.root)

    @staticmethod
    def _write_new(path: Path, content: str) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        except FileExistsError:
            raise TemporaryToolArtifactConflict(
                "temporary tool artifact was concurrently created"
            ) from None


_FC_CONTRACT_CHECK_SOURCE = "\n".join(
    (
        "import ast, asyncio, importlib.util, inspect, json, os, py_compile, sys",
        "from pathlib import Path",
        'root = Path(os.environ.get("DEVPILOT_CONTRACT_ROOT", "/workspace/devpilot"))',
        'manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))',
        'source_path = root / manifest["entry_module"]',
        "py_compile.compile(str(source_path), doraise=True)",
        'tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))',
        "classes = [node for node in tree.body if isinstance(node, ast.ClassDef) "
        'and node.name == manifest["entry_class"]]',
        "valid = len(classes) == 1 and any("
        "(isinstance(base, ast.Name) and base.id == 'Tool') or "
        "(isinstance(base, ast.Attribute) and base.attr == 'Tool') "
        "for base in classes[0].bases)",
        "valid = valid and any("
        "isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "
        "node.name == 'execute' for node in classes[0].body)",
        "def schema_errors(value, schema, path='value'):",
        "    if not isinstance(schema, dict): return ['schema is not an object']",
        "    expected = schema.get('type')",
        "    if expected == 'object' and not isinstance(value, dict): return [path + ' must be object']",  # noqa: E501
        "    if expected == 'array' and not isinstance(value, list): return [path + ' must be array']",  # noqa: E501
        "    if expected == 'string' and not isinstance(value, str): return [path + ' must be string']",  # noqa: E501
        "    if expected == 'integer' and (not isinstance(value, int) or isinstance(value, bool)): return [path + ' must be integer']",  # noqa: E501
        "    if expected == 'number' and (not isinstance(value, (int, float)) or isinstance(value, bool)): return [path + ' must be number']",  # noqa: E501
        "    if expected == 'boolean' and not isinstance(value, bool): return [path + ' must be boolean']",  # noqa: E501
        "    if expected == 'object':",
        "        errors = [path + '.' + str(key) + ' is required' for key in schema.get('required', []) if key not in value]",  # noqa: E501
        "        for key, child in schema.get('properties', {}).items():",
        "            if key in value: errors.extend(schema_errors(value[key], child, path + '.' + key))",  # noqa: E501
        "        return errors",
        "    return []",
        "def sample(schema):",
        "    if not isinstance(schema, dict): return None",
        "    if 'default' in schema: return schema['default']",
        "    expected = schema.get('type')",
        "    if expected == 'object': return {key: sample(child) for key, child in schema.get('properties', {}).items() if key in schema.get('required', [])}",  # noqa: E501
        "    if expected == 'array': return []",
        "    if expected == 'string': return 'fixture'",
        "    if expected == 'integer': return 1",
        "    if expected == 'number': return 1.0",
        "    if expected == 'boolean': return True",
        "    return None",
        "def invalid_sample(schema):",
        "    if not isinstance(schema, dict): return None",
        "    properties = schema.get('properties', {})",
        "    required = schema.get('required', [])",
        "    if required and required[0] in properties:",
        "        child = properties[required[0]]",
        "        expected = child.get('type') if isinstance(child, dict) else None",
        "        wrong = {'string': 1, 'integer': 'wrong', 'number': 'wrong', 'boolean': 'wrong', 'array': {}, 'object': 'wrong'}.get(expected, None)",  # noqa: E501
        "        return {required[0]: wrong}",
        "    if properties:",
        "        return {next(iter(properties)): object()}",
        "    return None",
        "async def invoke(value):",
        "    result = tool.execute(**value)",
        "    return await result if inspect.isawaitable(result) else result",
        "async def invoke_checked(value):",
        "    errors = tool.validate_params(value)",
        "    if errors:",
        "        raise ValueError('controlled invalid-parameter fixture: ' + ';'.join(errors))",
        "    return await invoke(value)",
        "if valid:",
        "    sys.path.insert(0, str(root))",
        "    spec = importlib.util.spec_from_file_location('temporary_candidate', source_path)",
        "    module = importlib.util.module_from_spec(spec)",
        "    spec.loader.exec_module(module)",
        "    tool_class = getattr(module, manifest['entry_class'])",
        "    tool = tool_class.create(None)",
        "    schema = tool.to_schema()",
        "    json.dumps(schema, ensure_ascii=False, sort_keys=True)",
        "    parameter_schema = manifest['parameter_schema']",
        "    output_schema = manifest['output_schema']",
        "    if schema['function']['parameters'] != parameter_schema: raise ValueError('parameter schema mismatch')",  # noqa: E501
        "    valid_params = sample(parameter_schema)",
        "    if tool.validate_params(valid_params): raise ValueError('valid fixture rejected')",
        "    invalid_params = invalid_sample(parameter_schema)",
        "    invalid_errors = tool.validate_params(invalid_params)",
        "    if not invalid_errors: raise ValueError('invalid fixture accepted')",
        "    output = asyncio.run(invoke_checked(valid_params))",
        "    json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)",
        "    output_errors = schema_errors(output, output_schema, 'output')",
        "    if output_errors: raise ValueError('output schema mismatch: ' + ';'.join(output_errors))",  # noqa: E501
        "    controlled_error = None",
        "    try:",
        "        asyncio.run(invoke_checked(invalid_params))",
        "    except Exception as exc: controlled_error = type(exc).__name__",
        "    if controlled_error is None: raise ValueError('controlled error fixture did not fail')",  # noqa: E501
        'print(json.dumps({"ok": valid, "entry_class": manifest["entry_class"], "fixtures": {"success": True, "invalid_params": True, "controlled_error": controlled_error is not None}}, sort_keys=True))',  # noqa: E501
        "sys.exit(0 if valid else 1)",
        "",
    )
)

_FC_TOOL_SHIM_FILES = (
    ExecutionFile("nanobot/__init__.py", b""),
    ExecutionFile("nanobot/agent/__init__.py", b""),
    ExecutionFile("nanobot/agent/tools/__init__.py", b""),
    ExecutionFile(
        "nanobot/agent/tools/base.py",
     b"""import json

class Tool:
    @classmethod
    def create(cls, ctx):
        return cls()
    def to_schema(self):
        return {"type": "function", "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }}
    def validate_params(self, params):
        def check(value, schema, path='parameter'):
            if not isinstance(schema, dict): return ['invalid schema']
            expected = schema.get('type')
            if expected == 'object' and not isinstance(value, dict):
                return [path + ' must be object']
            if expected == 'string' and not isinstance(value, str):
                return [path + ' must be string']
            if expected == 'integer' and (not isinstance(value, int) or isinstance(value, bool)):
                return [path + ' must be integer']
            if expected == 'number' and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                return [path + ' must be number']
            if expected == 'boolean' and not isinstance(value, bool):
                return [path + ' must be boolean']
            if expected == 'array' and not isinstance(value, list):
                return [path + ' must be array']
            if expected == 'object':
                errors = [
                    path + '.' + str(key) + ' is required'
                    for key in schema.get('required', [])
                    if key not in value
                ]
                for key, child in schema.get('properties', {}).items():
                    if key in value: errors.extend(check(value[key], child, path + '.' + key))
                if schema.get('additionalProperties') is False:
                    errors.extend(
                        path + '.' + str(key) + ' is unexpected'
                        for key in value
                        if key not in schema.get('properties', {})
                    )
                return errors
            return []
        return check(params, self.parameters)

def tool_parameters(schema):
    def decorate(cls):
        cls.parameters = property(lambda self: dict(schema))
        return cls
    return decorate
""",
    ),
)


@dataclass(frozen=True, slots=True)
class TemporaryToolValidationResult:
    """Provider-neutral outcome of the FC pre-activation contract check."""

    status: str
    manifest: TemporaryToolManifest
    receipt: ExecutionResult | None = None
    error_code: str | None = None
    execution_run_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        receipt = self.receipt
        return {
            "validation_version": TEMPORARY_TOOL_VALIDATION_VERSION,
            "status": self.status,
            "tool_id": self.manifest.tool_id,
            "version": self.manifest.version,
            "source_hash": self.manifest.source_hash,
            "run_id": self.manifest.run_id,
            "error_code": self.error_code,
            "execution_run_id": self.execution_run_id,
            "receipt": (
                {
                    "backend": receipt.backend,
                    "sandbox_id": receipt.sandbox_id,
                    "exit_code": receipt.exit_code,
                    "stdout": receipt.stdout,
                    "stderr": receipt.stderr,
                }
                if receipt is not None
                else None
            ),
        }


class TemporaryToolValidator:
    """Run the non-importing tool contract check through the FC execution port."""

    def __init__(self, backend: ExecutionBackend) -> None:
        self._backend = backend

    def validate(self, artifact: TemporaryToolArtifact) -> TemporaryToolValidationResult:
        manifest = artifact.manifest
        request = ExecutionRequest(
            # FC run ids are single-use. Validation is invoked once explicitly
            # and once again inside activation, so every invocation needs its
            # own opaque execution id even for the same immutable artifact.
            run_id=(
                f"{manifest.run_id}:temporary-tool-validation:"
                f"{manifest.source_hash[:12]}:{uuid.uuid4().hex}"
            ),
            project_id=manifest.project_id,
            files=(
                ExecutionFile(manifest.entry_module, artifact.source_path.read_bytes()),
                ExecutionFile(
                    "manifest.json",
                    json.dumps(
                        manifest.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                ),
                ExecutionFile("contract_check.py", _FC_CONTRACT_CHECK_SOURCE.encode("utf-8")),
                *_FC_TOOL_SHIM_FILES,
            ),
            command="python /workspace/devpilot/contract_check.py",
            timeout_seconds=300,
            network_allowed=False,
            command_profile="temporary-tool-contract",
        )
        try:
            receipt = self._backend.run(request)
        except ExecutionError as exc:
            result = TemporaryToolValidationResult(
                status=TemporaryToolState.REJECTED.value,
                manifest=manifest,
                error_code=exc.code,
                execution_run_id=request.run_id,
            )
        else:
            result = TemporaryToolValidationResult(
                status=(
                    TemporaryToolState.VALIDATED.value
                    if receipt.exit_code == 0
                    else TemporaryToolState.REJECTED.value
                ),
                manifest=manifest,
                receipt=receipt,
                error_code=None if receipt.exit_code == 0 else "TEMPORARY_TOOL_CONTRACT_FAILED",
                execution_run_id=request.run_id,
            )
        try:
            TemporaryToolArtifactStore(artifact.root).write_validation_receipt(artifact, result)
        except OSError:
            # A validation without a durable receipt must not be promoted.
            return TemporaryToolValidationResult(
                status=TemporaryToolState.REJECTED.value,
                manifest=manifest,
                receipt=result.receipt,
                error_code="TEMPORARY_TOOL_RECEIPT_PERSIST_FAILED",
                execution_run_id=request.run_id,
            )
        return result


def _trusted_temporary_confirmation_attributes(
    request_context: Any | None = None,
) -> dict[str, Any] | None:
    """Read a gateway-owned confirmation envelope, never model arguments."""

    try:
        context = current_request_context()
        value = context.attributes.get("devpilot_confirmation") if context is not None else None
        if isinstance(value, dict):
            return value
    except (ImportError, TypeError, ValueError):
        pass
    value = getattr(request_context, "attributes", {}).get("devpilot_confirmation")
    if isinstance(value, dict):
        return value
    try:
        from nanobot.bus.events import current_inbound_attributes

        value = current_inbound_attributes().get("devpilot_confirmation")
        return value if isinstance(value, dict) else None
    except (ImportError, TypeError, ValueError):
        return None


def _temporary_confirmation_payload(
    *, action: str, binding: Mapping[str, str], context: Mapping[str, object]
) -> str:
    return "\n".join(
        (
            "temporary-tool-confirmation-v1",
            action,
            binding["session_id"],
            binding["run_id"],
            binding["project_id"],
            binding["tool_id"],
            binding["version"],
            binding["source_hash"],
            str(context["capability"]),
            str(context["confirmation_id"]),
            str(context["issued_at"]),
            str(context["expires_at"]),
            str(context["user_event_nonce"]),
        )
    )


def _consume_temporary_confirmation(
    request: ToolContext,
    *,
    action: str,
    binding: Mapping[str, str],
    capability: str,
) -> bool:
    """Verify and consume one gateway-signed, scope-bound confirmation."""

    envelope = _trusted_temporary_confirmation_attributes(request)
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"action", "context", "verifier"}
        or envelope.get("action") != action
    ):
        return False
    verifier = envelope.get("verifier")
    context = envelope.get("context")
    if not callable(verifier) or not isinstance(context, Mapping):
        return False
    expected = {
        "session_id",
        "run_id",
        "project_id",
        "tool_id",
        "version",
        "source_hash",
        "capability",
        "confirmation_id",
        "issued_at",
        "expires_at",
        "user_event_nonce",
        "signature",
    }
    if set(context) != expected:
        return False
    if any(context.get(key) != value for key, value in binding.items()):
        return False
    if context.get("capability") != capability:
        return False
    confirmation_id = context.get("confirmation_id")
    signature = context.get("signature")
    issued_at = context.get("issued_at")
    expires_at = context.get("expires_at")
    nonce = context.get("user_event_nonce")
    if not all(
        isinstance(value, str)
        and value
        and len(value) <= 256
        and not any(ord(char) < 0x20 for char in value)
        for value in (confirmation_id, signature, issued_at, expires_at, nonce)
    ):
        return False
    if not _SHA256.fullmatch(signature):
        return False
    try:
        issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        if issued.tzinfo is None or expires.tzinfo is None:
            return False
        issued = issued.astimezone(UTC)
        expires = expires.astimezone(UTC)
        if (
            expires <= issued
            or (expires - issued).total_seconds() > 300
            or not (issued <= now < expires)
        ):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    payload = _temporary_confirmation_payload(
        action=action,
        binding=binding,
        context=context,
    )
    try:
        if not verifier(payload, signature):
            return False
    except Exception:
        return False
    with _TEMPORARY_CONFIRMATION_LOCK:
        if confirmation_id in _CONSUMED_TEMPORARY_CONFIRMATIONS:
            return False
        _CONSUMED_TEMPORARY_CONFIRMATIONS.add(confirmation_id)
    return True


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "minLength": 1, "maxLength": 4096},
            "requested_capability": {"type": "string", "minLength": 1, "maxLength": 256},
            "existing_tools": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[a-z][a-z0-9._-]{0,63}$"},
                "maxItems": 256,
            },
            "suggested_tool_id": {"type": ["string", "null"]},
        },
        "required": ["reason", "requested_capability"],
        "additionalProperties": False,
    }
)
class TemporaryToolGapReportTool(Tool):
    """Record why the current registered tools cannot complete the task."""

    _scopes = {"core"}

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path(
            os.environ.get("DEVPILOT_DATA_DIR", str(Path.home() / ".nanobot-skill-improvement"))
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> TemporaryToolGapReportTool:
        del ctx
        return cls()

    @property
    def name(self) -> str:
        return "devpilot_report_tool_gap"

    @property
    def description(self) -> str:
        return "Record a redacted capability gap before generating a temporary Tool candidate."

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        scope = current_workspace_scope()
        request = current_request_context()
        if scope is None or request is None or not temporary_tools_allowed(
            scope.access_mode,
            source_channel=scope.source_channel,
            restrict_to_workspace=scope.restrict_to_workspace,
        ):
            return ToolResult.error("TEMPORARY_TOOL_FULL_ACCESS_REQUIRED")
        session_id = request.session_key or request.chat_id
        run_id = request.attributes.get("devpilot_run_id") or request.attributes.get(
            "devpilot_task_id"
        )
        if not isinstance(run_id, str) or not run_id:
            return ToolResult.error("TEMPORARY_TOOL_RUN_MISSING")
        project_id = request.attributes.get("devpilot_project_id")
        if not isinstance(project_id, str) or not project_id:
            project_id = hashlib.sha256(str(scope.project_path).encode("utf-8")).hexdigest()[:16]
        existing = kwargs.get("existing_tools", [])
        if not isinstance(existing, list) or any(not isinstance(item, str) for item in existing):
            return ToolResult.error("TEMPORARY_TOOL_GAP_INVALID")
        try:
            gap = ToolCapabilityGap(
                session_id=session_id,
                run_id=run_id,
                project_id=project_id,
                reason=kwargs["reason"],
                requested_capability=kwargs["requested_capability"],
                existing_tools=tuple(existing),
                suggested_tool_id=kwargs.get("suggested_tool_id"),
            )
            path = ToolCapabilityGapStore(self._data_dir).write(gap)
        except (KeyError, TemporaryToolContractError, OSError, TypeError, ValueError) as exc:
            return ToolResult.error(f"TEMPORARY_TOOL_GAP_INVALID: {type(exc).__name__}")
        return json.dumps(
            {"ok": True, "state": gap.status, "gap": gap.to_dict(), "record": str(path)}
        )


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "tool_id": {"type": "string", "pattern": "^[a-z][a-z0-9._-]{0,63}$"},
            "version": {"type": "string", "minLength": 1, "maxLength": 128},
            "source_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": ["tool_id", "version", "source_hash"],
        "additionalProperties": False,
    }
)
class TemporaryToolValidateTool(Tool):
    """Validate one generated artifact through the configured FC backend."""

    _scopes = {"core"}

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path(
            os.environ.get("DEVPILOT_DATA_DIR", str(Path.home() / ".nanobot-skill-improvement"))
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> TemporaryToolValidateTool:
        del ctx
        return cls()

    @property
    def name(self) -> str:
        return "devpilot_validate_temporary_tool"

    @property
    def description(self) -> str:
        return "Validate a temporary Tool artifact in FC before it can be activated."

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        scope = current_workspace_scope()
        if scope is None or not temporary_tools_allowed(
            scope.access_mode,
            source_channel=scope.source_channel,
            restrict_to_workspace=scope.restrict_to_workspace,
        ):
            return ToolResult.error(
                "TEMPORARY_TOOL_FULL_ACCESS_REQUIRED: temporary tools require Full Access"
            )
        request = current_request_context()
        if request is None or not request.sender_id:
            return ToolResult.error("TEMPORARY_TOOL_CONTEXT_MISSING: no active WebUI request")
        session_id = request.session_key or request.chat_id
        run_id = request.attributes.get("devpilot_run_id")
        if not isinstance(run_id, str) or not run_id:
            run_id = request.attributes.get("devpilot_task_id")
        try:
            tool_id = kwargs["tool_id"]
            version = kwargs["version"]
            source_hash = kwargs["source_hash"]
            if not all(
                isinstance(value, str) and value
                for value in (tool_id, version, source_hash)
            ):
                raise TemporaryToolContractError("tool_id, version and source_hash are required")
            if not isinstance(run_id, str) or not run_id:
                raise TemporaryToolContractError("no active Run binding")
            artifact = TemporaryToolArtifactStore(self._data_dir).load_for_session(
                session_id=session_id,
                run_id=run_id,
                tool_id=tool_id,
                version=version,
                source_hash=source_hash,
            )
            store = TemporaryToolArtifactStore(self._data_dir)
            lifecycle = store.load_lifecycle(artifact)
            if lifecycle.state is TemporaryToolState.GENERATED:
                store.transition_lifecycle(artifact, TemporaryToolState.VALIDATING)
            result = TemporaryToolValidator(
                build_fc_backend_from_environment()
            ).validate(artifact)
            lifecycle = store.load_lifecycle(artifact)
            if lifecycle.state is TemporaryToolState.VALIDATING:
                store.transition_lifecycle(
                    artifact,
                    TemporaryToolState.VALIDATED
                    if result.status == TemporaryToolState.VALIDATED.value
                    else TemporaryToolState.REJECTED,
                )
            return json.dumps(
                {"ok": result.status == "validated", **result.to_dict()},
                ensure_ascii=False,
            )
        except (TemporaryToolContractError, ExecutionError, OSError, TypeError, ValueError) as exc:
            return ToolResult.error(f"TEMPORARY_TOOL_VALIDATION_FAILED: {type(exc).__name__}")


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "tool_id": {"type": "string", "pattern": "^[a-z][a-z0-9._-]{0,63}$"},
            "version": {"type": "string", "minLength": 1, "maxLength": 128},
            "source_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": ["tool_id", "version", "source_hash"],
        "additionalProperties": False,
    }
)
class TemporaryToolActivateTool(Tool):
    """Revalidate and hot-load a temporary Tool into the current Run."""

    _scopes = {"core"}

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path(
            os.environ.get("DEVPILOT_DATA_DIR", str(Path.home() / ".nanobot-skill-improvement"))
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> TemporaryToolActivateTool:
        del ctx
        return cls()

    @property
    def name(self) -> str:
        return "devpilot_activate_temporary_tool"

    @property
    def description(self) -> str:
        return (
            "Revalidate a generated temporary Tool in FC and hot-load it into the current Run. "
            "Activation is session-scoped and does not require an agent restart."
        )

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        scope = current_workspace_scope()
        if scope is None or not temporary_tools_allowed(
            scope.access_mode,
            source_channel=scope.source_channel,
            restrict_to_workspace=scope.restrict_to_workspace,
        ):
            return ToolResult.error(
                "TEMPORARY_TOOL_FULL_ACCESS_REQUIRED: temporary tools require Full Access"
            )
        request = current_request_context()
        manager = _TEMPORARY_PLUGIN_MANAGER
        if request is None or not request.sender_id:
            return ToolResult.error("TEMPORARY_TOOL_CONTEXT_MISSING: no active WebUI request")
        if manager is None:
            return ToolResult.error(
                "TEMPORARY_TOOL_MANAGER_UNAVAILABLE: runtime manager is not bound"
            )
        session_id = request.session_key or request.chat_id
        run_id = request.attributes.get("devpilot_run_id")
        if not isinstance(run_id, str) or not run_id:
            run_id = request.attributes.get("devpilot_task_id")
        try:
            tool_id = kwargs["tool_id"]
            version = kwargs["version"]
            source_hash = kwargs["source_hash"]
            if not all(
                isinstance(value, str) and value
                for value in (tool_id, version, source_hash)
            ):
                raise TemporaryToolContractError("tool_id, version and source_hash are required")
            if not isinstance(run_id, str) or not run_id:
                raise TemporaryToolContractError("no active Run binding")
            artifact = TemporaryToolArtifactStore(self._data_dir).load_for_session(
                session_id=session_id,
                run_id=run_id,
                tool_id=tool_id,
                version=version,
                source_hash=source_hash,
            )
            if artifact.manifest.expires_at <= int(time.time()):
                raise TemporaryToolContractError("temporary tool artifact has expired")
            sensitive_capabilities = _SENSITIVE_CAPABILITIES
            if sensitive_capabilities.intersection(artifact.manifest.declared_capabilities):
                if not _temporary_tool_confirmation_matches(request, artifact):
                    return json.dumps(
                        {
                            "ok": False,
                            "code": "USER_CONFIRMATION_REQUIRED",
                            "requires_user_confirmation": True,
                            "message": (
                                "该临时工具声明会读取隐私数据或向外发送数据，"
                                "需要针对本工具和本次 Run 的明确确认。"
                            ),
                        },
                        ensure_ascii=False,
                    )
            store = TemporaryToolArtifactStore(self._data_dir)
            lifecycle = store.load_lifecycle(artifact)
            if lifecycle.state is TemporaryToolState.GENERATED:
                store.transition_lifecycle(artifact, TemporaryToolState.VALIDATING)
            validation = TemporaryToolValidator(
                build_fc_backend_from_environment()
            ).validate(artifact)
            lifecycle = store.load_lifecycle(artifact)
            if lifecycle.state is TemporaryToolState.VALIDATING:
                store.transition_lifecycle(
                    artifact,
                    TemporaryToolState.VALIDATED
                    if validation.status == TemporaryToolState.VALIDATED.value
                    else TemporaryToolState.REJECTED,
                )
            if validation.status != TemporaryToolState.VALIDATED.value:
                return json.dumps(
                    {
                        "ok": False,
                        "state": TemporaryToolState.REJECTED.value,
                        **validation.to_dict(),
                    },
                    ensure_ascii=False,
                )
            manifest = PluginManifest.from_mapping(
                {
                    "plugin_id": artifact.manifest.tool_id,
                    "version": artifact.manifest.version,
                    "content_hash": artifact.manifest.source_hash,
                    "kind": PluginKind.TOOL.value,
                    "api_version": "devpilot.temporary/v1",
                    "scope": "session",
                    "entry": artifact.manifest.entry_module,
                    "requires": [],
                    "provides": [f"tool:{artifact.manifest.name}"],
                    # The Full Access gate is enforced above and the richer
                    # model-authored capability declaration stays in the
                    # temporary manifest sidecar for audit/reporting.
                    "requested_capabilities": [],
                    "execution_mode": PluginExecutionMode.IN_PROCESS.value,
                }
            )
            record = PluginRecord(
                manifest,
                PluginSource.TEMPORARY,
                session_id=session_id,
                project_id=artifact.manifest.project_id,
                run_id=run_id,
            )
            manager.authorize(session_id)
            result = manager.register_temporary_version(record, artifact.root)
            if (
                not result.ok
                and result.code is not None
                and result.code.value != "duplicate_version"
            ):
                lifecycle = store.load_lifecycle(artifact)
                if lifecycle.state is TemporaryToolState.VALIDATED:
                    store.transition_lifecycle(artifact, TemporaryToolState.REJECTED)
                return json.dumps({"ok": False, **result.to_dict()}, ensure_ascii=False)
            activated = manager.activate_temporary(
                artifact.manifest.tool_id,
                artifact.manifest.version,
                session_id=session_id,
                run_id=run_id,
                project_id=artifact.manifest.project_id,
            )
            payload = activated.to_dict()
            if activated.ok:
                lifecycle = store.load_lifecycle(artifact)
                if lifecycle.state is TemporaryToolState.VALIDATED:
                    store.transition_lifecycle(artifact, TemporaryToolState.ACTIVE)
            else:
                # Activation failure must not leave a registered candidate or
                # a misleading validated state behind.
                manager.remove(
                    artifact.manifest.tool_id,
                    artifact.manifest.version,
                    session_id=session_id,
                    operation_details={"temporary_activation": "rollback"},
                )
                lifecycle = store.load_lifecycle(artifact)
                if lifecycle.state is TemporaryToolState.VALIDATED:
                    store.transition_lifecycle(artifact, TemporaryToolState.REJECTED)
            payload.update(
                {
                    "state": TemporaryToolState.ACTIVE.value if activated.ok else "rejected",
                    "source_hash": artifact.manifest.source_hash,
                    "tool": artifact.manifest.name,
                    "fc_validation": validation.to_dict(),
                    "restart_required": False,
                }
            )
            return json.dumps(payload, ensure_ascii=False)
        except (TemporaryToolContractError, ExecutionError, OSError, TypeError, ValueError) as exc:
            return ToolResult.error(f"TEMPORARY_TOOL_ACTIVATION_FAILED: {type(exc).__name__}")


class TemporaryToolEnsureTool(Tool):
    """Run gap completion, FC validation and activation as one tool call."""

    _scopes = {"core"}

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir

    @classmethod
    def create(cls, ctx: ToolContext) -> TemporaryToolEnsureTool:
        del ctx
        return cls()

    @property
    def name(self) -> str:
        return "devpilot_ensure_temporary_tool"

    @property
    def description(self) -> str:
        return (
            "When the current tool set cannot complete the task, generate a temporary Tool, "
            "validate it in FC, hot-load it into this Run, and return the new tool name."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return TemporaryToolCreateTool(self._data_dir).parameters

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        gap = await TemporaryToolGapReportTool(self._data_dir).execute(
            reason="现有工具集合不足以完成当前任务；已确认需要临时工具并继续隔离验证。",
            requested_capability=kwargs.get("name", "temporary_tool"),
            existing_tools=[],
            suggested_tool_id=kwargs.get("tool_id"),
        )
        if isinstance(gap, ToolResult) and gap.is_error:
            return gap
        created = await TemporaryToolCreateTool(self._data_dir).execute(**kwargs)
        if isinstance(created, ToolResult) and created.is_error:
            return created
        try:
            created_payload = json.loads(str(created))
            if not isinstance(created_payload, dict) or not created_payload.get("ok"):
                return ToolResult.error("TEMPORARY_TOOL_CHAIN_CREATE_FAILED")
            validation = await TemporaryToolValidateTool(self._data_dir).execute(
                tool_id=created_payload["tool_id"],
                version=created_payload["version"],
                source_hash=created_payload["source_hash"],
            )
            if isinstance(validation, ToolResult) and validation.is_error:
                return validation
            validation_payload = json.loads(str(validation))
            if not isinstance(validation_payload, dict) or not validation_payload.get("ok"):
                return json.dumps(
                    {
                        "ok": False,
                        "state": TemporaryToolState.REJECTED.value,
                        "validation": validation_payload,
                    },
                    ensure_ascii=False,
                )
            activated = await TemporaryToolActivateTool(self._data_dir).execute(
                tool_id=created_payload["tool_id"],
                version=created_payload["version"],
                source_hash=created_payload["source_hash"],
            )
            if isinstance(activated, ToolResult) and activated.is_error:
                return activated
            activation_payload = json.loads(str(activated))
            if not isinstance(activation_payload, dict) or not activation_payload.get("ok"):
                return json.dumps(
                    {
                        "ok": False,
                        "state": TemporaryToolState.REJECTED.value,
                        "tool_id": created_payload["tool_id"],
                        "version": created_payload["version"],
                        "activation": activation_payload,
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ok": True,
                    "state": TemporaryToolState.ACTIVE.value,
                    "tool_id": created_payload["tool_id"],
                    "version": created_payload["version"],
                    "source_hash": created_payload["source_hash"],
                    "tool": kwargs["name"],
                    "restart_required": False,
                    "stages": {
                        "create": "generated",
                        "validate": "validated",
                        "activate": "active",
                    },
                    "activation": activation_payload,
                },
                ensure_ascii=False,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return ToolResult.error("TEMPORARY_TOOL_CHAIN_RESPONSE_INVALID")


class TemporaryToolPersistTool(Tool):
    """Persist the most recent user-approved temporary snapshot for a project."""

    _scopes = {"core"}

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path(
            os.environ.get("DEVPILOT_DATA_DIR", str(Path.home() / ".nanobot-skill-improvement"))
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> TemporaryToolPersistTool:
        del ctx
        return cls()

    @property
    def name(self) -> str:
        return "devpilot_persist_temporary_tool"

    @property
    def description(self) -> str:
        return (
            "After the user explicitly agrees, save the pending temporary Tool "
            "as a reusable project plugin."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        del kwargs
        scope = current_workspace_scope()
        manager = _TEMPORARY_PLUGIN_MANAGER
        request = current_request_context()
        if scope is None or not temporary_tools_allowed(
            scope.access_mode,
            source_channel=scope.source_channel,
            restrict_to_workspace=scope.restrict_to_workspace,
        ):
            return ToolResult.error("TEMPORARY_TOOL_FULL_ACCESS_REQUIRED")
        if request is None or not request.sender_id or manager is None:
            return ToolResult.error("TEMPORARY_TOOL_CONTEXT_MISSING")
        session_id = request.session_key or request.chat_id
        project_id = request.attributes.get("devpilot_project_id")
        if not isinstance(project_id, str) or not project_id:
            project_id = manager.project_id_for_path(scope.project_path)
        pending = TemporaryToolArtifactStore(self._data_dir).pending_for_session(session_id)
        if not pending:
            return ToolResult.error("TEMPORARY_TOOL_PENDING_NOT_FOUND")
        artifact = pending[0]
        store = TemporaryToolArtifactStore(self._data_dir)
        try:
            binding = {
                "session_id": artifact.manifest.session_id,
                "run_id": artifact.manifest.run_id,
                "project_id": project_id,
                "tool_id": artifact.manifest.tool_id,
                "version": artifact.manifest.version,
                "source_hash": artifact.manifest.source_hash,
            }
            if not _consume_temporary_confirmation(
                request,
                action="temporary_tool_persist",
                binding=binding,
                capability="persist_temporary_tool",
            ):
                return json.dumps(
                    {
                        "ok": False,
                        "code": "USER_CONFIRMATION_REQUIRED",
                        "requires_user_confirmation": True,
                        "message": "保存临时工具需要针对本工具、项目和本次 Run 的明确确认。",
                    },
                    ensure_ascii=False,
                )
            if store.load_lifecycle(artifact).state is not TemporaryToolState.PERSISTENCE_REQUESTED:
                return json.dumps(
                    {
                        "ok": False,
                        "code": "TEMPORARY_TOOL_PERSISTENCE_NOT_REQUESTED",
                    },
                    ensure_ascii=False,
                )
            validation = TemporaryToolValidator(
                build_fc_backend_from_environment()
            ).validate(artifact)
            if validation.status != TemporaryToolState.VALIDATED.value:
                return json.dumps(
                    {
                        "ok": False,
                        "code": "TEMPORARY_TOOL_REVALIDATION_FAILED",
                        **validation.to_dict(),
                    },
                    ensure_ascii=False,
                )
            lifecycle = store.load_lifecycle(artifact)
            if lifecycle.state in {
                TemporaryToolState.GENERATED,
                TemporaryToolState.VALIDATED,
                TemporaryToolState.ACTIVE,
            }:
                store.transition_lifecycle(artifact, TemporaryToolState.RETIRING)
            lifecycle = store.load_lifecycle(artifact)
            if lifecycle.state in {
                TemporaryToolState.RETIRING,
                TemporaryToolState.CLEANUP_PENDING,
            }:
                store.transition_lifecycle(artifact, TemporaryToolState.PERSISTENCE_REQUESTED)
            root = (
                self._data_dir
                / "persistent-tools"
                / project_id
                / artifact.manifest.tool_id
                / artifact.manifest.version
                / artifact.manifest.source_hash
            )
            root.mkdir(parents=True, exist_ok=True)
            destination = root / artifact.manifest.entry_module
            manifest_destination = root / "manifest.json"
            source_bytes = artifact.source_path.read_bytes()
            manifest_bytes = json.dumps(
                artifact.manifest.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if (
                destination.exists()
                and destination.read_bytes() != source_bytes
            ):
                raise TemporaryToolArtifactConflict(
                    "persistent artifact already contains other content"
                )
            if not destination.exists():
                temporary_destination = destination.with_suffix(destination.suffix + ".tmp")
                temporary_destination.write_bytes(source_bytes)
                os.replace(temporary_destination, destination)
            if (
                manifest_destination.exists()
                and manifest_destination.read_bytes() != manifest_bytes
            ):
                raise TemporaryToolArtifactConflict(
                    "persistent manifest already contains other content"
                )
            if not manifest_destination.exists():
                temporary_manifest = manifest_destination.with_suffix(".json.tmp")
                temporary_manifest.write_bytes(manifest_bytes)
                os.replace(temporary_manifest, manifest_destination)
            manifest = PluginManifest.from_mapping(
                {
                    "plugin_id": artifact.manifest.tool_id,
                    "version": artifact.manifest.version,
                    "content_hash": artifact.manifest.source_hash,
                    "kind": PluginKind.TOOL.value,
                    "api_version": "devpilot.temporary/v1",
                    "scope": "project",
                    "entry": artifact.manifest.entry_module,
                    "requires": [],
                    "provides": [f"tool:{artifact.manifest.name}"],
                    "requested_capabilities": [],
                    "execution_mode": PluginExecutionMode.IN_PROCESS.value,
                }
            )
            result = manager.register_persisted_version(
                PluginRecord(manifest, PluginSource.PERSISTED, project_id=project_id),
                root,
            )
            if (
                not result.ok
                and result.code is not None
                and result.code.value != "duplicate_version"
            ):
                return json.dumps({"ok": False, **result.to_dict()}, ensure_ascii=False)
            manager.authorize("", project_id)
            loaded = manager.load(
                artifact.manifest.tool_id,
                artifact.manifest.version,
                session_id=session_id,
                project_id=project_id,
            )
            if not loaded.ok:
                return json.dumps(
                    {"ok": False, "code": "TEMPORARY_TOOL_PERSIST_LOAD_FAILED", **loaded.to_dict()},
                    ensure_ascii=False,
                )
            store.transition_lifecycle(artifact, TemporaryToolState.PERSISTED)
            store.delete_artifact(artifact)
            return json.dumps(
                {
                    "ok": loaded.ok,
                    "state": TemporaryToolState.PERSISTED.value if loaded.ok else "rejected",
                    "tool_id": artifact.manifest.tool_id,
                    "version": artifact.manifest.version,
                    "project_id": project_id,
                    "result": loaded.to_dict(),
                },
                ensure_ascii=False,
            )
        except (OSError, TypeError, ValueError, TemporaryToolContractError) as exc:
            return ToolResult.error(f"TEMPORARY_TOOL_PERSIST_FAILED: {type(exc).__name__}")


class TemporaryToolDiscardTool(Tool):
    """Discard all expired/pending temporary snapshots after the user declines."""

    _scopes = {"core"}

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path(
            os.environ.get("DEVPILOT_DATA_DIR", str(Path.home() / ".nanobot-skill-improvement"))
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> TemporaryToolDiscardTool:
        del ctx
        return cls()

    @property
    def name(self) -> str:
        return "devpilot_discard_temporary_tools"

    @property
    def description(self) -> str:
        return "Discard pending temporary Tool snapshots after the user declines persistence."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        del kwargs
        scope = current_workspace_scope()
        request = current_request_context()
        if scope is None or not temporary_tools_allowed(
            scope.access_mode,
            source_channel=scope.source_channel,
            restrict_to_workspace=scope.restrict_to_workspace,
        ):
            return ToolResult.error("TEMPORARY_TOOL_FULL_ACCESS_REQUIRED")
        if request is None or not request.sender_id:
            return ToolResult.error("TEMPORARY_TOOL_CONTEXT_MISSING")
        session_id = request.session_key or request.chat_id
        store = TemporaryToolArtifactStore(self._data_dir)
        pending = store.pending_for_session(session_id)
        removed = 0
        for artifact in pending:
            try:
                lifecycle = store.load_lifecycle(artifact)
                if lifecycle.state in {
                    TemporaryToolState.GENERATED,
                    TemporaryToolState.VALIDATED,
                    TemporaryToolState.ACTIVE,
                }:
                    store.transition_lifecycle(artifact, TemporaryToolState.RETIRING)
                lifecycle = store.load_lifecycle(artifact)
                if lifecycle.state in {
                    TemporaryToolState.RETIRING,
                    TemporaryToolState.CLEANUP_PENDING,
                    TemporaryToolState.PERSISTENCE_REQUESTED,
                }:
                    store.transition_lifecycle(artifact, TemporaryToolState.DISPOSED)
                store.delete_artifact(artifact)
            except OSError:
                continue
            removed += 1
        return json.dumps(
            {"ok": True, "state": TemporaryToolState.DISPOSED.value, "removed": removed},
            ensure_ascii=False,
        )


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "tool_id": {"type": "string", "pattern": "^[a-z][a-z0-9._-]{0,63}$"},
            "version": {"type": "string", "minLength": 1, "maxLength": 128},
            "name": {"type": "string", "pattern": "^[a-z][a-z0-9._-]{0,63}$"},
            "description": {"type": "string", "minLength": 1, "maxLength": 4096},
            "entry_class": {
                "type": "string",
                "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$",
            },
            "source": {"type": "string", "minLength": 1, "maxLength": 262144},
            "parameter_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "declared_capabilities": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "maxItems": 16,
            },
            "expires_in_seconds": {
                "type": "integer",
                "minimum": 60,
                "maximum": 86400,
            },
        },
        "required": [
            "tool_id",
            "version",
            "name",
            "description",
            "entry_class",
            "source",
            "parameter_schema",
            "output_schema",
        ],
        "additionalProperties": False,
    }
)
class TemporaryToolCreateTool(Tool):
    """Create a frozen candidate artifact; never imports or executes its source."""

    _scopes = {"core"}

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path(
            os.environ.get("DEVPILOT_DATA_DIR", str(Path.home() / ".nanobot-skill-improvement"))
        )

    @classmethod
    def create(cls, ctx: ToolContext) -> TemporaryToolCreateTool:
        del ctx
        return cls()

    @property
    def name(self) -> str:
        return "devpilot_create_temporary_tool"

    @property
    def description(self) -> str:
        return (
            "Create a Full Access, session-scoped Python Tool candidate artifact. "
            "The source is only syntax/protocol checked here and must pass FC "
            "validation before it can be activated."
        )

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        scope = current_workspace_scope()
        if scope is None or not temporary_tools_allowed(
            scope.access_mode,
            source_channel=scope.source_channel,
            restrict_to_workspace=scope.restrict_to_workspace,
        ):
            return ToolResult.error(
                "TEMPORARY_TOOL_FULL_ACCESS_REQUIRED: temporary tools require Full Access"
            )
        request = current_request_context()
        if request is None or not request.sender_id:
            return ToolResult.error("TEMPORARY_TOOL_CONTEXT_MISSING: no active WebUI request")
        session_id = request.session_key or request.chat_id
        run_id = request.attributes.get("devpilot_run_id")
        if not isinstance(run_id, str) or not run_id:
            run_id = request.attributes.get("devpilot_task_id")
        if not isinstance(run_id, str) or not run_id:
            return ToolResult.error("TEMPORARY_TOOL_RUN_MISSING: no active Run binding")
        project_id = request.attributes.get("devpilot_project_id")
        if not isinstance(project_id, str) or not project_id:
            project_id = hashlib.sha256(str(scope.project_path).encode("utf-8")).hexdigest()[:16]
        try:
            if not ToolCapabilityGapStore(self._data_dir).has_for_run(
                session_id=session_id, run_id=run_id
            ):
                return ToolResult.error(
                    "TEMPORARY_TOOL_GAP_REQUIRED: report the missing capability before creation"
                )
            source = kwargs.get("source")
            entry_class = kwargs.get("entry_class")
            if not isinstance(source, str) or not isinstance(entry_class, str):
                raise TemporaryToolContractError("source and entry_class are required")
            expires_in = kwargs.get("expires_in_seconds", 3600)
            if not isinstance(expires_in, int) or isinstance(expires_in, bool):
                raise TemporaryToolContractError("expires_in_seconds must be an integer")
            capabilities = kwargs.get("declared_capabilities", [])
            if not isinstance(capabilities, list):
                raise TemporaryToolContractError("declared_capabilities must be a list")
            now = int(time.time())
            manifest = TemporaryToolManifest.from_mapping(
                {
                    "tool_id": kwargs.get("tool_id"),
                    "version": kwargs.get("version"),
                    "session_id": session_id,
                    "run_id": run_id,
                    "project_id": project_id,
                    "name": kwargs.get("name"),
                    "description": kwargs.get("description"),
                    "parameter_schema": kwargs.get("parameter_schema"),
                    "output_schema": kwargs.get("output_schema"),
                    "entry_module": "candidate.py",
                    "entry_class": entry_class,
                    "source_hash": source_sha256(source),
                    "declared_capabilities": capabilities,
                    "created_at": now,
                    "expires_at": now + expires_in,
                    "scope": "session",
                }
            )
            artifact = TemporaryToolArtifactStore(self._data_dir).write(manifest, source)
            return json.dumps(
                {
                    "ok": True,
                    "state": TemporaryToolState.GENERATED.value,
                    "tool_id": manifest.tool_id,
                    "version": manifest.version,
                    "source_hash": manifest.source_hash,
                    "session_id": manifest.session_id,
                    "run_id": manifest.run_id,
                    "artifact": artifact.root.name,
                    "next_step": "FC validation is required before activation",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        except (TemporaryToolContractError, OSError, TypeError, ValueError) as exc:
            return ToolResult.error(f"TEMPORARY_TOOL_DEFINITION_INVALID: {type(exc).__name__}")


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
    status: str = "gap_detected"
    created_at: int = field(default_factory=lambda: int(time.time()))

    def __post_init__(self) -> None:
        _required_binding(self.session_id, "session_id")
        _required_binding(self.run_id, "run_id")
        _required_binding(self.project_id, "project_id")
        _required_text(self.reason, "reason")
        _required_text(self.requested_capability, "requested_capability")
        if any(_ID.fullmatch(name) is None for name in self.existing_tools):
            raise TemporaryToolContractError("existing_tools contains an invalid tool name")
        if self.suggested_tool_id is not None:
            _required_id(self.suggested_tool_id, "suggested_tool_id")
        if self.status != TemporaryToolState.GAP_DETECTED.value:
            raise TemporaryToolContractError("capability gap must start in gap_detected state")
        if self.created_at < 0:
            raise TemporaryToolContractError("capability gap created_at must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "reason": _redact_gap_text(self.reason),
            "requested_capability": _redact_gap_text(self.requested_capability),
            "existing_tools": [_redact_gap_text(item) for item in self.existing_tools],
            "suggested_tool_id": self.suggested_tool_id,
            "status": self.status,
            "created_at": self.created_at,
        }


_GAP_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(?P<prefix>['\"\\]?(?:bearer|api[_ -]?key|token|secret|password|"
        r"authorization|cookie)[^A-Za-z0-9]{0,12}[:=]\s*['\"\\]?)"
        r"(?P<value>[A-Za-z0-9._~+/=-]+)"
    ),
    re.compile(r"(?i)-----BEGIN [^-]+-----.*?-----END [^-]+-----"),
)


def _redact_gap_text(value: str) -> str:
    """Bound gap diagnostics and remove common credential-shaped values."""
    redacted = value
    for pattern in _GAP_SECRET_PATTERNS:
        if "prefix" in pattern.groupindex:
            redacted = pattern.sub(
                lambda match: f"{match.group('prefix')}[REDACTED]", redacted
            )
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted[:4096]


class ToolCapabilityGapStore:
    """Persist only the redacted explanation that preceded candidate creation."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()

    def _root(self, gap: ToolCapabilityGap) -> Path:
        session_hash = hashlib.sha256(gap.session_id.encode("utf-8")).hexdigest()[:16]
        return self.data_dir / "temporary-tools" / "gaps" / session_hash / gap.run_id

    def write(self, gap: ToolCapabilityGap) -> Path:
        root = self._root(gap)
        root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            gap.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        path = root / (hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24] + ".json")
        if path.exists() and path.read_text(encoding="utf-8") != payload:
            raise TemporaryToolArtifactConflict(
                "capability gap record already contains other content"
            )
        if not path.exists():
            TemporaryToolArtifactStore._write_new(path, payload)
        return path

    def has_for_run(self, *, session_id: str, run_id: str) -> bool:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        root = self.data_dir / "temporary-tools" / "gaps" / session_hash / run_id
        return root.is_dir() and any(root.glob("*.json"))


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
        _required_id(self.name, "name")
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
        if self.history[-1] is not self.state:
            raise TemporaryToolContractError("lifecycle state must match its history tail")
        for previous, target in zip(self.history, self.history[1:], strict=False):
            if target not in _TRANSITIONS[previous]:
                raise TemporaryToolContractError(
                    "invalid persisted temporary tool transition: "
                    f"{previous.value} -> {target.value}"
                )

    @classmethod
    def from_mapping(cls, raw: object) -> TemporaryToolLifecycle:
        if not isinstance(raw, Mapping):
            raise TemporaryToolContractError("temporary tool lifecycle must be an object")
        raw_history = raw.get("history")
        if not isinstance(raw_history, list) or not raw_history:
            raise TemporaryToolContractError("temporary tool lifecycle history is required")
        try:
            history = [TemporaryToolState(value) for value in raw_history]
            state = TemporaryToolState(raw.get("state"))
        except (TypeError, ValueError) as exc:
            raise TemporaryToolContractError("temporary tool lifecycle state is invalid") from exc
        if state is not history[-1]:
            raise TemporaryToolContractError("temporary tool lifecycle state is inconsistent")
        return cls(state=state, history=history)

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


def _temporary_tool_confirmation_matches(
    request: Any, artifact: TemporaryToolArtifact
) -> bool:
    """Accept only a gateway-signed confirmation for this exact tool action."""
    sensitive = _SENSITIVE_CAPABILITIES.intersection(
        artifact.manifest.declared_capabilities
    )
    if not sensitive:
        return True
    binding = {
        "session_id": artifact.manifest.session_id,
        "run_id": artifact.manifest.run_id,
        "project_id": artifact.manifest.project_id,
        "tool_id": artifact.manifest.tool_id,
        "version": artifact.manifest.version,
        "source_hash": artifact.manifest.source_hash,
    }
    action = "temporary_tool_sensitive:" + ",".join(sorted(sensitive))
    return _consume_temporary_confirmation(
        request,
        action=action,
        binding=binding,
        capability=",".join(sorted(sensitive)),
    )


def temporary_tools_allowed(
    access_mode: str,
    *,
    source_channel: str | None,
    restrict_to_workspace: bool = False,
) -> bool:
    """Return whether model-authored temporary tools may be created for a turn."""
    return (
        access_mode == "full"
        and source_channel == "websocket"
        and restrict_to_workspace is False
    )
