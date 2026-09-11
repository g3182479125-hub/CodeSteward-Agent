"""Bounded FC execution for model-generated pure-computation candidates.

This module is deliberately separate from ChangeSet verification.  A model
candidate is uploaded as data to an injected execution backend and is never
imported by the DevPilot host.  The fixed runner is a contract adapter; FC is
the execution boundary, not the AST checks below.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from time import time
from typing import Protocol

from .execution import (
    ExecutionBackend,
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
)
from .redaction import is_sensitive_field_name, redact_sensitive_payload, redact_sensitive_text

CANDIDATE_MAX_TRIALS = 3
CANDIDATE_MAX_TRIAL_SECONDS = 120
CANDIDATE_TIMEOUT_SECONDS = 30
CANDIDATE_MAX_OUTPUT_BYTES = 64 * 1024
CANDIDATE_MAX_SOURCE_BYTES = 64 * 1024
CANDIDATE_MAX_INPUT_BYTES = 64 * 1024
CANDIDATE_API_VERSION = "nanobot.tool/v1"
CANDIDATE_COMMAND_PROFILE = "model-candidate"
_CANDIDATE_ENTRY = "candidate.py"
_CANDIDATE_RUNNER = "candidate_runner.py"
_INPUT_FILE = "input.json"
_CANDIDATE_OUTPUT_LIMIT_EXIT_CODE = 86
_BUDGET_KEY_PREFIX = "candidate-budget-"
_SENSITIVE_INPUT_NAME = frozenset(
    {"database", "db", "connection_string", "dsn", "session", "cookie", "headers", "env"}
)
_LOCAL_REFERENCE_PATTERN = re.compile(r"(?i)(?:file://|[A-Za-z]:[\\/]|\\\\|(?<![A-Za-z0-9])/[^/])")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_TOOL_CAPABILITY_PATTERN = re.compile(r"^tool:[a-z][a-z0-9._-]{0,63}$")
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


@dataclass(frozen=True, slots=True)
class CandidateTrialGrant:
    """Host-issued scope for one session's approved candidate input projection."""

    grant_id: str
    session_id: str
    task_id: str
    project_id: str
    allowed_input_fields: tuple[str, ...]
    expires_at_epoch: int | None = None


class CandidateTrialStore(Protocol):
    """Persistence port backed by DevPilot's existing idempotency table."""

    def reserve_idempotency(self, key: str, request_hash: str) -> tuple[str, str | None]: ...

    def complete_idempotency(self, key: str, response_json: str) -> None: ...

    def get_idempotency(self, key: str) -> tuple[str, str | None] | None: ...


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
        }


@dataclass(frozen=True, slots=True)
class _CandidateManifest:
    plugin_id: str
    version: str
    content_hash: str


@dataclass(slots=True)
class _TaskBudget:
    trials: int = 0
    reserved_seconds: int = 0


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
    if manifest.get("kind") != "tool":
        return None, ("CANDIDATE_KIND_INVALID", "模型候选首版仅支持纯计算 tool")
    if manifest.get("api_version") != CANDIDATE_API_VERSION:
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
    if (
        provides is None
        or len(provides) != 1
        or not _TOOL_CAPABILITY_PATTERN.fullmatch(provides[0])
    ):
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
        "trial_id",
        "duration_ms",
        "cleanup_status",
        "output_truncated",
    }
    if not required.issubset(payload):
        return None
    if not all(
        isinstance(payload.get(field), str) for field in {"status", "trial_id", "cleanup_status"}
    ):
        return None
    if not isinstance(payload.get("duration_ms"), int) or isinstance(
        payload.get("duration_ms"), bool
    ):
        return None
    if not isinstance(payload.get("output_truncated"), bool):
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
    )


