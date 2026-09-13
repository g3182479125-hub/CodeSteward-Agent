"""Bounded FC execution for model-generated pure-computation candidates.

This module is deliberately separate from ChangeSet verification.  A model
candidate is uploaded as data to an injected execution backend and is never
imported by the DevPilot host.  The fixed runner is a contract adapter; FC is
the execution boundary, not the AST checks below.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from threading import RLock
from time import time
from typing import Protocol

from .execution import (
    ExecutionBackend,
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
    ExecutionResult,
)
from .fc_sandbox import FCSandboxBackend
from .redaction import is_sensitive_field_name, redact_sensitive_payload, redact_sensitive_text

CANDIDATE_MAX_TRIALS = 3
CANDIDATE_MAX_TRIAL_SECONDS = 120
CANDIDATE_MAX_EXECUTIONS = 30
CANDIDATE_MAX_EXECUTION_SECONDS = 600
CANDIDATE_TIMEOUT_SECONDS = 30
CANDIDATE_MAX_OUTPUT_BYTES = 64 * 1024
CANDIDATE_MAX_SOURCE_BYTES = 64 * 1024
CANDIDATE_MAX_INPUT_BYTES = 64 * 1024
CANDIDATE_API_VERSION = "nanobot.tool/v1"
CANDIDATE_LOOP_API_VERSION = "nanobot.loop-driver/v1"
CANDIDATE_COMMAND_PROFILE = "model-candidate"
CANDIDATE_TEMPLATE = "code-interpreter-v1"
CANDIDATE_NETWORK_POLICY = "provider-default"
_CANDIDATE_ENTRY = "candidate.py"
_CANDIDATE_RUNNER = "candidate_runner.py"
_INPUT_FILE = "input.json"
_CANDIDATE_OUTPUT_LIMIT_EXIT_CODE = 86
_BUDGET_KEY_PREFIX = "candidate-budget-"
_RESULT_STATUSES = frozenset({"passed", "failed", "cancelled", "unknown", "rejected"})
_SENSITIVE_INPUT_NAME = frozenset(
    {"database", "db", "connection_string", "dsn", "session", "cookie", "headers", "env"}
)
_LOCAL_REFERENCE_PATTERN = re.compile(r"(?i)(?:file://|[A-Za-z]:[\\/]|\\\\|(?<![A-Za-z0-9])/[^/])")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_TOOL_CAPABILITY_PATTERN = re.compile(r"^tool:[a-z][a-z0-9._-]{0,63}$")
_LOOP_CAPABILITY = "loop:remote-strategy"
_FORBIDDEN_AST_NODES = (
    ast.AsyncFor,
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.Await,
    ast.Delete,
    ast.Global,
    ast.Import,
    ast.ImportFrom,
    ast.Nonlocal,
    ast.Raise,
    ast.Try,
    ast.TryStar,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)
_FORBIDDEN_CALLS = frozenset(
    {"__import__", "breakpoint", "compile", "eval", "exec", "input", "open", "print"}
)
_FIXED_RUNNER_SOURCE = b"""import json

from candidate import transform


with open("/workspace/devpilot/input.json", encoding="utf-8") as input_file:
    payload = json.load(input_file)
result = transform(payload)
serialized = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
if len(serialized.encode("utf-8")) > 65536:
    raise SystemExit(86)
print(serialized)
"""

CandidateInputRule = tuple[str, str, int, int | float | None, int | float | None]


@dataclass(frozen=True, slots=True)
class CandidateTrialGrant:
    """Host-issued scope for one session's approved candidate input projection."""

    grant_id: str
    session_id: str
    task_id: str
    project_id: str
    allowed_input_fields: tuple[str, ...]
    allowed_input_rules: tuple[CandidateInputRule, ...] = ()
    expires_at_epoch: int | None = None
    signature: str = ""


