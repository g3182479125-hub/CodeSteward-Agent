"""Live model-candidate plugins for the H6 harness extension.

The host owns the catalog, authorization and route switch.  Candidate source is
stored as an immutable artifact and is sent to the existing H5 FC runner for
both trial and live calls; this module never imports candidate Python.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from nanobot.agent.plugins.catalog import PluginRecord
from nanobot.agent.plugins.contracts import (
    DynamicPluginPolicy,
    PluginCandidateStatus,
    PluginExecutionMode,
    PluginKind,
    PluginManifest,
    PluginScope,
    PluginSource,
)
from nanobot.agent.plugins.manager import PluginManager, ScopeKey, _RegisteredPlugin
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context

from .candidate_runner import (
    CANDIDATE_API_VERSION,
    CANDIDATE_COMMAND_PROFILE,
    CANDIDATE_MAX_EXECUTION_SECONDS,
    CANDIDATE_MAX_EXECUTIONS,
    CANDIDATE_NETWORK_POLICY,
    CANDIDATE_TEMPLATE,
    CANDIDATE_TIMEOUT_SECONDS,
    CandidateInputRule,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
    CandidateTrialRunner,
    _validate_source,
)
from .fc_sandbox import build_fc_backend_from_environment
from .storage import ProjectStore

if TYPE_CHECKING:
    pass


_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MAX_RULES = 12
_AUTH_TTL_SECONDS = 1800
_DEFAULT_RULES: tuple[CandidateInputRule, ...] = (("value", "string", 4096, None, None),)
_SENSITIVE_FIELDS = frozenset(
    {"api_key", "apikey", "token", "secret", "password", "authorization", "headers", "env"}
)
_INPUT_RULE_KINDS = ("string", "integer", "number", "boolean")
_MODEL_CANDIDATE_POLICY = DynamicPluginPolicy(
    enabled=True,
    allowed_kinds=frozenset({"tool"}),
    allowed_capabilities=frozenset(),
    allowed_actions=frozenset({"activate", "stop", "remove"}),
    allowed_scopes=frozenset({"session", "project"}),
    max_active_plugins=3,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _error(code: str, message: str, **details: object) -> str:
    payload: dict[str, object] = {"ok": False, "code": code, "message": message}
    safe_details = {
        key: value
        for key, value in details.items()
        if isinstance(value, (str, int, float, bool))
    }
    if safe_details:
        payload["details"] = safe_details
    return _json(payload)


def _result(value: object) -> str:
    return _json({"ok": True, "result": value})


def _rules_schema(rules: tuple[CandidateInputRule, ...]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field, kind, max_length, min_value, max_value in rules:
        schema: dict[str, Any] = {"type": kind}
        if kind == "string":
            schema["maxLength"] = max_length
        if min_value is not None:
            schema["minimum"] = min_value
        if max_value is not None:
            schema["maximum"] = max_value
        properties[field] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


def _rules_view(rules: tuple[CandidateInputRule, ...]) -> list[dict[str, object]]:
    return [
        {
            "field": field,
            "kind": kind,
            "max_length": max_length,
            "min_value": min_value,
            "max_value": max_value,
        }
        for field, kind, max_length, min_value, max_value in rules
    ]


def _candidate_contract_hash(
    manifest: Mapping[str, object],
    rules: tuple[CandidateInputRule, ...],
) -> str:
    """Hash every sidecar and fixed execution setting that trial approves."""
    payload = {
        "manifest": dict(manifest),
        "input_rules": _rules_view(rules),
        "execution": {
            "profile": CANDIDATE_COMMAND_PROFILE,
            "template": CANDIDATE_TEMPLATE,
            "network_policy": CANDIDATE_NETWORK_POLICY,
        },
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _fixture_value(rule: CandidateInputRule) -> object:
    _field, kind, _max_length, min_value, _max_value = rule
    if kind == "string":
        return "fixture"
    if kind == "boolean":
        return False
    if min_value is not None:
        return min_value
    return 1 if kind == "integer" else 1.0


def _fixed_trial_input(rules: tuple[CandidateInputRule, ...]) -> dict[str, object]:
    """Create deterministic fixture data; model-supplied trial data is ignored."""
    return {rule[0]: _fixture_value(rule) for rule in rules}


def _parse_rules(raw: object) -> tuple[CandidateInputRule, ...]:
    if raw is None:
        return _DEFAULT_RULES
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ValueError("input_rules must be a list")
    if not 1 <= len(raw) <= _MAX_RULES:
        raise ValueError("input_rules must contain between 1 and 12 items")
    result: list[CandidateInputRule] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("each input rule must be an object")
        field = item.get("field")
        kind = item.get("kind")
        max_length = item.get("max_length")
        min_value = item.get("min_value")
        max_value = item.get("max_value")
        if (
            not isinstance(field, str)
            or _FIELD.fullmatch(field) is None
            or field.casefold() in _SENSITIVE_FIELDS
            or field in seen
            or kind not in set(_INPUT_RULE_KINDS)
            or not isinstance(max_length, int)
            or isinstance(max_length, bool)
            or not 1 <= max_length <= 65536
        ):
            raise ValueError("input rule is invalid or requests a sensitive field")
        if kind in {"string", "boolean"} and (min_value is not None or max_value is not None):
            raise ValueError("string and boolean rules cannot have numeric limits")
        if kind == "integer" and any(
            value is not None and (not isinstance(value, int) or isinstance(value, bool))
            for value in (min_value, max_value)
        ):
            raise ValueError("integer rule limits must be integers")
        if kind == "number" and any(
            value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool))
            for value in (min_value, max_value)
        ):
            raise ValueError("number rule limits must be numbers")
        if min_value is not None and max_value is not None and min_value > max_value:
            raise ValueError("input rule minimum cannot exceed maximum")
        seen.add(field)
        result.append(cast(CandidateInputRule, (field, kind, max_length, min_value, max_value)))
    return tuple(result)


class ModelCandidateController:
    """Host-owned H6 catalog adapter and FC-proxy factory."""

    def __init__(
        self,
        workspace: Path,
        runner: CandidateTrialRunner | None = None,
        grant_issuer: CandidateTrialGrantIssuer | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.data_dir = Path(
            os.environ.get("DEVPILOT_DATA_DIR", str(Path.home() / ".nanobot-skill-improvement"))
        ).expanduser().resolve()
        self._manager: PluginManager | None = None
        self._runner = runner
        self._issuer: CandidateTrialGrantIssuer | None = grant_issuer
        self._runner_error: tuple[str, str] | None = None
        self._runner_lock = threading.Lock()
        self._authorizations: dict[tuple[str, str], dict[str, object]] = {}

    def bind_plugin_manager(self, manager: PluginManager) -> None:
        self._manager = manager

    def _context(self) -> tuple[Any, str, str, str, str] | None:
        request = current_request_context()
        if request is None or not request.sender_id:
            return None
        session_id = request.session_key or request.chat_id
        workspace = request.workspace or self.workspace
        project_id = request.attributes.get("devpilot_project_id")
        if not isinstance(project_id, str) or not project_id:
            project_id = PluginManager.project_id_for_path(workspace)
        task_id = request.attributes.get("devpilot_task_id")
        if not isinstance(task_id, str) or not task_id:
            task_id = "session-task-" + hashlib.sha256(session_id.encode()).hexdigest()[:16]
        return request, session_id, project_id, task_id, str(workspace)

    def _ensure_runner(self) -> CandidateTrialRunner | None:
        if self._runner is not None:
            return self._runner
        with self._runner_lock:
            if self._runner is not None:
                return self._runner
            if os.environ.get("DEVPILOT_ENABLE_MODEL_CANDIDATES") != "1":
                self._runner_error = (
                    "MODEL_CANDIDATES_DISABLED",
                    "模型候选插件未启用；设置 DEVPILOT_ENABLE_MODEL_CANDIDATES=1 后重启 nanobot",
                )
                return None
            try:
                secret = os.environ.get("DEVPILOT_CANDIDATE_GRANT_SECRET", "").encode("utf-8")
                self._issuer = CandidateTrialGrantIssuer(secret)
                backend = build_fc_backend_from_environment()
                store = ProjectStore(self.data_dir / "devpilot.db")
                self._runner = CandidateTrialRunner(
                    backend,
                    store=store,
                    grant_issuer=self._issuer,
                )
            except Exception:
                self._runner_error = (
                    "CANDIDATE_RUNNER_UNAVAILABLE",
                    "模型候选插件未绑定可用的 FC 试用执行器；未执行本机回退",
                )
                return None
        return self._runner

    def _runner_failure(self) -> str:
        code, message = self._runner_error or (
            "CANDIDATE_RUNNER_UNAVAILABLE",
            "模型候选插件未绑定可用的 FC 试用执行器；未执行本机回退",
        )
        return _error(code, message)

    def _require_manager(self) -> PluginManager:
        if self._manager is None:
            raise RuntimeError("model candidate manager is not bound")
        return self._manager

    def _registration(
        self,
        plugin_id: str,
        version: str,
        context: tuple[Any, str, str, str, str],
    ) -> _RegisteredPlugin | None:
        _request, session_id, project_id, _task_id, _workspace = context
        return self._require_manager()._find_visible(plugin_id, version, session_id, project_id)

    def _authorize(self, context: tuple[Any, str, str, str, str]) -> str | None:
        request, session_id, project_id, _task_id, _workspace = context
        if request.attributes.get("devpilot_model_candidates_enabled") is not True:
            return _error(
                "MODEL_CANDIDATES_PROFILE_DISABLED",
                "当前任务 Profile 未启用模型候选插件；不能仅靠聊天确认绕过配置",
            )
        manager = self._require_manager()
        key = (session_id, project_id)
        grant = self._authorizations.get(key)
        now = int(time.time())
        if (
            grant is not None
            and grant.get("sender_id") == request.sender_id
            and grant.get("task_id") == _task_id
            and isinstance(grant.get("expires_at"), int)
            and int(grant["expires_at"]) > now
        ):
            return None
        if grant is not None:
            self._authorizations.pop(key, None)
            manager.revoke_authorization(session_id)
        confirmation = (request.original_user_text or "").strip().casefold()
        explicit = confirmation in {
            "确认试用",
            "确认启用",
            "确认插件",
            "执行试用",
            "confirm plugin",
            "approve plugin",
        }
        if not explicit:
            return _error(
                "PLUGIN_AUTHORIZATION_REQUIRED",
                "请先在 nanobot 对话中明确回复‘确认试用’或‘确认启用’，再执行候选插件动作",
                requires_user_confirmation=True,
            )
        # Model candidates are session-scoped.  Do not turn one chat's explicit
        # confirmation into a project-wide dynamic-routing grant.
        manager.authorize(session_id)
        self._authorizations[key] = {
            "sender_id": request.sender_id,
            "task_id": _task_id,
            "expires_at": now + _AUTH_TTL_SECONDS,
        }
        return None

    def _artifact(
        self,
        registration: _RegisteredPlugin,
    ) -> tuple[str, dict[str, object], tuple[CandidateInputRule, ...], str]:
        root = registration.artifact_root
        manifest = registration.record.manifest
        entry = manifest.resolve_entry(root)
        source = entry.read_text(encoding="utf-8")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != manifest.content_hash:
            raise ValueError("candidate source hash changed")
        raw_manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        raw_rules = json.loads((root / "input-rules.json").read_text(encoding="utf-8"))
        if not isinstance(raw_manifest, dict) or raw_manifest != manifest.to_dict():
            raise ValueError("candidate manifest sidecar changed")
        rules = _parse_rules(raw_rules)
        contract_hash = _candidate_contract_hash(raw_manifest, rules)
        return source, cast(dict[str, object], raw_manifest), rules, contract_hash

    def build_proxy(self, registration: _RegisteredPlugin, _scope_key: ScopeKey) -> Tool:
        _source, _manifest, rules, contract_hash = self._artifact(registration)
        manager = self._require_manager()
        if not manager.candidate_binding_matches(
            registration.record.manifest.plugin_id,
            registration.record.manifest.version,
            session_id=registration.record.session_id or "",
            project_id=registration.record.project_id,
            contract_hash=contract_hash,
        ):
            raise ValueError("candidate trial binding is stale")
        return ModelCandidateProxyTool(self, registration, rules)

    def _manifest(self, plugin_id: str, version: str, source: str) -> PluginManifest:
        content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return PluginManifest.from_mapping(
            {
                "plugin_id": plugin_id,
                "version": version,
                "content_hash": content_hash,
                "kind": PluginKind.TOOL.value,
                "api_version": CANDIDATE_API_VERSION,
                "scope": PluginScope.SESSION.value,
                "entry": "candidate.py",
                "requires": [],
                "provides": [f"tool:{plugin_id}"],
                "requested_capabilities": [],
                "execution_mode": PluginExecutionMode.FC_PROXY.value,
            }
        )

    def define(self, kwargs: Mapping[str, object], context: tuple[Any, str, str, str, str]) -> str:
        plugin_id = kwargs.get("plugin_id")
        version = kwargs.get("version")
        source = kwargs.get("source")
        if not isinstance(plugin_id, str) or _PLUGIN_ID.fullmatch(plugin_id) is None:
            return _error("CANDIDATE_ID_INVALID", "plugin_id 必须是小写安全标识")
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
            return _error("CANDIDATE_VERSION_INVALID", "version 必须使用语义化版本，例如 1.0.0")
        if not isinstance(source, str):
            return _error("CANDIDATE_SOURCE_INVALID", "source 必须是 candidate.py 文本")
        source_error = _validate_source(source)
        if source_error is not None:
            return _error(source_error[0], source_error[1])
        try:
            rules = _parse_rules(kwargs.get("input_rules"))
            manifest = self._manifest(plugin_id, version, source)
        except (TypeError, ValueError):
            return _error("CANDIDATE_DEFINITION_INVALID", "候选定义或输入规则无效")
        _request, session_id, _project_id, _task_id, _workspace = context
        session_digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
        root = (
            self.data_dir
            / "model-candidates"
            / "session"
            / session_digest
            / plugin_id
            / version
            / manifest.content_hash
        )
        try:
            root.mkdir(parents=True, exist_ok=True)
            files = {
                "candidate.py": source,
                "manifest.json": _json(manifest.to_dict()),
                "input-rules.json": _json(_rules_view(rules)),
            }
            for name, content in files.items():
                path = root / name
                if path.exists() and path.read_text(encoding="utf-8") != content:
                    return _error(
                        "CANDIDATE_ARTIFACT_CONFLICT",
                        "候选版本 artifact 已存在但内容不一致",
                    )
                if not path.exists():
                    path.write_text(content, encoding="utf-8", newline="\n")
            record = PluginRecord(
                manifest,
                PluginSource.MODEL_CANDIDATE,
                session_id=session_id,
                candidate_status=PluginCandidateStatus.DEFINED,
            )
            result = self._require_manager().register_candidate_version(record, root)
            return _result(result.to_dict()) if result.ok else _json(result.to_dict())
        except (OSError, ValueError, TypeError):
            return _error("CANDIDATE_DEFINITION_FAILED", "候选 artifact 无法安全写入或注册")

    def inspect(self, context: tuple[Any, str, str, str, str]) -> str:
        _request, session_id, project_id, _task_id, _workspace = context
        manager = self._require_manager()
        return _result(
            {
                "status": (
                    "enabled"
                    if os.environ.get("DEVPILOT_ENABLE_MODEL_CANDIDATES") == "1"
                    else "disabled"
                ),
                "constraints": {
                    "execution": "fc_proxy",
                    "candidate_entry": "candidate.py::transform(payload)",
                    "max_source_bytes": 65536,
                    "max_trials": 3,
                    "max_trial_seconds": 120,
                    "live_execution": {
                        "max_calls": CANDIDATE_MAX_EXECUTIONS,
                        "max_seconds": CANDIDATE_MAX_EXECUTION_SECONDS,
                    },
                    "input_rules": _input_rule_item_schema(),
                    "restart_required_for_plugin_change": False,
                },
                "candidates": list(manager.list_view(session_id, project_id=project_id)),
            }
        )

    async def trial(
        self,
        kwargs: Mapping[str, object],
        context: tuple[Any, str, str, str, str],
    ) -> str:
        if (authorization_error := self._authorize(context)) is not None:
            return authorization_error
        runner = self._ensure_runner()
        if runner is None:
            return self._runner_failure()
        plugin_id, version = kwargs.get("plugin_id"), kwargs.get("version")
        if not isinstance(plugin_id, str) or not isinstance(version, str):
            return _error("CANDIDATE_REFERENCE_INVALID", "trial 需要 plugin_id 和 version")
        registration = self._registration(plugin_id, version, context)
        if registration is None or registration.record.source is not PluginSource.MODEL_CANDIDATE:
            return _error("PLUGIN_NOT_FOUND", "候选版本在当前会话不可见")
        try:
            source, manifest, rules, contract_hash = self._artifact(registration)
            issuer = self._issuer
            if issuer is None:
                return self._runner_failure()
            _request, session_id, project_id, task_id, _workspace = context
            trial_id = kwargs.get("trial_id")
            trial_ref = (
                trial_id
                if isinstance(trial_id, str) and trial_id
                else f"trial-{uuid.uuid4().hex}"
            )
            grant = issuer.issue(
                session_id=session_id,
                task_id=task_id,
                project_id=project_id,
                allowed_input_rules=rules,
                expires_at_epoch=int(time.time()) + 180,
            )
            request = CandidateTrialRequest(
                task_id=task_id,
                project_id=project_id,
                trial_id=trial_ref,
                session_id=session_id,
                grant=grant,
                manifest=manifest,
                source=source,
                input_data=_fixed_trial_input(rules),
                timeout_seconds=CANDIDATE_TIMEOUT_SECONDS,
            )
            result = await asyncio.to_thread(runner.trial, request)
            payload = result.to_dict()
            if result.status == "passed" and result.source_hash and result.input_hash:
                binding = self._require_manager().mark_candidate_tested(
                    plugin_id,
                    version,
                    session_id=session_id,
                    project_id=project_id,
                    trial_id=trial_ref,
                    content_hash=result.source_hash,
                    input_hash=result.input_hash,
                    contract_hash=contract_hash,
                )
                payload["candidate_status"] = "tested" if binding.ok else "defined"
                payload["trial_binding"] = binding.to_dict()
                payload["trial_input_source"] = "host_fixed_fixture"
            elif result.status == "rejected":
                rejection = self._require_manager().mark_candidate_rejected(
                    plugin_id,
                    version,
                    session_id=session_id,
                    project_id=project_id,
                )
                payload["candidate_status"] = (
                    "rejected" if rejection.ok else "defined"
                )
                payload["rejection"] = rejection.to_dict()
                payload["trial_input_source"] = "host_fixed_fixture"
            elif result.status != "passed":
                payload["candidate_status"] = "defined"
                payload["trial_input_source"] = "host_fixed_fixture"
            return (
                _result(payload)
                if result.status == "passed"
                else _json({"ok": False, "result": payload})
            )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return _error("CANDIDATE_TRIAL_PREPARATION_FAILED", "候选试用请求无法准备")

    async def activate(
        self,
        kwargs: Mapping[str, object],
        context: tuple[Any, str, str, str, str],
    ) -> str:
        if (authorization_error := self._authorize(context)) is not None:
            return authorization_error
        if self._ensure_runner() is None:
            return self._runner_failure()
        plugin_id, version = kwargs.get("plugin_id"), kwargs.get("version")
        if not isinstance(plugin_id, str) or not isinstance(version, str):
            return _error("CANDIDATE_REFERENCE_INVALID", "activate 需要 plugin_id 和 version")
        _request, session_id, project_id, _task_id, _workspace = context
        task_id = context[3]
        manager = self._require_manager()
        registration = self._registration(plugin_id, version, context)
        if registration is None or registration.record.source is not PluginSource.MODEL_CANDIDATE:
            return _error("PLUGIN_NOT_FOUND", "候选版本在当前会话不可见")
        runner = self._ensure_runner()
        if runner is None:
            return self._runner_failure()
        budget_check = getattr(runner, "can_activate_live", None)
        if callable(budget_check):
            try:
                has_budget = budget_check(task_id)
            except Exception:
                has_budget = False
            if has_budget is not True:
                return _error(
                    "CANDIDATE_LIVE_BUDGET_EXCEEDED",
                    "候选已没有可用的任务级 live 执行预算，未激活路由",
                )
        try:
            _source, _manifest, _rules, contract_hash = self._artifact(registration)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return _error(
                "CANDIDATE_CONTRACT_CHANGED",
                "候选工件或执行契约已变化，必须重新定义并试用",
            )
        active, _scope_key = manager._find_active(plugin_id, session_id, project_id)
        if active is not None and active.registration.record.manifest.version != version:
            result = await manager.update_candidate(
                plugin_id,
                version,
                session_id=session_id,
                project_id=project_id,
                policy=_MODEL_CANDIDATE_POLICY,
                contract_hash=contract_hash,
            )
        else:
            result = manager.activate_candidate(
                plugin_id,
                version,
                session_id=session_id,
                project_id=project_id,
                policy=_MODEL_CANDIDATE_POLICY,
                contract_hash=contract_hash,
            )
        payload = result.to_dict()
        if result.ok:
            payload["next_step"] = (
                "候选代理已进入当前进程；下一轮对话会收到它的新工具 schema，"
                "无需重启 nanobot。"
            )
        return _result(payload) if result.ok else _json(payload)

    async def execute_active(
        self,
        registration: _RegisteredPlugin,
        rules: tuple[CandidateInputRule, ...],
        input_data: Mapping[str, object],
    ) -> str:
        context = self._context()
        if context is None:
            return _error("CANDIDATE_CONTEXT_INVALID", "候选调用缺少可信请求上下文")
        authorization_error = self._authorize(context)
        if authorization_error is not None:
            return authorization_error
        runner = self._ensure_runner()
        if runner is None:
            return self._runner_failure()
        try:
            source, manifest, artifact_rules, contract_hash = self._artifact(registration)
            manager = self._require_manager()
            if not manager.candidate_binding_matches(
                registration.record.manifest.plugin_id,
                registration.record.manifest.version,
                session_id=registration.record.session_id or "",
                project_id=registration.record.project_id,
                contract_hash=contract_hash,
            ):
                return _error(
                    "INVALID_CANDIDATE_STATE",
                    "候选版本的试用绑定已失效，必须重新试用后才能调用",
                )
            if artifact_rules != rules:
                return _error("CANDIDATE_CONTRACT_CHANGED", "候选输入契约已变化，必须重新试用")
            issuer = self._issuer
            if issuer is None:
                return self._runner_failure()
            _request, session_id, project_id, task_id, _workspace = context
            turn_id = _request.turn_id or uuid.uuid4().hex
            grant = issuer.issue(
                session_id=session_id,
                task_id=task_id,
                project_id=project_id,
                allowed_input_rules=rules,
                expires_at_epoch=int(time.time()) + 180,
            )
            request = CandidateTrialRequest(
                task_id=task_id,
                project_id=project_id,
                trial_id=f"call-{turn_id}-{uuid.uuid4().hex[:8]}",
                session_id=session_id,
                grant=grant,
                manifest=manifest,
                source=source,
                input_data=dict(input_data),
                timeout_seconds=CANDIDATE_TIMEOUT_SECONDS,
                execution_mode="live",
            )
            result = await asyncio.to_thread(runner.execute, request)
            return _json({"ok": result.status == "passed", "result": result.to_dict()})
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return _error("CANDIDATE_EXECUTION_PREPARATION_FAILED", "候选调用请求无法准备")

    async def manage(self, kwargs: Mapping[str, object]) -> str:
        context = self._context()
        if context is None:
            return _error("CANDIDATE_CONTEXT_INVALID", "模型候选动作需要可信请求上下文")
        if context[0].attributes.get("devpilot_model_candidates_enabled") is not True:
            return _error(
                "MODEL_CANDIDATES_PROFILE_DISABLED",
                "当前任务 Profile 未启用模型候选插件；不能仅靠聊天确认绕过配置",
            )
        action = kwargs.get("action")
        if action == "inspect":
            return self.inspect(context)
        if action == "define":
            if os.environ.get("DEVPILOT_ENABLE_MODEL_CANDIDATES") != "1":
                self._ensure_runner()
                return self._runner_failure()
            return self.define(kwargs, context)
        if action == "trial":
            return await self.trial(kwargs, context)
        if action == "activate":
            return await self.activate(kwargs, context)
        if action == "status":
            operation_id = kwargs.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                return _error("OPERATION_REFERENCE_INVALID", "status 需要 operation_id")
            status = self._require_manager().operation_status(
                operation_id,
                session_id=context[1],
                project_id=context[2],
            )
            return (
                _result(status)
                if status is not None
                else _error("OPERATION_NOT_FOUND", "该操作不属于当前会话或已不存在")
            )
        if action == "revoke":
            self._authorizations.pop((context[1], context[2]), None)
            self._require_manager().revoke_authorization(context[1])
            return _result({"status": "authorization_revoked"})
        if action in {"stop", "remove"}:
            if (authorization_error := self._authorize(context)) is not None:
                return authorization_error
            plugin_id, version = kwargs.get("plugin_id"), kwargs.get("version")
            if not isinstance(plugin_id, str) or not isinstance(version, str):
                return _error("CANDIDATE_REFERENCE_INVALID", f"{action} 需要 plugin_id 和 version")
            registration = self._registration(plugin_id, version, context)
            if (
                registration is None
                or registration.record.source is not PluginSource.MODEL_CANDIDATE
            ):
                return _error("PLUGIN_NOT_FOUND", "候选版本在当前会话不可见")
            _request, session_id, project_id, _task_id, _workspace = context
            if action == "stop":
                active, _scope_key = self._require_manager()._find_active(
                    plugin_id,
                    session_id,
                    project_id,
                )
                if active is None:
                    return _error("PLUGIN_NOT_ACTIVE", "候选版本当前未激活")
                if active.registration.record.manifest.version != version:
                    return _error(
                        "INVALID_CANDIDATE_STATE",
                        "stop 的 version 必须与当前激活版本完全一致",
                    )
                result = self._require_manager().stop(
                    plugin_id,
                    session_id=session_id,
                    project_id=project_id,
                    policy=_MODEL_CANDIDATE_POLICY,
                    operation_id=(
                        kwargs.get("operation_id")
                        if isinstance(kwargs.get("operation_id"), str)
                        else None
                    ),
                )
            else:
                result = self._require_manager().remove(
                    plugin_id,
                    version,
                    session_id=session_id,
                    project_id=project_id,
                    policy=_MODEL_CANDIDATE_POLICY,
                    operation_id=(
                        kwargs.get("operation_id")
                        if isinstance(kwargs.get("operation_id"), str)
                        else None
                    ),
                )
            return _result(result.to_dict()) if result.ok else _json(result.to_dict())
        return _error(
            "CANDIDATE_ACTION_INVALID",
            "action 必须是 inspect/define/trial/activate/stop/remove/status/revoke",
        )


class ModelCandidateProxyTool(Tool):
    """Dynamic Tool proxy; only its JSON input crosses into the FC runner."""

    _plugin_discoverable = False

    def __init__(
        self,
        controller: ModelCandidateController,
        registration: _RegisteredPlugin,
        rules: tuple[CandidateInputRule, ...],
    ) -> None:
        self._controller = controller
        self._registration = registration
        self._rules = rules

    @property
    def name(self) -> str:
        return self._registration.record.manifest.plugin_id

    @property
    def description(self) -> str:
        return (
            "A live model-generated pure-computation tool executed only through the FC "
            "sandbox proxy."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _rules_schema(self._rules)

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return await self._controller.execute_active(self._registration, self._rules, kwargs)


def _input_rule_variant(kind: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "field": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "Safe payload field name; do not use secrets or credentials.",
        },
        "kind": {"type": "string", "enum": [kind]},
        "max_length": {"type": "integer", "minimum": 1, "maximum": 65536},
    }
    if kind in {"integer", "number"}:
        bound_type = "integer" if kind == "integer" else "number"
        properties.update(
            {
                "min_value": {
                    "type": bound_type,
                    "description": "Optional inclusive lower bound.",
                },
                "max_value": {
                    "type": bound_type,
                    "description": "Optional inclusive upper bound.",
                },
            }
        )
    return {
        "type": "object",
        "properties": properties,
        "required": ["field", "kind", "max_length"],
        "additionalProperties": False,
    }


def _input_rule_item_schema() -> dict[str, Any]:
    common_properties = {
        "field": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "Safe payload field name; do not use secrets or credentials.",
        },
        "kind": {"type": "string", "enum": list(_INPUT_RULE_KINDS)},
        "max_length": {"type": "integer", "minimum": 1, "maximum": 65536},
        "min_value": {"type": "number"},
        "max_value": {"type": "number"},
    }
    return {
        "type": "object",
        "properties": common_properties,
        "required": ["field", "kind", "max_length"],
        "additionalProperties": False,
        "oneOf": [_input_rule_variant(kind) for kind in _INPUT_RULE_KINDS],
    }


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "inspect",
                    "define",
                    "trial",
                    "activate",
                    "stop",
                    "remove",
                    "status",
                    "revoke",
                ],
                "description": (
                    "Inspect or manage one model candidate; activation never imports "
                    "candidate code."
                ),
            },
            "plugin_id": {"type": "string", "minLength": 1},
            "version": {"type": "string", "minLength": 1},
            "source": {"type": "string", "maxLength": 65536},
            "input_rules": {
                "type": "array",
                "description": (
                    "Optional candidate input contract. Each item requires field, kind "
                    "and max_length; integer kinds use integer bounds, while number "
                    "kinds may use decimal bounds. Example: "
                    '[{"field":"name","kind":"string","max_length":64}].'
                ),
                "minItems": 1,
                "maxItems": 12,
                "items": _input_rule_item_schema(),
            },
            "input_data": {"type": "object", "additionalProperties": True},
            "trial_id": {"type": "string", "minLength": 1},
            "operation_id": {"type": "string", "minLength": 1},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
)
class ModelPluginManagementTool(Tool):
    """Expose the eight H6 model-candidate actions through nanobot chat."""

    _scopes = {"core"}

    def __init__(self, controller: ModelCandidateController) -> None:
        self._controller = controller

    @property
    def name(self) -> str:
        return "devpilot_model_plugin"

    @property
    def description(self) -> str:
        return (
            "Manage model-generated candidate tools live: inspect, define, trial, "
            "activate, stop or remove. Candidate source is never imported by "
            "nanobot; trial and calls use the configured FC proxy. For define, "
            "input_rules items must use field/kind/max_length; integer bounds must be "
            "integers and number bounds may be decimals, for example {field: name, "
            "kind: string, max_length: 64}."
        )

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if errors or "input_rules" not in params:
            return errors
        try:
            _parse_rules(params["input_rules"])
        except ValueError as exc:
            errors.append(str(exc))
        return errors

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        try:
            return await self._controller.manage(kwargs)
        except (KeyError, TypeError, ValueError, OSError):
            return ToolResult.error(
                "MODEL_CANDIDATE_ACTION_INVALID: candidate action was rejected safely"
            )
