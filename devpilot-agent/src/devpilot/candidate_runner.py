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
from threading import Lock

from .execution import (
    ExecutionBackend,
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
)
from .redaction import is_sensitive_field_name, redact_sensitive_payload

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
_FIXED_RUNNER_SOURCE = b'''import json

from candidate import transform


with open("/workspace/devpilot/input.json", encoding="utf-8") as input_file:
    payload = json.load(input_file)
result = transform(payload)
serialized = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
if len(serialized.encode("utf-8")) > 65536:
    raise SystemExit(86)
print(serialized)
'''


@dataclass(frozen=True, slots=True)
class CandidateTrialRequest:
    """A model candidate plus only the approved JSON data for one task."""

    task_id: str
    project_id: str
    trial_id: str
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
    capabilities = _mapping_sequence(
        manifest.get("requested_capabilities")
    )
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
    if len(functions) != 1 or functions[0].name != "transform" or any(
        not isinstance(node, ast.FunctionDef) for node in top_level
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


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


class CandidateTrialRunner:
    """Run model candidates through one bounded, injected execution backend."""

    def __init__(
        self,
        backend: ExecutionBackend | None,
        *,
        max_trials: int = CANDIDATE_MAX_TRIALS,
        max_trial_seconds: int = CANDIDATE_MAX_TRIAL_SECONDS,
        timeout_seconds: int = CANDIDATE_TIMEOUT_SECONDS,
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
        self._lock = Lock()
        self._budgets: dict[str, _TaskBudget] = {}

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
            budget = self._budgets.get(task_id, _TaskBudget())
            return {
                "trials": budget.trials,
                "reserved_seconds": budget.reserved_seconds,
                "max_trials": self._max_trials,
                "max_trial_seconds": self._max_trial_seconds,
            }

    def _run(self, request: CandidateTrialRequest) -> CandidateExecutionResult:
        if not request.task_id or not request.project_id or not request.trial_id:
            return _failure(request, "CANDIDATE_SCOPE_INVALID", "候选任务作用域不完整")
        if not isinstance(request.source, str):
            return _failure(request, "CANDIDATE_SOURCE_INVALID", "候选源码必须是文本")
        if not isinstance(request.manifest, Mapping):
            return _failure(request, "CANDIDATE_MANIFEST_INVALID", "候选 manifest 必须是对象")
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
        if _contains_sensitive_field(request.input_data):
            return _failure(
                request,
                "CANDIDATE_INPUT_SENSITIVE",
                "候选输入包含疑似凭据字段，已拒绝上传",
                candidate_id=manifest.plugin_id,
                version=manifest.version,
                source_hash=source_hash,
                input_hash=input_hash,
                status="rejected",
        )
        run_id = "candidate-trial-" + hashlib.sha256(
            f"{request.task_id}\0{request.trial_id}".encode()
        ).hexdigest()[:32]
        with self._lock:
            budget = self._budgets.setdefault(request.task_id, _TaskBudget())
            if (
                budget.trials >= self._max_trials
                or budget.reserved_seconds + self._timeout_seconds > self._max_trial_seconds
            ):
                return _failure(
                    request,
                    "CANDIDATE_BUDGET_EXCEEDED",
                    "候选试运行已达到任务预算",
                    candidate_id=manifest.plugin_id,
                    version=manifest.version,
                    run_id=run_id,
                    source_hash=source_hash,
                    input_hash=input_hash,
                )
            budget.trials += 1
            budget.reserved_seconds += self._timeout_seconds
        if self._backend is None:
            return _failure(
                request,
                "CANDIDATE_BACKEND_UNAVAILABLE",
                "候选试运行后端不可用，未执行本机回退",
                candidate_id=manifest.plugin_id,
                version=manifest.version,
                run_id=run_id,
                source_hash=source_hash,
                input_hash=input_hash,
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
                return _failure(
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
            except Exception:
                return _failure(
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
            cleanup_status = self._cleanup_status(run_id)
        if (
            result.output_truncated
            or len(result.stdout.encode("utf-8")) > CANDIDATE_MAX_OUTPUT_BYTES
        ):
            return _failure(
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
        if result.exit_code == _CANDIDATE_OUTPUT_LIMIT_EXIT_CODE:
            return _failure(
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
        if result.exit_code != 0:
            return _failure(
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
        try:
            output = json.loads(result.stdout)
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _failure(
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
        return CandidateExecutionResult(
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