class CandidateTrialGrantIssuer:
    """Host-only issuer/verifier for scoped candidate trial grants.

    A caller may construct the dataclass for serialization, but only a grant
    signed by this host-owned issuer is accepted by the runner.  The secret is
    supplied by the host and never included in a candidate request or result.
    """

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("candidate grant secret must contain at least 32 bytes")
        self._secret = secret

    def issue(
        self,
        *,
        session_id: str,
        task_id: str,
        project_id: str,
        allowed_input_rules: tuple[CandidateInputRule, ...],
        expires_at_epoch: int | None = None,
    ) -> CandidateTrialGrant:
        grant = CandidateTrialGrant(
            grant_id=str(uuid.uuid4()),
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            allowed_input_fields=tuple(rule[0] for rule in allowed_input_rules),
            allowed_input_rules=allowed_input_rules,
            expires_at_epoch=expires_at_epoch,
        )
        return CandidateTrialGrant(
            grant_id=grant.grant_id,
            session_id=grant.session_id,
            task_id=grant.task_id,
            project_id=grant.project_id,
            allowed_input_fields=grant.allowed_input_fields,
            allowed_input_rules=grant.allowed_input_rules,
            expires_at_epoch=grant.expires_at_epoch,
            signature=self._sign(grant),
        )

    def verify(self, grant: CandidateTrialGrant) -> bool:
        if not isinstance(grant.signature, str) or not grant.signature:
            return False
        try:
            expected = self._sign(grant)
        except (TypeError, ValueError, OverflowError):
            return False
        return hmac.compare_digest(grant.signature, expected)

    def _sign(self, grant: CandidateTrialGrant) -> str:
        payload = json.dumps(
            {
                "grant_id": grant.grant_id,
                "session_id": grant.session_id,
                "task_id": grant.task_id,
                "project_id": grant.project_id,
                "allowed_input_fields": grant.allowed_input_fields,
                "allowed_input_rules": grant.allowed_input_rules,
                "expires_at_epoch": grant.expires_at_epoch,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()


class CandidateTrialStore(Protocol):
    """Persistence port backed by DevPilot's existing idempotency table."""

    def reserve_idempotency(self, key: str, request_hash: str) -> tuple[str, str | None]: ...

    def complete_idempotency(self, key: str, response_json: str) -> None: ...

    def compare_and_set_idempotency(
        self, key: str, expected_response_json: str | None, response_json: str
    ) -> bool: ...

    def get_idempotency(self, key: str) -> tuple[str, str | None] | None: ...

    def reserve_candidate_budget(
        self,
        task_id: str,
        max_trials: int,
        max_trial_seconds: int,
        reservation_seconds: int,
        operation_id: str,
    ) -> tuple[bool, int, int, int]: ...

    def get_candidate_budget(self, task_id: str) -> tuple[int, int]: ...

    def validate_candidate_lease(
        self, task_id: str, operation_id: str, lease_token: int
    ) -> bool: ...

    def settle_candidate_budget(
        self, task_id: str, operation_id: str, lease_token: int | None
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class CandidateTrialRequest:
    """A model candidate plus only the approved JSON data for one task."""

    task_id: str
    project_id: str
    trial_id: str
    session_id: str
    grant: CandidateTrialGrant | None
    manifest: Mapping[str, object]
    source: str
    input_data: object
    timeout_seconds: int = CANDIDATE_TIMEOUT_SECONDS
    execution_mode: str = "trial"
    # Host-only cancellation probe. It is intentionally excluded from the
    # manifest/input hashes and never crosses the FC data boundary.
    cancel_requested: Callable[[], bool] | None = None


@dataclass(frozen=True, slots=True)
class CandidateExecutionResult:
    """Secret-free, provider-neutral result for a candidate trial or call."""

    status: str
    candidate_id: str | None
    version: str | None
    trial_id: str
    run_id: str | None
    backend: str | None
    sandbox_id: str | None
    exit_code: int | None
    output: object | None
    error_code: str | None
    error_message: str | None
    duration_ms: int
    cleanup_status: str
    source_hash: str | None
    input_hash: str | None
    output_truncated: bool = False
    operation_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a bounded result without source code or raw input."""
        return {
            "status": self.status,
            "candidate_id": self.candidate_id,
            "version": self.version,
            "trial_id": self.trial_id,
            "run_id": self.run_id,
            "backend": self.backend,
            "sandbox_id": self.sandbox_id,
            "exit_code": self.exit_code,
            "output": self.output,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "duration_ms": self.duration_ms,
            "cleanup_status": self.cleanup_status,
            "source_hash": self.source_hash,
            "input_hash": self.input_hash,
            "output_truncated": self.output_truncated,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True, slots=True)
class _CandidateManifest:
    plugin_id: str
    version: str
    content_hash: str


def _failure(
    request: CandidateTrialRequest,
    code: str,
    message: str,
    *,
    candidate_id: str | None = None,
    version: str | None = None,
    run_id: str | None = None,
    source_hash: str | None = None,
    input_hash: str | None = None,
    duration_ms: int = 0,
    cleanup_status: str = "not_started",
    exit_code: int | None = None,
    backend: str | None = None,
    sandbox_id: str | None = None,
    output_truncated: bool = False,
    operation_id: str | None = None,
    status: str = "failed",
) -> CandidateExecutionResult:
    return CandidateExecutionResult(
        status=status,
        candidate_id=candidate_id,
        version=version,
        trial_id=request.trial_id,
        run_id=run_id,
        backend=backend,
        sandbox_id=sandbox_id,
        exit_code=exit_code,
        output=None,
        error_code=code,
        error_message=message,
        duration_ms=duration_ms,
        cleanup_status=cleanup_status,
        source_hash=source_hash,
        input_hash=input_hash,
        output_truncated=output_truncated,
        operation_id=operation_id,
    )


def _mapping_sequence(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _validate_manifest(
    manifest: Mapping[str, object], source: str
) -> tuple[_CandidateManifest | None, tuple[str, str] | None]:
    expected_fields = {
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
    if set(manifest) != expected_fields:
        return None, ("CANDIDATE_MANIFEST_INVALID", "候选 manifest 字段不完整或包含未知字段")
    plugin_id = manifest.get("plugin_id")
    version = manifest.get("version")
    content_hash = manifest.get("content_hash")
    if not (
        isinstance(plugin_id, str)
        and _ID_PATTERN.fullmatch(plugin_id)
        and isinstance(version, str)
        and _VERSION_PATTERN.fullmatch(version)
        and isinstance(content_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", content_hash)
    ):
        return None, ("CANDIDATE_MANIFEST_INVALID", "候选标识、版本或内容哈希无效")
    kind = manifest.get("kind")
    api_version = manifest.get("api_version")
    if kind == "tool":
        expected_api_version = CANDIDATE_API_VERSION
        expected_provides: tuple[str, ...] | None = None
    elif kind == "loop":
        expected_api_version = CANDIDATE_LOOP_API_VERSION
        expected_provides = (_LOOP_CAPABILITY,)
    else:
        return None, ("CANDIDATE_KIND_INVALID", "模型候选仅支持纯计算 tool 或受限 loop")
    if api_version != expected_api_version:
        return None, ("CANDIDATE_API_VERSION_INVALID", "候选接口版本不受支持")
    if manifest.get("scope") not in {"session", "project"}:
        return None, ("CANDIDATE_SCOPE_INVALID", "模型候选不能使用 global 作用域")
    if manifest.get("entry") != _CANDIDATE_ENTRY:
        return None, ("CANDIDATE_ENTRY_INVALID", "候选入口必须是 candidate.py")
    requires = _mapping_sequence(manifest.get("requires"))
    provides = _mapping_sequence(manifest.get("provides"))
    capabilities = _mapping_sequence(manifest.get("requested_capabilities"))
    if requires != ():
        return None, ("CANDIDATE_DEPENDENCY_DENIED", "纯计算候选不能依赖宿主服务")
    if provides is None or len(provides) != 1:
        return None, ("CANDIDATE_MANIFEST_INVALID", "候选必须提供一个显式能力")
    if expected_provides is not None:
        if provides != expected_provides:
            return None, ("CANDIDATE_MANIFEST_INVALID", "受限 loop 只能提供 remote-strategy 能力")
    elif not _TOOL_CAPABILITY_PATTERN.fullmatch(provides[0]):
        return None, ("CANDIDATE_MANIFEST_INVALID", "候选必须提供一个显式 tool 能力")
    if capabilities != ():
        return None, ("CANDIDATE_CAPABILITY_DENIED", "纯计算候选不能申请宿主权限")
    if manifest.get("execution_mode") != "fc_proxy":
        return None, ("CANDIDATE_EXECUTION_MODE_INVALID", "模型候选必须使用 FC 代理执行")
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if source_hash != content_hash:
        return None, ("CANDIDATE_HASH_MISMATCH", "候选内容与 manifest 哈希不一致")
    return _CandidateManifest(plugin_id, version, content_hash), None


def _validate_source(source: str) -> tuple[str, str] | None:
    try:
        encoded = source.encode("utf-8")
    except UnicodeEncodeError:
        return "CANDIDATE_SOURCE_INVALID", "候选源码不是有效 UTF-8"
    if not encoded or len(encoded) > CANDIDATE_MAX_SOURCE_BYTES:
        return "CANDIDATE_SOURCE_LIMIT_EXCEEDED", "候选源码超过 64 KiB 限制"
    if _LOCAL_REFERENCE_PATTERN.search(source) is not None:
        return "CANDIDATE_SOURCE_LOCAL_REFERENCE", "候选源码包含本机绝对路径或 file 引用"
    try:
        tree = ast.parse(source, filename=_CANDIDATE_ENTRY, mode="exec")
    except SyntaxError:
        return "CANDIDATE_SOURCE_INVALID", "候选源码无法解析"
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_AST_NODES):
            return "CANDIDATE_SOURCE_FORBIDDEN", "候选源码包含不允许的导入、异步或副作用结构"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                return "CANDIDATE_SOURCE_FORBIDDEN", "候选源码调用了不允许的运行时能力"
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    allowed_docstring = (
        isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
        if tree.body
        else False
    )
    top_level = tree.body[1:] if allowed_docstring else tree.body
    if (
        len(functions) != 1
        or functions[0].name != "transform"
        or any(not isinstance(node, ast.FunctionDef) for node in top_level)
    ):
        return "CANDIDATE_ENTRY_INVALID", "候选必须只定义一个 transform(payload) 函数"
    function = functions[0]
    arguments = function.args
    if (
        len(arguments.posonlyargs) + len(arguments.args) != 1
        or arguments.args[0].arg != "payload"
        or arguments.vararg is not None
        or arguments.kwarg is not None
        or arguments.kwonlyargs
        or function.decorator_list
    ):
        return "CANDIDATE_ENTRY_INVALID", "候选入口必须是无装饰器的 transform(payload)"
    return None


def _contains_sensitive_field(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and is_sensitive_field_name(key))
            or _contains_sensitive_field(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_sensitive_field(item) for item in value)
    return False


def _contains_sensitive_text(value: object) -> bool:
    if isinstance(value, str):
        return (
            redact_sensitive_text(value) != value
            or _LOCAL_REFERENCE_PATTERN.search(value) is not None
        )
    if isinstance(value, Mapping):
        return any(
            _contains_sensitive_text(key) or _contains_sensitive_text(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_sensitive_text(item) for item in value)
    return False


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _result_from_dict(payload: object) -> CandidateExecutionResult | None:
    if not isinstance(payload, Mapping):
        return None
    required = {
        "status",
        "candidate_id",
        "version",
        "trial_id",
        "run_id",
        "backend",
        "sandbox_id",
        "exit_code",
        "output",
        "error_code",
        "error_message",
        "duration_ms",
        "cleanup_status",
        "source_hash",
        "input_hash",
        "output_truncated",
        "operation_id",
    }
    if not required.issubset(payload):
        return None
    if not all(
        isinstance(payload.get(field), str)
        for field in {"status", "trial_id", "cleanup_status"}
    ) or payload["status"] not in _RESULT_STATUSES:
        return None
    if not isinstance(payload.get("duration_ms"), int) or isinstance(
        payload.get("duration_ms"), bool
    ):
        return None
    if payload["duration_ms"] < 0 or not isinstance(payload.get("output_truncated"), bool):
        return None
    optional_strings = {
        "candidate_id",
        "version",
        "run_id",
        "backend",
        "sandbox_id",
        "error_code",
        "error_message",
        "source_hash",
        "input_hash",
        "operation_id",
    }
    if any(
        field in payload and payload[field] is not None and not isinstance(payload[field], str)
        for field in optional_strings
    ):
        return None
    if "exit_code" in payload and payload["exit_code"] is not None and (
        not isinstance(payload["exit_code"], int) or isinstance(payload["exit_code"], bool)
    ):
        return None
    return CandidateExecutionResult(
        status=payload["status"],
        candidate_id=payload.get("candidate_id")
        if isinstance(payload.get("candidate_id"), str)
        else None,
        version=payload.get("version") if isinstance(payload.get("version"), str) else None,
        trial_id=payload["trial_id"],
        run_id=payload.get("run_id") if isinstance(payload.get("run_id"), str) else None,
        backend=payload.get("backend") if isinstance(payload.get("backend"), str) else None,
        sandbox_id=payload.get("sandbox_id")
        if isinstance(payload.get("sandbox_id"), str)
        else None,
        exit_code=payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
        output=payload.get("output"),
        error_code=payload.get("error_code")
        if isinstance(payload.get("error_code"), str)
        else None,
        error_message=payload.get("error_message")
        if isinstance(payload.get("error_message"), str)
        else None,
        duration_ms=payload["duration_ms"],
        cleanup_status=payload["cleanup_status"],
        source_hash=payload.get("source_hash")
        if isinstance(payload.get("source_hash"), str)
        else None,
        input_hash=payload.get("input_hash")
        if isinstance(payload.get("input_hash"), str)
        else None,
        output_truncated=payload["output_truncated"],
        operation_id=payload.get("operation_id")
        if isinstance(payload.get("operation_id"), str)
        else None,
    )


def _validate_grant(
    request: CandidateTrialRequest,
    issuer: CandidateTrialGrantIssuer,
) -> tuple[str, str] | None:
    grant = request.grant
    if not isinstance(grant, CandidateTrialGrant):
        return "CANDIDATE_GRANT_REQUIRED", "候选试运行必须使用 Host 签发的试用授权"
    if (
        grant.session_id != request.session_id
        or grant.task_id != request.task_id
        or grant.project_id != request.project_id
    ):
        return "CANDIDATE_GRANT_SCOPE_INVALID", "候选试用授权与当前会话或项目不匹配"
    if (
        not isinstance(grant.grant_id, str)
        or not isinstance(grant.allowed_input_fields, tuple)
        or not grant.grant_id
        or not grant.allowed_input_fields
        or not issuer.verify(grant)
    ):
        return "CANDIDATE_GRANT_INVALID", "候选试用授权缺少输入字段投影"
    if any(not isinstance(field, str) for field in grant.allowed_input_fields):
        return "CANDIDATE_GRANT_INVALID", "候选试用授权的输入字段投影无效"
    if len(set(grant.allowed_input_fields)) != len(grant.allowed_input_fields) or any(
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", field)
        for field in grant.allowed_input_fields
    ):
        return "CANDIDATE_GRANT_INVALID", "候选试用授权的输入字段投影无效"
    if (
        not isinstance(grant.allowed_input_rules, tuple)
        or len(grant.allowed_input_rules) != len(grant.allowed_input_fields)
        or any(
            not isinstance(rule, tuple) or len(rule) != 5
            for rule in grant.allowed_input_rules
        )
    ):
        return "CANDIDATE_GRANT_INVALID", "候选试用授权缺少值级输入规则"
    for rule in grant.allowed_input_rules:
        field, kind, max_length, min_value, max_value = rule
        if (
            not isinstance(field, str)
            or field not in grant.allowed_input_fields
            or kind not in {"string", "integer", "number", "boolean"}
            or not isinstance(max_length, int)
            or isinstance(max_length, bool)
            or not 1 <= max_length <= CANDIDATE_MAX_INPUT_BYTES
            or (min_value is not None and (
                not isinstance(min_value, (int, float)) or isinstance(min_value, bool)
            ))
            or (max_value is not None and (
                not isinstance(max_value, (int, float)) or isinstance(max_value, bool)
            ))
            or (
                min_value is not None
                and max_value is not None
                and min_value > max_value
            )
            or (
                kind == "integer"
                and any(
                    value is not None and not isinstance(value, int)
                    or isinstance(value, bool)
                    for value in (min_value, max_value)
                )
            )
            or (
                kind in {"string", "boolean"}
                and (min_value is not None or max_value is not None)
            )
        ):
            return "CANDIDATE_GRANT_INVALID", "候选试用授权的值级输入规则无效"
    if grant.expires_at_epoch is not None and (
        not isinstance(grant.expires_at_epoch, int)
        or isinstance(grant.expires_at_epoch, bool)
    ):
        return "CANDIDATE_GRANT_INVALID", "候选试用授权的过期时间无效"
    if grant.expires_at_epoch is not None and grant.expires_at_epoch <= int(time()):
        return "CANDIDATE_GRANT_EXPIRED", "候选试用授权已过期"
    return None


def _validate_approved_input(
    input_data: object, grant: CandidateTrialGrant
) -> tuple[str, str] | None:
    if not isinstance(input_data, Mapping):
        return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入必须是已授权字段对象"
    if _contains_sensitive_field(input_data) or _contains_sensitive_text(input_data):
        return "CANDIDATE_INPUT_SENSITIVE", "候选输入包含疑似凭据或本机引用"
    fields = tuple(input_data)
    if any(not isinstance(field, str) for field in fields):
        return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入字段名必须是文本"
    allowed = set(grant.allowed_input_fields)
    rules = {
        field: (kind, max_length, min_value, max_value)
        for field, kind, max_length, min_value, max_value in grant.allowed_input_rules
    }
    if any(field not in allowed for field in fields):
        return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入包含未获授权的字段"
    if any(field.casefold() in _SENSITIVE_INPUT_NAME for field in fields):
        return "CANDIDATE_INPUT_SENSITIVE", "候选输入包含不允许的数据类别"
    for field in fields:
        kind, max_length, min_value, max_value = rules[field]
        value = input_data[field]
        if kind == "string" and (not isinstance(value, str) or len(value) > max_length):
            return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入不符合 Host 签发的值级规则"
        if kind == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入不符合 Host 签发的值级规则"
            if (min_value is not None and value < min_value) or (
                max_value is not None and value > max_value
            ):
                return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入超出 Host 签发的数值范围"
        if kind == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入不符合 Host 签发的值级规则"
            if (min_value is not None and value < min_value) or (
                max_value is not None and value > max_value
            ):
                return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入超出 Host 签发的数值范围"
        if kind == "boolean" and not isinstance(value, bool):
            return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入不符合 Host 签发的值级规则"
    return None


def _candidate_backend_error(backend: ExecutionBackend | None) -> tuple[str, str] | None:
    """Require the fixed FC candidate contract, not a caller-set flag."""

    if backend is None:
        return "CANDIDATE_BACKEND_UNAVAILABLE", "候选试运行未绑定受信 FC 后端，未执行本机回退"
    if not isinstance(backend, FCSandboxBackend):
        return "CANDIDATE_BACKEND_UNAVAILABLE", "候选试运行只允许使用受信 FC 后端，未执行本机回退"
    contract = getattr(backend, "candidate_execution_contract", None)
    if not callable(contract):
        return "CANDIDATE_BACKEND_CONTRACT_INVALID", "候选后端未提供固定 FC 执行契约"
    try:
        actual = contract()
    except Exception:
        return "CANDIDATE_BACKEND_CONTRACT_INVALID", "候选后端能力检查失败"
    expected = (
        "fc-agent-sandbox",
        CANDIDATE_COMMAND_PROFILE,
        CANDIDATE_TEMPLATE,
        CANDIDATE_NETWORK_POLICY,
    )
    if actual != expected:
        return "CANDIDATE_BACKEND_CONTRACT_INVALID", "候选后端未绑定固定 FC profile、模板和策略"
    return None


def _execution_receipt_error(result: ExecutionResult) -> tuple[str, str] | None:
    expected = (
        "fc-agent-sandbox",
        CANDIDATE_COMMAND_PROFILE,
        CANDIDATE_TEMPLATE,
        CANDIDATE_NETWORK_POLICY,
    )
    actual = (result.backend, result.command_profile, result.template, result.network_policy)
    if actual != expected:
        return "CANDIDATE_RESULT_CONTRACT_INVALID", "FC 返回结果未满足固定候选执行契约"
    return None


class CandidateTrialRunner:
    """Run model candidates through one bounded, injected execution backend."""

    def __init__(
        self,
        backend: ExecutionBackend | None,
        *,
        max_trials: int = CANDIDATE_MAX_TRIALS,
        max_trial_seconds: int = CANDIDATE_MAX_TRIAL_SECONDS,
        timeout_seconds: int = CANDIDATE_TIMEOUT_SECONDS,
        store: CandidateTrialStore,
        grant_issuer: CandidateTrialGrantIssuer,
    ) -> None:
        if not 1 <= max_trials <= CANDIDATE_MAX_TRIALS:
            raise ValueError("max_trials must be between 1 and 3")
        if not 1 <= max_trial_seconds <= CANDIDATE_MAX_TRIAL_SECONDS:
            raise ValueError("max_trial_seconds must be between 1 and 120")
        if not 1 <= timeout_seconds <= CANDIDATE_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 1 and 30")
        self._backend = backend
        self._max_trials = max_trials
        self._max_trial_seconds = max_trial_seconds
        self._timeout_seconds = timeout_seconds
        self._store = store
        self._grant_issuer = grant_issuer
        self._lock = RLock()

    def trial(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        """Validate and trial one candidate without executing it on the host."""
        return self._run(request)

    def execute(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        """Execute a validated candidate through the same FC proxy path.

        H6 will decide when a candidate is active.  Keeping the runtime path
        here ensures activation cannot later introduce a host-import fallback.
        """
        return self._run(request)

    def budget_status(self, task_id: str) -> dict[str, int]:
        trials, reserved_seconds = self._store.get_candidate_budget(task_id)
        return {
            "trials": trials,
            "reserved_seconds": reserved_seconds,
            "max_trials": self._max_trials,
            "max_trial_seconds": self._max_trial_seconds,
        }

    def can_activate_live(self, task_id: str) -> bool:
        """Fail closed unless the task has room for one live candidate call.

        Trial and live calls deliberately share the same durable counter.  The
        trial limit remains stricter while a trial is being requested; a live
        call uses the larger overall H6 execution allowance.
        """
        try:
            calls, reserved_seconds = self._store.get_candidate_budget(task_id)
        except Exception:
            return False
        return (
            calls < CANDIDATE_MAX_EXECUTIONS
            and reserved_seconds + self._timeout_seconds <= CANDIDATE_MAX_EXECUTION_SECONDS
        )

    def operation_status(self, request: CandidateTrialRequest) -> dict[str, object]:
        """Read one durable operation without starting or retrying execution."""

        operation_id = self._operation_key(request)
        grant_error = _validate_grant(request, self._grant_issuer)
        if grant_error is not None:
            return {
                "status": "rejected",
                "operation_id": operation_id,
                "error_code": grant_error[0],
            }
        record = self._store.get_idempotency(operation_id)
        if record is None:
            return {"status": "not_found", "operation_id": operation_id}
        payload = self._decode_operation(record[1])
        if payload is None or not self._operation_scope_matches(payload, request):
            return {
                "status": "unknown",
                "operation_id": operation_id,
                "error_code": "CANDIDATE_PERSISTENCE_INVALID",
            }
        if (
            payload.get("record_type") == "candidate_trial"
            and payload.get("status") == "in_progress"
        ):
            if not self._operation_envelope_matches(payload, request, operation_id):
                return {
                    "status": "unknown",
                    "operation_id": operation_id,
                    "error_code": "CANDIDATE_PERSISTENCE_INVALID",
                }
            payload["operation_id"] = operation_id
            return payload
        if not self._operation_envelope_matches(payload, request, operation_id):
            return {
                "status": "unknown",
                "operation_id": operation_id,
                "error_code": "CANDIDATE_PERSISTENCE_INVALID",
            }
        if _result_from_dict(payload) is None:
            return {
                "status": "unknown",
                "operation_id": operation_id,
                "error_code": "CANDIDATE_PERSISTENCE_INVALID",
            }
        payload["operation_id"] = operation_id
        return payload

    def cancel(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        """Cancel a durable in-progress operation through the backend port."""

        operation_id = self._operation_key(request)
        grant_error = _validate_grant(request, self._grant_issuer)
        if grant_error is not None:
            return _failure(
                request,
                grant_error[0],
                grant_error[1],
                operation_id=operation_id,
                status="rejected",
            )
        backend_error = _candidate_backend_error(self._backend)
        if backend_error is not None:
            return _failure(
                request,
                backend_error[0],
                backend_error[1],
                operation_id=operation_id,
            )
        progress = self._operation_progress(request)
        if progress is None:
            return _failure(
                request,
                "CANDIDATE_OPERATION_NOT_FOUND",
                "候选试运行没有可取消的持久 operation",
                operation_id=operation_id,
            )
        run_id = progress.get("run_id")
        cancel = getattr(self._backend, "cancel", None)
        if not isinstance(run_id, str) or not callable(cancel):
            return _failure(
                request,
                "CANDIDATE_CANCEL_UNAVAILABLE",
                "候选执行后端不支持安全取消",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id if isinstance(run_id, str) else None,
                operation_id=operation_id,
            )
        try:
            state = cancel(run_id)
        except Exception:
            return _failure(
                request,
                "CANDIDATE_CANCEL_FAILED",
                "候选执行取消失败，结果保持未知",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id,
                operation_id=operation_id,
                status="unknown",
            )
        if (
            not isinstance(state, Mapping)
            or state.get("status") != "cancelled"
            or state.get("cleanup_status") != "cleaned"
        ):
            return _failure(
                request,
                "CANDIDATE_CANCEL_UNCONFIRMED",
                "FC 未确认取消和清理完成，operation 保持未知",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id,
                operation_id=operation_id,
                status="unknown",
            )
        cleanup_status = (
            state.get("cleanup_status") if isinstance(state, Mapping) else "unknown"
        )
        result = _failure(
            request,
            "CANDIDATE_CANCELLED",
            "候选试运行已取消",
            candidate_id=self._string_value(progress.get("candidate_id")),
            version=self._string_value(progress.get("version")),
            run_id=run_id,
            sandbox_id=(
                self._string_value(state.get("sandbox_id"))
                if isinstance(state, Mapping)
                else None
            ),
            cleanup_status=cleanup_status if isinstance(cleanup_status, str) else "unknown",
            operation_id=operation_id,
            status="cancelled",
        )
        operation_hash = self._string_value(progress.get("operation_hash"))
        if operation_hash is None:
            return _failure(
                request,
                "CANDIDATE_PERSISTENCE_INVALID",
                "候选 operation 缺少不可变操作哈希",
                operation_id=operation_id,
                status="unknown",
            )
        return self._complete_operation(request, operation_id, operation_hash, result)

    def recover(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        """Query an unfinished run and finalize only a provider-confirmed result."""

        operation_id = self._operation_key(request)
        grant_error = _validate_grant(request, self._grant_issuer)
        if grant_error is not None:
            return _failure(
                request,
                grant_error[0],
                grant_error[1],
                operation_id=operation_id,
                status="rejected",
            )
        backend_error = _candidate_backend_error(self._backend)
        if backend_error is not None:
            return _failure(
                request,
                backend_error[0],
                backend_error[1],
                operation_id=operation_id,
            )
        progress = self._operation_progress(request)
        if progress is None:
            record = self._store.get_idempotency(operation_id)
            replay = _result_from_dict(self._decode_operation(record[1]) if record else None)
            return replay or _failure(
                request,
                "CANDIDATE_OPERATION_NOT_FOUND",
                "候选试运行没有可恢复的持久 operation",
                operation_id=operation_id,
            )
        run_id = progress.get("run_id")
        getter = getattr(self._backend, "get_run_state", None)
        if not isinstance(run_id, str) or not callable(getter):
            return _failure(
                request,
                "CANDIDATE_RESULT_UNKNOWN",
                "无法查询候选运行状态，未自动重试",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id if isinstance(run_id, str) else None,
                operation_id=operation_id,
                status="unknown",
            )
        try:
            state = getter(run_id)
        except Exception:
            state = None
        if not isinstance(state, Mapping):
            return _failure(
                request,
                "CANDIDATE_RESULT_UNKNOWN",
                "候选运行状态无法读取，未自动重试",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id,
                operation_id=operation_id,
                status="unknown",
            )
        stored_sandbox_id = self._string_value(progress.get("sandbox_id"))
        provider_sandbox_id = self._string_value(state.get("sandbox_id"))
        if (
            stored_sandbox_id is not None
            and provider_sandbox_id is not None
            and stored_sandbox_id != provider_sandbox_id
        ):
            return _failure(
                request,
                "CANDIDATE_RESULT_UNKNOWN",
                "持久 operation 与 FC 返回的 sandbox 不匹配，未自动重试",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id,
                sandbox_id=provider_sandbox_id,
                operation_id=operation_id,
                status="unknown",
            )
        if stored_sandbox_id is None and provider_sandbox_id is not None:
            if not self._update_operation_progress(
                operation_id, {"sandbox_id": provider_sandbox_id}
            ):
                return _failure(
                    request,
                    "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                    "候选恢复时无法持久化 sandbox 标识",
                    candidate_id=self._string_value(progress.get("candidate_id")),
                    version=self._string_value(progress.get("version")),
                    run_id=run_id,
                    sandbox_id=provider_sandbox_id,
                    operation_id=operation_id,
                    status="unknown",
                )
        state_name = state.get("status")
        if state_name in {"starting", "running"}:
            return _failure(
                request,
                "CANDIDATE_TRIAL_IN_PROGRESS",
                "候选运行仍在处理中，未自动重试",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id,
                operation_id=operation_id,
                status="unknown",
            )
        raw_result = state.get("result")
        if not isinstance(raw_result, Mapping):
            result = _failure(
                request,
                "CANDIDATE_RESULT_UNKNOWN",
                "候选运行缺少可验证结果，未自动重试",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id,
                sandbox_id=self._string_value(state.get("sandbox_id")),
                cleanup_status=self._string_value(state.get("cleanup_status")) or "unknown",
                operation_id=operation_id,
                status="unknown",
            )
            return self._complete_operation(request, operation_id, "", result)
        execution = self._execution_from_mapping(raw_result)
        if execution is None:
            return _failure(
                request,
                "CANDIDATE_RESULT_UNKNOWN",
                "候选运行结果格式无法验证，未自动重试",
                candidate_id=self._string_value(progress.get("candidate_id")),
                version=self._string_value(progress.get("version")),
                run_id=run_id,
                operation_id=operation_id,
                status="unknown",
            )
        operation_hash = self._string_value(progress.get("operation_hash"))
        if operation_hash is None:
            return _failure(
                request,
                "CANDIDATE_PERSISTENCE_INVALID",
                "候选 operation 缺少不可变操作哈希",
                operation_id=operation_id,
                status="unknown",
            )
        return self._finalize_execution(
            request,
            operation_id,
            operation_hash,
            self._string_value(progress.get("candidate_id")),
            self._string_value(progress.get("version")),
            self._string_value(progress.get("source_hash")),
            self._string_value(progress.get("input_hash")),
            run_id,
            execution,
        )

    @staticmethod
    def _string_value(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _decode_operation(response_json: str | None) -> dict[str, object] | None:
        if not response_json:
            return None
        try:
            decoded = json.loads(response_json)
        except (TypeError, ValueError):
            return None
        return dict(decoded) if isinstance(decoded, Mapping) else None

    @staticmethod
    def _operation_scope_matches(
        payload: Mapping[str, object], request: CandidateTrialRequest
    ) -> bool:
        return all(
            payload.get(field) == getattr(request, field)
            for field in ("task_id", "session_id", "project_id", "trial_id")
        ) and payload.get("execution_mode", "trial") == request.execution_mode

    @staticmethod
    def _operation_envelope_matches(
        payload: Mapping[str, object],
        request: CandidateTrialRequest,
        operation_key: str,
    ) -> bool:
        operation_hash = payload.get("operation_hash")
        return (
            payload.get("record_type") == "candidate_trial"
            and payload.get("operation_id") == operation_key
            and isinstance(operation_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", operation_hash) is not None
            and CandidateTrialRunner._operation_scope_matches(payload, request)
        )

    def _operation_progress(self, request: CandidateTrialRequest) -> dict[str, object] | None:
        record = self._store.get_idempotency(self._operation_key(request))
        payload = self._decode_operation(record[1]) if record else None
        if (
            payload is None
            or payload.get("record_type") != "candidate_trial"
            or payload.get("status") != "in_progress"
            or not self._operation_envelope_matches(
                payload, request, self._operation_key(request)
            )
        ):
            return None
        return payload

    def _update_operation_progress(
        self, operation_key: str, updates: Mapping[str, object]
    ) -> bool:
        record = self._store.get_idempotency(operation_key)
        if record is None:
            return False
        payload = self._decode_operation(record[1])
        if (
            payload is None
            or payload.get("status") != "in_progress"
            or payload.get("record_type") != "candidate_trial"
            or payload.get("operation_id") != operation_key
            or not isinstance(payload.get("operation_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["operation_hash"]) is None
        ):
            return False
        payload.update(updates)
        updated = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return self._store.compare_and_set_idempotency(operation_key, record[1], updated)

    def _budget_key(self, task_id: str) -> str:
        digest = hashlib.sha256(task_id.encode()).hexdigest()
        return _BUDGET_KEY_PREFIX + digest

    def _operation_key(self, request: CandidateTrialRequest) -> str:
        digest = hashlib.sha256(
            f"{request.session_id}\0{request.project_id}\0{request.task_id}\0{request.trial_id}".encode()
        ).hexdigest()
        return "candidate-operation-" + digest

    def _operation_hash(
        self,
        request: CandidateTrialRequest,
        source_hash: str,
        input_hash: str,
        manifest: _CandidateManifest,
    ) -> str:
        grant = request.grant
        assert isinstance(grant, CandidateTrialGrant)
        payload = {
            "session_id": request.session_id,
            "task_id": request.task_id,
            "trial_id": request.trial_id,
            "execution_mode": request.execution_mode,
            "grant_id": grant.grant_id,
            "candidate_id": manifest.plugin_id,
            "version": manifest.version,
            "source_hash": source_hash,
            "input_hash": input_hash,
        }
        return hashlib.sha256(_json_bytes(payload)).hexdigest()

    def _reserve_operation(
        self,
        request: CandidateTrialRequest,
        operation_key: str,
        operation_hash: str,
        *,
        candidate_id: str,
        version: str,
        run_id: str,
        source_hash: str,
        input_hash: str,
        budget_task_id: str,
    ) -> tuple[CandidateExecutionResult | None, bool]:
        try:
            state, response_json = self._store.reserve_idempotency(
                operation_key, operation_hash
            )
        except ValueError:
            return (
                _failure(
                    request,
                    "CANDIDATE_TRIAL_CONFLICT",
                    "相同试运行编号绑定了不同候选内容",
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    operation_id=operation_key,
                    status="rejected",
                ),
                False,
            )
        except Exception:
            return (
                _failure(
                    request,
                    "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                    "候选试运行状态无法持久化",
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    operation_id=operation_key,
                ),
                False,
            )
        if state == "replay":
            try:
                decoded = json.loads(response_json or "")
            except (TypeError, ValueError):
                decoded = None
            replay = _result_from_dict(decoded)
            if replay is None or not isinstance(decoded, Mapping):
                replay = None
            elif not self._operation_envelope_matches(decoded, request, operation_key):
                replay = None
            elif decoded.get("operation_hash") != operation_hash:
                replay = None
            if replay is None:
                return (
                    _failure(
                        request,
                        "CANDIDATE_PERSISTENCE_INVALID",
                        "候选试运行结果记录无法读取",
                        candidate_id=candidate_id,
                        version=version,
                        run_id=run_id,
                        source_hash=source_hash,
                        input_hash=input_hash,
                        operation_id=operation_key,
                    ),
                    False,
                )
            return replay, False
        if state == "in_progress":
            decoded = self._decode_operation(response_json)
            if (
                decoded is None
                or not self._operation_envelope_matches(decoded, request, operation_key)
                or decoded.get("operation_hash") != operation_hash
            ):
                return (
                    _failure(
                        request,
                        "CANDIDATE_PERSISTENCE_INVALID",
                        "候选试运行状态记录无法验证",
                        candidate_id=candidate_id,
                        version=version,
                        run_id=run_id,
                        source_hash=source_hash,
                        input_hash=input_hash,
                        operation_id=operation_key,
                    ),
                    False,
                )
            return (
                _failure(
                    request,
                    "CANDIDATE_TRIAL_IN_PROGRESS",
                    "相同候选试运行仍在处理中，请先查询或恢复 operation_id",
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    operation_id=operation_key,
                ),
                False,
            )
        progress = json.dumps(
            {
                "record_type": "candidate_trial",
                "status": "in_progress",
                "operation_id": operation_key,
                "operation_hash": operation_hash,
                "task_id": request.task_id,
                "session_id": request.session_id,
                "project_id": request.project_id,
                "trial_id": request.trial_id,
                "execution_mode": request.execution_mode,
                "budget_task_id": budget_task_id,
                "candidate_id": candidate_id,
                "version": version,
                "source_hash": source_hash,
                "input_hash": input_hash,
                "run_id": run_id,
                "sandbox_id": None,
                "lease_token": None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            self._store.complete_idempotency(operation_key, progress)
        except Exception:
            abort = getattr(self._store, "abort_idempotency", None)
            if callable(abort):
                try:
                    abort(operation_key)
                except Exception:
                    pass
            return (
                _failure(
                    request,
                    "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                    "候选试运行状态无法持久化",
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    operation_id=operation_key,
                ),
                False,
            )
        return None, True

    def _complete_operation(
        self,
        request: CandidateTrialRequest,
        operation_key: str,
        operation_hash: str,
        result: CandidateExecutionResult,
    ) -> CandidateExecutionResult:
        try:
            if (
                not re.fullmatch(r"[0-9a-f]{64}", operation_hash)
                or result.operation_id != operation_key
                or result.trial_id != request.trial_id
            ):
                raise ValueError("candidate operation envelope is invalid")
            persisted = result.to_dict()
            persisted.update(
                {
                    "record_type": "candidate_trial",
                    "task_id": request.task_id,
                    "session_id": request.session_id,
                    "project_id": request.project_id,
                    "operation_hash": operation_hash,
                }
            )
            serialized = json.dumps(
                persisted, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
            record = self._store.get_idempotency(operation_key)
            lease_token = None
            if record is None:
                raise RuntimeError("candidate operation reservation is missing")
            progress = self._decode_operation(record[1])
            if (
                progress is None
                or progress.get("status") != "in_progress"
                or not self._operation_envelope_matches(progress, request, operation_key)
                or progress.get("operation_hash") != operation_hash
            ):
                raise ValueError("candidate operation reservation is invalid")
            if isinstance(progress.get("lease_token"), int):
                lease_token = progress["lease_token"]
            if not self._store.compare_and_set_idempotency(operation_key, record[1], serialized):
                raise RuntimeError("candidate operation completion lost its compare-and-set")
            if result.status != "unknown":
                budget_task_id = progress.get("budget_task_id", request.task_id)
                if not isinstance(budget_task_id, str) or not budget_task_id:
                    raise ValueError("candidate budget binding is invalid")
                self._store.settle_candidate_budget(
                    budget_task_id,
                    operation_key,
                    lease_token,
                )
        except Exception:
            return _failure(
                request,
                "CANDIDATE_RESULT_PERSISTENCE_FAILED",
                "候选结果无法持久化，未将本次执行视为成功",
                candidate_id=result.candidate_id,
                version=result.version,
                run_id=result.run_id,
                source_hash=result.source_hash,
                input_hash=result.input_hash,
                duration_ms=result.duration_ms,
                cleanup_status=result.cleanup_status,
                exit_code=result.exit_code,
                backend=result.backend,
                sandbox_id=result.sandbox_id,
                operation_id=result.operation_id,
            )
        return result

    def _run(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        if request.execution_mode not in {"trial", "live"}:
            return _failure(
                request,
                "CANDIDATE_EXECUTION_MODE_INVALID",
                "候选执行模式无效",
            )
        # A trial retains its tighter H5 allowance, while every trial and live
        # call consumes this one durable H6 task budget.  This prevents an
        # activated candidate from resetting its spend simply by changing mode.
        budget_task_id = request.task_id
        max_calls = (
            self._max_trials
            if request.execution_mode == "trial"
            else CANDIDATE_MAX_EXECUTIONS
        )
        max_seconds = (
            self._max_trial_seconds
            if request.execution_mode == "trial"
            else CANDIDATE_MAX_EXECUTION_SECONDS
        )
        if not request.task_id or not request.project_id or not request.trial_id:
            return _failure(request, "CANDIDATE_SCOPE_INVALID", "候选任务作用域不完整")
        if not isinstance(request.source, str):
            return _failure(request, "CANDIDATE_SOURCE_INVALID", "候选源码必须是文本")
        if not isinstance(request.manifest, Mapping):
            return _failure(request, "CANDIDATE_MANIFEST_INVALID", "候选 manifest 必须是对象")
        grant = request.grant
        grant_error = _validate_grant(request, self._grant_issuer)
        if grant_error is not None:
            return _failure(request, grant_error[0], grant_error[1], status="rejected")
        assert isinstance(grant, CandidateTrialGrant)
        backend_error = _candidate_backend_error(self._backend)
        if backend_error is not None:
            return _failure(request, backend_error[0], backend_error[1], status="failed")
        if request.timeout_seconds != self._timeout_seconds:
            return _failure(
                request,
                "CANDIDATE_TIMEOUT_INVALID",
                "候选试运行必须使用固定 30 秒超时",
            )
        source_error = _validate_source(request.source)
        source_hash = hashlib.sha256(request.source.encode("utf-8")).hexdigest()
        if source_error is not None:
            return _failure(
                request,
                source_error[0],
                source_error[1],
                source_hash=source_hash,
                status="rejected",
            )
        if redact_sensitive_text(request.source) != request.source:
            return _failure(
                request,
                "CANDIDATE_SOURCE_SENSITIVE",
                "候选源码包含疑似凭据内容",
                source_hash=source_hash,
                status="rejected",
            )
        manifest, manifest_error = _validate_manifest(request.manifest, request.source)
        if manifest_error is not None or manifest is None:
            code, message = manifest_error or (
                "CANDIDATE_MANIFEST_INVALID",
                "候选 manifest 无效",
            )
            return _failure(
                request,
                code,
                message,
                candidate_id=(
                    request.manifest.get("plugin_id")
                    if isinstance(request.manifest.get("plugin_id"), str)
                    else None
                ),
                version=(
                    request.manifest.get("version")
                    if isinstance(request.manifest.get("version"), str)
                    else None
                ),
                source_hash=source_hash,
                status="rejected",
            )
        try:
            input_bytes = _json_bytes(request.input_data)
        except (TypeError, ValueError, OverflowError):
            return _failure(
                request,
                "CANDIDATE_INPUT_INVALID",
                "候选输入必须是可序列化 JSON",
                candidate_id=manifest.plugin_id,
                version=manifest.version,
                source_hash=source_hash,
                status="rejected",
            )
        input_hash = hashlib.sha256(input_bytes).hexdigest()
        if len(input_bytes) > CANDIDATE_MAX_INPUT_BYTES:
            return _failure(
                request,
                "CANDIDATE_INPUT_LIMIT_EXCEEDED",
                "候选 JSON 输入超过 64 KiB 限制",
                candidate_id=manifest.plugin_id,
                version=manifest.version,
                source_hash=source_hash,
                input_hash=input_hash,
                status="rejected",
            )
        input_scope_error = _validate_approved_input(request.input_data, grant)
        if input_scope_error is not None:
            return _failure(
                request,
                input_scope_error[0],
                input_scope_error[1],
                candidate_id=manifest.plugin_id,
                version=manifest.version,
                source_hash=source_hash,
                input_hash=input_hash,
                status="rejected",
            )
        run_id = (
            "candidate-trial-"
            + hashlib.sha256(
                f"{request.session_id}\0{request.project_id}\0{request.task_id}\0{request.trial_id}".encode()
            ).hexdigest()[:32]
        )
        operation_key = self._operation_key(request)
        operation_hash = self._operation_hash(request, source_hash, input_hash, manifest)
        replay, reserved = self._reserve_operation(
            request,
            operation_key,
            operation_hash,
            candidate_id=manifest.plugin_id,
            version=manifest.version,
            run_id=run_id,
            source_hash=source_hash,
            input_hash=input_hash,
            budget_task_id=budget_task_id,
        )
        if replay is not None:
            return replay
        if not reserved:
            return _failure(
                request,
                "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                "候选试运行状态无法持久化",
                candidate_id=manifest.plugin_id,
                version=manifest.version,
                run_id=run_id,
                source_hash=source_hash,
                input_hash=input_hash,
            )

        def finish(result: CandidateExecutionResult) -> CandidateExecutionResult:
            if result.operation_id is None:
                result = replace(result, operation_id=operation_key)
            return self._complete_operation(request, operation_key, operation_hash, result)

        try:
            (
                reserved,
                _trials,
                _reserved_seconds,
                lease_token,
            ) = self._store.reserve_candidate_budget(
                budget_task_id,
                max_calls,
                max_seconds,
                self._timeout_seconds,
                operation_key,
            )
        except Exception:
            return finish(
                _failure(
                    request,
                    "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                    "候选任务预算无法原子预留",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                )
            )
        if not reserved:
            return finish(
                _failure(
                    request,
                    "CANDIDATE_BUDGET_EXCEEDED",
                    "候选试运行已达到任务预算",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                )
            )
        if not self._store.validate_candidate_lease(
            budget_task_id, operation_key, lease_token
        ):
            return finish(
                _failure(
                    request,
                    "CANDIDATE_LEASE_LOST",
                    "候选执行租约已失效，未启动 FC 运行",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    operation_id=operation_key,
                    status="unknown",
                )
            )
        if not self._update_operation_progress(
            operation_key, {"lease_token": lease_token}
        ):
            return finish(
                _failure(
                    request,
                    "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                    "候选执行租约无法持久化",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    operation_id=operation_key,
                    status="unknown",
                )
            )
        execution_request = ExecutionRequest(
            run_id=run_id,
            project_id=request.project_id,
            files=(
                ExecutionFile(_CANDIDATE_ENTRY, request.source.encode("utf-8")),
                ExecutionFile(_CANDIDATE_RUNNER, _FIXED_RUNNER_SOURCE),
                ExecutionFile(_INPUT_FILE, input_bytes),
            ),
            command="python3 /workspace/devpilot/candidate_runner.py",
            timeout_seconds=self._timeout_seconds,
            network_allowed=False,
            command_profile=CANDIDATE_COMMAND_PROFILE,
            max_output_bytes=CANDIDATE_MAX_OUTPUT_BYTES,
        )
        backend = self._backend
        assert backend is not None
        if request.cancel_requested is not None and request.cancel_requested():
            return finish(
                _failure(
                    request,
                    "CANDIDATE_CANCELLED",
                    "候选执行在派发 FC 前已取消",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    status="cancelled",
                )
            )
        try:
            result = backend.run(execution_request)
        except ExecutionError as exc:
            return finish(
                _failure(
                    request,
                    exc.code,
                    "候选 FC 试运行失败",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    cleanup_status=self._cleanup_status(run_id),
                )
            )
        except Exception:
            return finish(
                _failure(
                    request,
                    "CANDIDATE_EXECUTION_FAILED",
                    "候选执行后端返回未分类失败",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    cleanup_status=self._cleanup_status(run_id),
                )
            )
        receipt_error = _execution_receipt_error(result)
        if not self._store.validate_candidate_lease(
            budget_task_id, operation_key, lease_token
        ):
            return finish(
                _failure(
                    request,
                    "CANDIDATE_LEASE_LOST",
                    "候选执行租约已失效，FC 结果保持未知",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    cleanup_status=self._cleanup_status(run_id),
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                    status="unknown",
                )
            )
        if receipt_error is not None:
            return finish(
                _failure(
                    request,
                    receipt_error[0],
                    receipt_error[1],
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    cleanup_status=self._cleanup_status(run_id),
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                )
            )
        if not self._update_operation_progress(
            operation_key, {"sandbox_id": result.sandbox_id}
        ):
            return finish(
                _failure(
                    request,
                    "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                    "候选沙箱标识无法持久化",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                )
            )
        cleanup_status = self._cleanup_status(run_id)
        if cleanup_status != "cleaned":
            return finish(
                _failure(
                    request,
                    "CANDIDATE_CLEANUP_UNCONFIRMED",
                    "候选沙箱清理状态未确认，结果不能视为成功",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                )
            )
        if (
            result.output_truncated
            or len(result.stdout.encode("utf-8")) > CANDIDATE_MAX_OUTPUT_BYTES
        ):
            return finish(
                _failure(
                    request,
                    "CANDIDATE_OUTPUT_LIMIT_EXCEEDED",
                    "候选输出超过 64 KiB 限制",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                    output_truncated=True,
                )
            )
        if result.exit_code == _CANDIDATE_OUTPUT_LIMIT_EXIT_CODE:
            return finish(
                _failure(
                    request,
                    "CANDIDATE_OUTPUT_LIMIT_EXCEEDED",
                    "候选输出超过 64 KiB 限制",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                    output_truncated=True,
                )
            )
        if result.exit_code != 0:
            return finish(
                _failure(
                    request,
                    "CANDIDATE_EXECUTION_FAILED",
                    "候选程序返回非零退出码",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                )
            )
        try:
            output = json.loads(result.stdout)
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, json.JSONDecodeError):
            return finish(
                _failure(
                    request,
                    "CANDIDATE_OUTPUT_INVALID",
                    "候选未返回合法 JSON 结果",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                )
            )
        return finish(
            CandidateExecutionResult(
                status="passed",
                candidate_id=manifest.plugin_id,
                version=manifest.version,
                trial_id=request.trial_id,
                run_id=run_id,
                backend=result.backend,
                sandbox_id=result.sandbox_id,
                exit_code=result.exit_code,
                output=redact_sensitive_payload(output),
                error_code=None,
                error_message=None,
                duration_ms=result.duration_ms,
                cleanup_status=cleanup_status,
                source_hash=source_hash,
                input_hash=input_hash,
            )
        )

    @staticmethod
    def _execution_from_mapping(payload: Mapping[str, object]) -> ExecutionResult | None:
        required = {"backend", "sandbox_id", "exit_code", "stdout", "stderr", "duration_ms"}
        if not required.issubset(payload):
            return None
        if not all(
            isinstance(payload.get(field), str)
            for field in ("backend", "sandbox_id", "stdout", "stderr")
        ):
            return None
        if not all(
            isinstance(payload.get(field), int) and not isinstance(payload.get(field), bool)
            for field in ("exit_code", "duration_ms")
        ):
            return None
        bundle_bytes = payload.get("bundle_bytes", 0)
        output_truncated = payload.get("output_truncated", False)
        optional_strings = ("command_profile", "region", "template", "network_policy")
        if not isinstance(bundle_bytes, int) or isinstance(bundle_bytes, bool) or bundle_bytes < 0:
            return None
        if not isinstance(output_truncated, bool):
            return None
        if any(
            not isinstance(payload.get(field, ""), str) for field in optional_strings
        ):
            return None
        return ExecutionResult(
            backend=payload["backend"],
            sandbox_id=payload["sandbox_id"],
            exit_code=payload["exit_code"],
            stdout=payload["stdout"],
            stderr=payload["stderr"],
            duration_ms=payload["duration_ms"],
            command_profile=payload.get("command_profile", ""),
            bundle_bytes=bundle_bytes,
            region=payload.get("region", ""),
            template=payload.get("template", ""),
            network_policy=payload.get("network_policy", ""),
            output_truncated=output_truncated,
        )

    def _finalize_execution(
        self,
        request: CandidateTrialRequest,
        operation_key: str,
        operation_hash: str,
        candidate_id: str | None,
        version: str | None,
        source_hash: str | None,
        input_hash: str | None,
        run_id: str,
        result: ExecutionResult,
    ) -> CandidateExecutionResult:
        receipt_error = _execution_receipt_error(result)
        if receipt_error is not None:
            return self._complete_operation(
                request,
                operation_key,
                operation_hash,
                _failure(
                    request,
                    receipt_error[0],
                    receipt_error[1],
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                    operation_id=operation_key,
                ),
            )
        cleanup_status = self._cleanup_status(run_id)
        if not cleanup_status:
            cleanup_status = "unknown"
        if cleanup_status != "cleaned":
            return self._complete_operation(
                request,
                operation_key,
                operation_hash,
                _failure(
                    request,
                    "CANDIDATE_CLEANUP_UNCONFIRMED",
                    "候选沙箱清理状态未确认，结果不能视为成功",
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                    operation_id=operation_key,
                ),
            )
        if result.output_truncated or (
            len(result.stdout.encode("utf-8")) > CANDIDATE_MAX_OUTPUT_BYTES
        ):
            return self._complete_operation(
                request,
                operation_key,
                operation_hash,
                _failure(
                    request,
                    "CANDIDATE_OUTPUT_LIMIT_EXCEEDED",
                    "候选输出超过 64 KiB 限制",
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                    output_truncated=True,
                    operation_id=operation_key,
                ),
            )
        if result.exit_code == _CANDIDATE_OUTPUT_LIMIT_EXIT_CODE or result.exit_code != 0:
            code = (
                "CANDIDATE_OUTPUT_LIMIT_EXCEEDED"
                if result.exit_code == _CANDIDATE_OUTPUT_LIMIT_EXIT_CODE
                else "CANDIDATE_EXECUTION_FAILED"
            )
            message = (
                "候选输出超过 64 KiB 限制"
                if code == "CANDIDATE_OUTPUT_LIMIT_EXCEEDED"
                else "候选程序返回非零退出码"
            )
            return self._complete_operation(
                request,
                operation_key,
                operation_hash,
                _failure(
                    request,
                    code,
                    message,
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                    output_truncated=code == "CANDIDATE_OUTPUT_LIMIT_EXCEEDED",
                    operation_id=operation_key,
                ),
            )
        try:
            output = json.loads(result.stdout)
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._complete_operation(
                request,
                operation_key,
                operation_hash,
                _failure(
                    request,
                    "CANDIDATE_OUTPUT_INVALID",
                    "候选未返回合法 JSON 结果",
                    candidate_id=candidate_id,
                    version=version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                    duration_ms=result.duration_ms,
                    cleanup_status=cleanup_status,
                    exit_code=result.exit_code,
                    backend=result.backend,
                    sandbox_id=result.sandbox_id,
                    operation_id=operation_key,
                ),
            )
        return self._complete_operation(
            request,
            operation_key,
            operation_hash,
            CandidateExecutionResult(
                status="passed",
                candidate_id=candidate_id,
                version=version,
                trial_id=request.trial_id,
                run_id=run_id,
                backend=result.backend,
                sandbox_id=result.sandbox_id,
                exit_code=result.exit_code,
                output=redact_sensitive_payload(output),
                error_code=None,
                error_message=None,
                duration_ms=result.duration_ms,
                cleanup_status=cleanup_status,
                source_hash=source_hash,
                input_hash=input_hash,
                operation_id=operation_key,
            ),
        )

    def _cleanup_status(self, run_id: str) -> str:
        if self._backend is None:
            return "not_started"
        getter = getattr(self._backend, "get_run_state", None)
        if not callable(getter):
            return "not_reported"
        try:
            state = getter(run_id)
        except Exception:
            return "unknown"
        if not isinstance(state, Mapping):
            return "unknown"
        value = state.get("cleanup_status")
        return value if isinstance(value, str) else "unknown"