def _validate_grant(request: CandidateTrialRequest) -> tuple[str, str] | None:
    grant = request.grant
    if not isinstance(grant, CandidateTrialGrant):
        return "CANDIDATE_GRANT_REQUIRED", "候选试运行必须使用 Host 签发的试用授权"
    if (
        grant.session_id != request.session_id
        or grant.task_id != request.task_id
        or grant.project_id != request.project_id
    ):
        return "CANDIDATE_GRANT_SCOPE_INVALID", "候选试用授权与当前会话或项目不匹配"
    if not grant.grant_id or not grant.allowed_input_fields:
        return "CANDIDATE_GRANT_INVALID", "候选试用授权缺少输入字段投影"
    if len(set(grant.allowed_input_fields)) != len(grant.allowed_input_fields) or any(
        not isinstance(field, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", field)
        for field in grant.allowed_input_fields
    ):
        return "CANDIDATE_GRANT_INVALID", "候选试用授权的输入字段投影无效"
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
    if any(field not in allowed for field in fields):
        return "CANDIDATE_INPUT_SCOPE_INVALID", "候选输入包含未获授权的字段"
    if any(field.casefold() in _SENSITIVE_INPUT_NAME for field in fields):
        return "CANDIDATE_INPUT_SENSITIVE", "候选输入包含不允许的数据类别"
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
        store: CandidateTrialStore | None = None,
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
        self._lock = RLock()
        self._budgets: dict[str, _TaskBudget] = {}
        self._operations: dict[str, tuple[str, CandidateExecutionResult | None]] = {}

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
        with self._lock:
            budget = self._budgets.get(task_id)
            if budget is None:
                budget = self._load_persisted_budget(task_id)
                self._budgets[task_id] = budget
        return {
            "trials": budget.trials,
            "reserved_seconds": budget.reserved_seconds,
            "max_trials": self._max_trials,
            "max_trial_seconds": self._max_trial_seconds,
        }

    def _budget_key(self, task_id: str) -> str:
        digest = hashlib.sha256(task_id.encode()).hexdigest()
        return _BUDGET_KEY_PREFIX + digest

    def _operation_key(self, request: CandidateTrialRequest) -> str:
        digest = hashlib.sha256(
            f"{request.session_id}\0{request.task_id}\0{request.trial_id}".encode()
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
    ) -> tuple[CandidateExecutionResult | None, bool]:
        if self._store is not None:
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
                    ),
                    False,
                )
            if state == "replay":
                try:
                    decoded = json.loads(response_json or "")
                except (TypeError, ValueError):
                    decoded = None
                replay = _result_from_dict(decoded)
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
                        ),
                        False,
                    )
                return replay, False
            if state == "in_progress":
                return (
                    _failure(
                        request,
                        "CANDIDATE_TRIAL_IN_PROGRESS",
                        "相同候选试运行仍在处理中，未自动重试",
                        candidate_id=candidate_id,
                        version=version,
                        run_id=run_id,
                        source_hash=source_hash,
                        input_hash=input_hash,
                    ),
                    False,
                )
            return None, True
        with self._lock:
            existing = self._operations.get(operation_key)
            if existing is not None:
                if existing[0] != operation_hash:
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
                            status="rejected",
                        ),
                        False,
                    )
                if existing[1] is not None:
                    return existing[1], False
                return (
                    _failure(
                        request,
                        "CANDIDATE_TRIAL_IN_PROGRESS",
                        "相同候选试运行仍在处理中，未自动重试",
                        candidate_id=candidate_id,
                        version=version,
                        run_id=run_id,
                        source_hash=source_hash,
                        input_hash=input_hash,
                    ),
                    False,
                )
            self._operations[operation_key] = (operation_hash, None)
        return None, True

    def _complete_operation(
        self,
        request: CandidateTrialRequest,
        operation_key: str,
        operation_hash: str,
        result: CandidateExecutionResult,
    ) -> CandidateExecutionResult:
        if self._store is None:
            with self._lock:
                self._operations[operation_key] = (operation_hash, result)
            return result
        try:
            serialized = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
            self._store.complete_idempotency(operation_key, serialized)
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
            )
        return result

    def _load_persisted_budget(self, task_id: str) -> _TaskBudget:
        if self._store is None:
            return _TaskBudget()
        record = self._store.get_idempotency(self._budget_key(task_id))
        if record is None or record[1] is None:
            return _TaskBudget()
        try:
            payload = json.loads(record[1])
        except (TypeError, ValueError):
            return _TaskBudget()
        if not isinstance(payload, Mapping):
            return _TaskBudget()
        trials = payload.get("trials")
        reserved = payload.get("reserved_seconds")
        if not (
            isinstance(trials, int)
            and not isinstance(trials, bool)
            and isinstance(reserved, int)
            and not isinstance(reserved, bool)
            and trials >= 0
            and reserved >= 0
        ):
            return _TaskBudget()
        return _TaskBudget(trials=trials, reserved_seconds=reserved)

    def _persist_budget(self, task_id: str, budget: _TaskBudget) -> None:
        if self._store is None:
            return
        key = self._budget_key(task_id)
        payload = json.dumps(
            {"trials": budget.trials, "reserved_seconds": budget.reserved_seconds},
            sort_keys=True,
        )
        record = self._store.get_idempotency(key)
        if record is None:
            state, _ = self._store.reserve_idempotency(
                key, hashlib.sha256(f"budget:{task_id}".encode()).hexdigest()
            )
            if state != "new":
                raise RuntimeError("candidate budget reservation is not new")
        self._store.complete_idempotency(key, payload)

    def _run(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        if not request.task_id or not request.project_id or not request.trial_id:
            return _failure(request, "CANDIDATE_SCOPE_INVALID", "候选任务作用域不完整")
        if not isinstance(request.source, str):
            return _failure(request, "CANDIDATE_SOURCE_INVALID", "候选源码必须是文本")
        if not isinstance(request.manifest, Mapping):
            return _failure(request, "CANDIDATE_MANIFEST_INVALID", "候选 manifest 必须是对象")
        grant = request.grant
        grant_error = _validate_grant(request)
        if grant_error is not None:
            return _failure(request, grant_error[0], grant_error[1], status="rejected")
        assert isinstance(grant, CandidateTrialGrant)
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
            + hashlib.sha256(f"{request.task_id}\0{request.trial_id}".encode()).hexdigest()[:32]
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
            return self._complete_operation(request, operation_key, operation_hash, result)

        with self._lock:
            budget = self._budgets.get(request.task_id)
            if budget is None:
                try:
                    budget = self._load_persisted_budget(request.task_id)
                except Exception:
                    return finish(
                        _failure(
                            request,
                            "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                            "候选任务预算无法读取",
                            candidate_id=manifest.plugin_id,
                            version=manifest.version,
                            run_id=run_id,
                            source_hash=source_hash,
                            input_hash=input_hash,
                        )
                    )
                self._budgets[request.task_id] = budget
            if (
                budget.trials >= self._max_trials
                or budget.reserved_seconds + self._timeout_seconds > self._max_trial_seconds
            ):
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
            old_trials = budget.trials
            old_reserved_seconds = budget.reserved_seconds
            budget.trials += 1
            budget.reserved_seconds += self._timeout_seconds
            try:
                self._persist_budget(request.task_id, budget)
            except Exception:
                budget.trials = old_trials
                budget.reserved_seconds = old_reserved_seconds
                return finish(
                    _failure(
                        request,
                        "CANDIDATE_PERSISTENCE_UNAVAILABLE",
                        "候选任务预算无法持久化",
                        candidate_id=manifest.plugin_id,
                        version=manifest.version,
                        run_id=run_id,
                        source_hash=source_hash,
                        input_hash=input_hash,
                    )
                )
        if not self._has_candidate_backend():
            return finish(
                _failure(
                    request,
                    "CANDIDATE_BACKEND_UNAVAILABLE",
                    "候选试运行未绑定受信 FC 后端，未执行本机回退",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
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
        with self._lock:
            try:
                result = self._backend.run(execution_request)
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

    def _has_candidate_backend(self) -> bool:
        backend = self._backend
        return (
            backend is not None
            and getattr(backend, "name", None) == "fc-agent-sandbox"
            and getattr(backend, "candidate_execution_available", False) is True
        )
