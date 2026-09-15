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
from collections.abc import Callable, Mapping, Sequence
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
from nanobot.security.workspace_access import current_workspace_scope

from .candidate_autonomy import CandidateAutonomyPolicy
from .candidate_runner import (
    CANDIDATE_API_VERSION,
    CANDIDATE_COMMAND_PROFILE,
    CANDIDATE_MAX_EXECUTION_SECONDS,
    CANDIDATE_MAX_EXECUTIONS,
    CANDIDATE_NETWORK_POLICY,
    CANDIDATE_TEMPLATE,
    CANDIDATE_TIMEOUT_SECONDS,
    CandidateExecutionResult,
    CandidateInputRule,
    CandidateTrialGrantIssuer,
    CandidateTrialRequest,
    CandidateTrialRunner,
    _validate_source,
)
from .fc_sandbox import build_fc_backend_from_environment
from .harness_oracle import HarnessOracleContractError
from .redaction import is_sensitive_field_name, redact_sensitive_payload
from .remote_loop_strategy import RemoteStrategyArtifact, RemoteStrategyCatalog
from .storage import ProjectStore

if TYPE_CHECKING:
    from .harness_selection import HarnessSelectionStore


HarnessCaseOracle = Callable[
    [str, str, int, int, Mapping[str, object], CandidateExecutionResult],
    Mapping[str, object],
]


def _target_case_oracle(oracle: HarnessCaseOracle, target_id: str) -> HarnessCaseOracle:
    """Resolve a host registry while retaining the existing injected test seam."""
    resolver = getattr(oracle, "for_target", None)
    if callable(resolver):
        return cast(HarnessCaseOracle, resolver(target_id))
    return oracle


def _fixed_harness_input(
    oracle: HarnessCaseOracle,
    fixture: Mapping[str, object],
    rules: tuple[CandidateInputRule, ...],
) -> dict[str, object]:
    fixed_input = getattr(oracle, "fixed_input", None)
    if callable(fixed_input):
        return dict(fixed_input(fixture, rules))
    return _fixed_trial_input(rules)


def _fixture_contract_hash(oracle: HarnessCaseOracle, fixture: Mapping[str, object]) -> str | None:
    contract_hash = getattr(oracle, "fixture_contract_hash", None)
    return str(contract_hash(fixture)) if callable(contract_hash) else None


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
    allowed_kinds=frozenset({"tool", "loop"}),
    allowed_capabilities=frozenset(),
    allowed_actions=frozenset({"activate", "stop", "remove", "switch-loop"}),
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


def _candidate_input_class(input_data: Mapping[str, object]) -> str:
    """Classify Host-visible live input before it can be sent to FC."""
    if any(is_sensitive_field_name(key) for key in input_data):
        return "sensitive"
    if redact_sensitive_payload(dict(input_data)) != dict(input_data):
        return "sensitive"
    return "task"


def _live_budget_available(runner: CandidateTrialRunner | None, task_id: str) -> bool:
    """Treat unavailable budget state as a hard stop for a real candidate call."""
    if runner is None:
        return False
    budget_check = getattr(runner, "can_activate_live", None)
    if not callable(budget_check):
        return False
    try:
        return budget_check(task_id) is True
    except Exception:
        return False


class ModelCandidateController:
    """Host-owned H6 catalog adapter and FC-proxy factory."""

    def __init__(
        self,
        workspace: Path,
        runner: CandidateTrialRunner | None = None,
        grant_issuer: CandidateTrialGrantIssuer | None = None,
        harness_selection_store: HarnessSelectionStore | None = None,
        harness_case_oracle: HarnessCaseOracle | None = None,
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
        self._autonomy_tasks: set[tuple[str, str]] = set()
        self._revoked_autonomy: set[tuple[str, str]] = set()
        self._autonomy_policy = CandidateAutonomyPolicy()
        self._remote_strategy_catalog: RemoteStrategyCatalog | None = None
        self._harness_selection_store = harness_selection_store
        self._harness_case_oracle = harness_case_oracle

    def bind_plugin_manager(self, manager: PluginManager) -> None:
        self._manager = manager

    def bind_harness_selection(
        self,
        store: HarnessSelectionStore,
        *,
        case_oracle: HarnessCaseOracle | None = None,
    ) -> None:
        """Bind the durable H9 gate and a Host-owned fixed-case oracle."""
        self._harness_selection_store = store
        self._harness_case_oracle = case_oracle

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

    def _remote_catalog(self) -> RemoteStrategyCatalog:
        """Return the H8 catalog backed by the same FC-only H5 executor."""
        runner = self._ensure_runner()
        issuer = self._issuer
        if runner is None or issuer is None:
            raise ValueError("remote loop strategy runner is unavailable")
        if self._remote_strategy_catalog is None:
            self._remote_strategy_catalog = RemoteStrategyCatalog(
                self.data_dir,
                runner=runner,
                grant_issuer=issuer,
            )
        return self._remote_strategy_catalog

    def define_remote_strategy(
        self, session_id: str, strategy_id: str, version: str, source: str
    ) -> RemoteStrategyArtifact:
        """Define an immutable H8 decision artifact without importing it locally."""
        return self._remote_catalog().define(session_id, strategy_id, version, source)

    def trial_remote_strategy(
        self, artifact: RemoteStrategyArtifact, *, task_id: str, project_id: str
    ) -> None:
        """Require the same FC trial path before a remote Driver can be selected."""
        result = self._remote_catalog().trial(artifact, task_id=task_id, project_id=project_id)
        if result.status != "passed":
            raise ValueError("remote loop strategy trial failed")

    async def define_loop_candidate(
        self,
        kwargs: Mapping[str, object],
        context: tuple[Any, str, str, str, str],
    ) -> str:
        """Persist a remote Loop candidate without executing its source locally."""
        if os.environ.get("DEVPILOT_ENABLE_MODEL_CANDIDATES") != "1":
            return self._runner_failure()
        strategy_id = kwargs.get("strategy_id")
        version = kwargs.get("version")
        source = kwargs.get("source")
        if not isinstance(strategy_id, str) or not isinstance(version, str) or not isinstance(
            source, str
        ):
            return _error(
                "LOOP_CANDIDATE_REFERENCE_INVALID",
                "define_loop 需要 strategy_id、version 和 source",
            )
        try:
            _request, session_id, _project_id, _task_id, _workspace = context
            artifact = await asyncio.to_thread(
                self.define_remote_strategy, session_id, strategy_id, version, source
            )
            return _result(
                {
                    "strategy_id": artifact.strategy_id,
                    "version": artifact.version,
                    "source_hash": artifact.source_hash,
                    "execution": "fc_proxy",
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _error("LOOP_CANDIDATE_DEFINITION_FAILED", "Loop 候选工件无法安全保存")

    async def trial_loop_candidate(
        self,
        kwargs: Mapping[str, object],
        context: tuple[Any, str, str, str, str],
    ) -> str:
        """Run one Loop candidate trial through the existing FC proxy."""
        if (authorization_error := self._authorize(context)) is not None:
            return authorization_error
        strategy_id = kwargs.get("strategy_id")
        version = kwargs.get("version")
        source_hash = kwargs.get("source_hash")
        if not all(
            isinstance(value, str) and value
            for value in (strategy_id, version, source_hash)
        ):
            return _error(
                "LOOP_CANDIDATE_REFERENCE_INVALID",
                "trial_loop 需要 strategy_id、version 和 source_hash",
            )
        try:
            _request, session_id, project_id, task_id, _workspace = context
            artifact = self._remote_catalog().load(
                session_id, strategy_id, version, source_hash
            )
            result = await asyncio.to_thread(
                self._remote_catalog().trial,
                artifact,
                task_id=task_id,
                project_id=project_id,
            )
            return _result(result.to_dict()) if result.status == "passed" else _json(
                {"ok": False, "result": result.to_dict()}
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _error("LOOP_CANDIDATE_TRIAL_FAILED", "Loop 候选试用未通过")

    async def evaluate_loop(
        self,
        kwargs: Mapping[str, object],
        context: tuple[Any, str, str, str, str],
    ) -> str:
        """Compare one trialled remote Loop candidate with its fixed baseline."""
        if (authorization_error := self._authorize(context)) is not None:
            return authorization_error
        store = self._harness_selection_store
        oracle = self._harness_case_oracle
        if store is None or oracle is None:
            return _error(
                "HARNESS_EVALUATOR_UNAVAILABLE",
                "H9 需要已配置的 Host 固定用例判定器；未执行 Loop 评测",
            )
        strategy_id = kwargs.get("strategy_id")
        if not isinstance(strategy_id, str) or not strategy_id:
            return _error("HARNESS_CANDIDATE_REFERENCE_INVALID", "evaluate_loop 需要 strategy_id")
        try:
            target_oracle = _target_case_oracle(oracle, strategy_id)
        except HarnessOracleContractError:
            return _error(
                "HARNESS_EVALUATOR_UNAVAILABLE",
                "当前 Loop 没有 Host 固定用例契约；未执行评测",
            )
        runner = self._ensure_runner()
        if runner is None:
            return self._runner_failure()
        candidate_version = kwargs.get("version")
        baseline_version = kwargs.get("baseline_version")
        candidate_hash = kwargs.get("source_hash")
        baseline_hash = kwargs.get("baseline_source_hash")
        if not all(
            isinstance(value, str) and value
            for value in (
                strategy_id,
                candidate_version,
                baseline_version,
                candidate_hash,
                baseline_hash,
            )
        ):
            return _error(
                "HARNESS_CANDIDATE_REFERENCE_INVALID",
                "evaluate_loop 需要 strategy_id、baseline_version、version 和两个 source_hash",
            )
        budget = {
            key: kwargs.get(key)
            for key in ("max_candidates", "max_evaluations", "max_tokens")
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in budget.values()
        ):
            return _error(
                "HARNESS_BUDGET_INVALID",
                "H9 评测必须在开始前设置 max_candidates、max_evaluations、max_tokens",
            )
        _request, session_id, project_id, task_id, workspace = context
        raw_refs = context[0].attributes.get("devpilot_harness_evidence_refs", [])
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes, bytearray)):
            return _error("HARNESS_EVIDENCE_MISSING", "当前任务没有可引用的 H9 反馈证据")
        evidence_refs = tuple(item for item in raw_refs if isinstance(item, str) and item)
        if not evidence_refs:
            return _error("HARNESS_EVIDENCE_MISSING", "当前任务没有可引用的 H9 反馈证据")
        try:
            catalog = self._remote_catalog()
            candidate_artifact = catalog.load(
                session_id, strategy_id, candidate_version, candidate_hash
            )
            baseline_artifact = catalog.load(
                session_id, strategy_id, baseline_version, baseline_hash
            )
            catalog.require_trialled(candidate_artifact)
            catalog.require_trialled(baseline_artifact)
            round_id = kwargs.get("round_id")
            if not isinstance(round_id, str) or not round_id:
                round_id = f"h9-{task_id}"
            candidate = store.create_candidate(
                kind="loop",
                target_id=strategy_id,
                baseline_version=baseline_version,
                candidate_version=candidate_version,
                baseline_hash=baseline_hash,
                candidate_hash=candidate_hash,
                scope={"project_id": project_id, "workspace": workspace},
                evidence_refs=evidence_refs,
                round_id=round_id,
                budget=budget,
            )
            def host_trial(
                category: str,
                version: str,
                scenario: int,
                repeat: int,
                fixture: Mapping[str, object],
            ) -> Mapping[str, object]:
                artifact = (
                    baseline_artifact if version == "baseline" else candidate_artifact
                )
                request = catalog.evaluation_request(
                    artifact,
                    session_id=session_id,
                    task_id=task_id,
                    project_id=project_id,
                    projection=catalog.fixed_evaluation_projection(fixture),
                    trial_id=f"remote-eval-{uuid.uuid4().hex}",
                )
                result = runner.execute(request)
                oracle_result = dict(
                    target_oracle(category, version, scenario, repeat, fixture, result)
                )
                return {
                    **oracle_result,
                    "fixture_contract_hash": _fixture_contract_hash(target_oracle, fixture),
                    "contract_passed": oracle_result.get(
                        "contract_passed", result.status == "passed"
                    ),
                    "side_effect_count": oracle_result.get("side_effect_count", 0),
                    "permission_violation": oracle_result.get("permission_violation", False),
                    "usage": {
                        "calls": 1,
                        "input_tokens": oracle_result.get("input_tokens"),
                        "output_tokens": oracle_result.get("output_tokens"),
                    },
                }

            report = store.run_fixed_evaluation(candidate.candidate_id, host_trial)
            return _result(
                {
                    "candidate_id": candidate.candidate_id,
                    "evaluation_id": report.evaluation_id,
                    "status": report.status,
                    "eligible": report.eligible,
                    "total_runs": report.total_runs,
                    "baseline_successes": report.baseline_successes,
                    "candidate_successes": report.candidate_successes,
                    "result_hash": report.result_hash,
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError):
            return _error(
                "HARNESS_EVALUATION_FAILED",
                "H9 Loop 固定评测未完成；本轮已停止，未晋升候选",
            )

    def remote_strategy_artifact(
        self,
        session_id: str,
        strategy_id: str,
        version: str,
        source_hash: str,
        *,
        project_id: str | None = None,
    ) -> RemoteStrategyArtifact:
        """Resolve a passed strategy before it can enter a Host handoff record."""
        catalog = self._remote_catalog()
        artifact = (
            catalog.load_for_scope(session_id, project_id, strategy_id, version, source_hash)
            if project_id is not None
            else catalog.load(session_id, strategy_id, version, source_hash)
        )
        catalog.require_trialled(artifact)
        return artifact

    def promote_remote_strategy_to_project(
        self,
        session_id: str,
        strategy_id: str,
        version: str,
        source_hash: str,
        *,
        project_id: str,
    ) -> None:
        """Persist one confirmed remote Loop artifact for future project tasks."""
        catalog = self._remote_catalog()
        artifact = catalog.load(session_id, strategy_id, version, source_hash)
        catalog.require_trialled(artifact)
        catalog.promote_to_project(artifact, project_id=project_id)

    async def execute_remote_strategy(
        self,
        strategy_id: str,
        strategy_version: str,
        source_hash: str,
        projection: Mapping[str, object],
        *,
        project_id: str | None = None,
    ) -> Mapping[str, object]:
        """Execute a session-pinned H8 strategy through FC using only projection data."""
        context = self._context()
        if context is None:
            raise ValueError("remote loop strategy lacks trusted request context")
        _request, session_id, context_project_id, task_id, _workspace = context
        effective_project_id = project_id or context_project_id
        catalog = self._remote_catalog()
        artifact = catalog.load_for_scope(
            session_id, effective_project_id, strategy_id, strategy_version, source_hash
        )
        decision_request = catalog.decision_request(
            artifact,
            session_id=session_id,
            task_id=task_id,
            project_id=effective_project_id,
            projection=projection,
        )
        try:
            return await asyncio.to_thread(catalog.execute_decision, decision_request)
        except asyncio.CancelledError:
            # ``to_thread`` does not cancel synchronous FC work itself. Send
            # the exact durable candidate request to the runner so it can use
            # its persisted operation/run receipt and call FC cancel/cleanup.
            await asyncio.shield(
                asyncio.to_thread(catalog.cancel_decision, decision_request)
            )
            raise

    async def recover_remote_strategy(
        self,
        strategy_id: str,
        strategy_version: str,
        source_hash: str,
        trial_id: str,
    ) -> CandidateExecutionResult:
        """Query one durable remote decision after restart; never replay it."""
        context = self._context()
        if context is None:
            raise ValueError("remote loop strategy lacks trusted request context")
        _request, session_id, _project_id, _task_id, _workspace = context
        return await asyncio.to_thread(
            self._remote_catalog().recover_decision,
            session_id,
            strategy_id,
            strategy_version,
            source_hash,
            trial_id,
        )

    def _require_manager(self) -> PluginManager:
        if self._manager is None:
            raise RuntimeError("model candidate manager is not bound")
        return self._manager

    def _autonomy_summary(
        self,
        *,
        operation: str,
        input_class: str,
        budget_available: bool = True,
        candidate_trialled: bool = True,
    ) -> dict[str, str] | None:
        scope = current_workspace_scope()
        if scope is None or scope.source_channel != "websocket":
            return None
        decision = self._autonomy_policy.decide(
            access_mode=scope.access_mode,
            operation=operation,
            input_class=input_class,
            budget_available=budget_available,
            candidate_trialled=candidate_trialled,
        )
        return {
            "access_mode": scope.access_mode,
            "decision": decision,
            "input_class": input_class,
        }

    def _registration(
        self,
        plugin_id: str,
        version: str,
        context: tuple[Any, str, str, str, str],
    ) -> _RegisteredPlugin | None:
        _request, session_id, project_id, _task_id, _workspace = context
        return self._require_manager()._find_visible(plugin_id, version, session_id, project_id)

    def _authorize(
        self,
        context: tuple[Any, str, str, str, str],
        *,
        operation: str = "call",
        input_class: str = "task",
        candidate_trialled: bool = True,
        budget_available: bool = True,
        confirmation_binding: Mapping[str, str] | None = None,
        apply_autonomy: bool = False,
        candidate_id: str | None = None,
        candidate_version: str | None = None,
    ) -> str | None:
        request, session_id, project_id, _task_id, _workspace = context
        if request.attributes.get("devpilot_model_candidates_enabled") is not True:
            return _error(
                "MODEL_CANDIDATES_PROFILE_DISABLED",
                "当前任务 Profile 未启用模型候选插件；不能仅靠聊天确认绕过配置",
            )
        manager = self._require_manager()
        autonomy = self._autonomy_summary(
            operation=operation,
            input_class=input_class,
            budget_available=budget_available,
            candidate_trialled=candidate_trialled,
        )
        h11_confirmation = False
        if (
            apply_autonomy
            and autonomy is not None
            and (session_id, _task_id) in self._autonomy_tasks
        ):
            decision = autonomy["decision"]
            revoked_key = (session_id, _task_id)
            if revoked_key in self._revoked_autonomy and operation not in {"stop", "remove"}:
                decision = "deny"
                autonomy = {**autonomy, "decision": decision}
            if candidate_id is not None and candidate_version is not None:
                try:
                    manager.record_candidate_autonomy(
                        candidate_id,
                        candidate_version,
                        session_id=session_id,
                        task_id=_task_id,
                        project_id=project_id,
                        access_mode=autonomy["access_mode"],
                        decision=decision,
                        input_class=input_class,
                        budget_available=budget_available,
                        candidate_trialled=candidate_trialled,
                    )
                    self._autonomy_tasks.add((session_id, _task_id))
                except (OSError, RuntimeError, TypeError, ValueError):
                    return _error(
                        "CANDIDATE_AUTONOMY_AUDIT_UNAVAILABLE",
                        "候选权限决策无法持久记录；未派发到 FC",
                    )
            if decision == "deny":
                return _error(
                    "CANDIDATE_AUTONOMY_DENIED",
                    "当前候选操作未满足权限、数据、试用或预算约束；未派发到 FC",
                )
            if decision in {"auto", "notice"}:
                manager.authorize(session_id)
                return None
            h11_confirmation = decision == "confirm"
        key = (session_id, project_id)
        grant = self._authorizations.get(key)
        now = int(time.time())
        if (
            grant is not None
            and grant.get("sender_id") == request.sender_id
            and grant.get("task_id") == _task_id
            and isinstance(grant.get("expires_at"), int)
            and int(grant["expires_at"]) > now
            and (
                not h11_confirmation
                or grant.get("confirmation_binding") == dict(confirmation_binding or {})
            )
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
            if h11_confirmation:
                return _error(
                    "CANDIDATE_REAL_INPUT_CONFIRMATION_REQUIRED",
                    "已验证临时工具；是否允许它在本次任务处理当前允许的数据？"
                    "确认后，同一候选版本、输入规则和预算范围内无需重复确认。",
                    requires_user_confirmation=True,
                )
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
            "confirmation_binding": dict(confirmation_binding or {}),
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
        expected_manifest = manifest.to_dict()
        # H9 may carry an already-tested FC artifact into a project selection.
        # Scope is host routing metadata, not candidate code or its FC contract;
        # every other immutable manifest field must still match the sidecar.
        scope_only_projection = {
            **expected_manifest,
            "scope": PluginScope.SESSION.value,
        }
        if not isinstance(raw_manifest, dict) or _json(raw_manifest) not in {
            _json(expected_manifest),
            _json(scope_only_projection),
        }:
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
            autonomy = self._autonomy_summary(
                operation="define", input_class="fixture", candidate_trialled=False
            )
            if autonomy is not None:
                self._require_manager().record_candidate_autonomy(
                    plugin_id,
                    version,
                    session_id=session_id,
                    task_id=_task_id,
                    project_id=_project_id,
                    access_mode=autonomy["access_mode"],
                    decision=autonomy["decision"],
                    input_class="fixture",
                    budget_available=True,
                    candidate_trialled=False,
                )
                self._autonomy_tasks.add((session_id, _task_id))
            return _result(result.to_dict()) if result.ok else _json(result.to_dict())
        except (OSError, RuntimeError, ValueError, TypeError):
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
        plugin_id, version = kwargs.get("plugin_id"), kwargs.get("version")
        if not isinstance(plugin_id, str) or not isinstance(version, str):
            return _error("CANDIDATE_REFERENCE_INVALID", "trial 需要 plugin_id 和 version")
        if (
            authorization_error := self._authorize(
                context,
                operation="trial",
                input_class="fixture",
                candidate_trialled=False,
                apply_autonomy=True,
                candidate_id=plugin_id,
                candidate_version=version,
            )
        ) is not None:
            return authorization_error
        runner = self._ensure_runner()
        if runner is None:
            return self._runner_failure()
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
                autonomy=self._autonomy_summary(
                    operation="trial", input_class="fixture", candidate_trialled=False
                ),
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
        plugin_id, version = kwargs.get("plugin_id"), kwargs.get("version")
        if not isinstance(plugin_id, str) or not isinstance(version, str):
            return _error("CANDIDATE_REFERENCE_INVALID", "activate 需要 plugin_id 和 version")
        if self._ensure_runner() is None:
            return self._runner_failure()
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
        candidate_trialled = (
            registration.record.candidate_status is PluginCandidateStatus.TESTED
            and manager.candidate_binding_matches(
                plugin_id,
                version,
                session_id=session_id,
                project_id=project_id,
                contract_hash=contract_hash,
            )
        )
        if (
            authorization_error := self._authorize(
                context,
                operation="activate",
                candidate_trialled=candidate_trialled,
                apply_autonomy=True,
                candidate_id=plugin_id,
                candidate_version=version,
            )
        ) is not None:
            return authorization_error
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

    async def evaluate(
        self,
        kwargs: Mapping[str, object],
        context: tuple[Any, str, str, str, str],
    ) -> str:
        """Evaluate one defined plugin candidate through H9's fixed suite."""
        if (authorization_error := self._authorize(context)) is not None:
            return authorization_error
        store = self._harness_selection_store
        oracle = self._harness_case_oracle
        if store is None or oracle is None:
            return _error(
                "HARNESS_EVALUATOR_UNAVAILABLE",
                "H9 需要已配置的 Host 固定用例判定器；未执行候选评测",
            )
        plugin_id = kwargs.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id:
            return _error("HARNESS_CANDIDATE_REFERENCE_INVALID", "evaluate 需要 plugin_id")
        try:
            target_oracle = _target_case_oracle(oracle, plugin_id)
        except HarnessOracleContractError:
            return _error(
                "HARNESS_EVALUATOR_UNAVAILABLE",
                "当前插件没有 Host 固定用例契约；未执行评测",
            )
        runner = self._ensure_runner()
        issuer = self._issuer
        if runner is None or issuer is None:
            return self._runner_failure()
        candidate_version = kwargs.get("version")
        baseline_version = kwargs.get("baseline_version")
        if not all(
            isinstance(value, str) and value
            for value in (plugin_id, candidate_version, baseline_version)
        ):
            return _error(
                "HARNESS_CANDIDATE_REFERENCE_INVALID",
                "evaluate 需要 plugin_id、baseline_version 和 version",
            )
        budget = {
            key: kwargs.get(key)
            for key in ("max_candidates", "max_evaluations", "max_tokens")
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in budget.values()
        ):
            return _error(
                "HARNESS_BUDGET_INVALID",
                "H9 评测必须在开始前设置 max_candidates、max_evaluations、max_tokens",
            )
        _request, session_id, project_id, task_id, workspace = context
        manager = self._require_manager()
        candidate_registration = self._registration(plugin_id, candidate_version, context)
        baseline_registration = self._registration(plugin_id, baseline_version, context)
        if candidate_registration is None or baseline_registration is None:
            return _error("PLUGIN_NOT_FOUND", "基线或候选版本在当前会话不可见")
        if (
            candidate_registration.record.source is not PluginSource.MODEL_CANDIDATE
            or baseline_registration.record.source is not PluginSource.MODEL_CANDIDATE
        ):
            return _error(
                "HARNESS_BASELINE_INVALID",
                "H9 仅允许比较同一 FC 代理模型候选族的基线和候选版本",
            )
        if candidate_registration.record.candidate_status is not PluginCandidateStatus.DEFINED:
            return _error(
                "HARNESS_CANDIDATE_STATE_INVALID",
                "候选必须处于 defined 状态才能开始 H9 评测",
            )
        try:
            candidate_source, candidate_manifest, candidate_rules, candidate_contract = (
                self._artifact(candidate_registration)
            )
            baseline_source, baseline_manifest, baseline_rules, _baseline_contract = (
                self._artifact(baseline_registration)
            )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return _error(
                "HARNESS_ARTIFACT_INVALID",
                "基线或候选工件发生变化，未启动 H9 评测",
            )
        if candidate_rules != baseline_rules:
            return _error(
                "HARNESS_INPUT_CONTRACT_CHANGED",
                "基线与候选输入契约不同，不能进行同输入对照",
            )
        raw_refs = context[0].attributes.get("devpilot_harness_evidence_refs", [])
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes, bytearray)):
            return _error("HARNESS_EVIDENCE_MISSING", "当前任务没有可引用的 H9 反馈证据")
        evidence_refs = tuple(item for item in raw_refs if isinstance(item, str) and item)
        if not evidence_refs:
            return _error("HARNESS_EVIDENCE_MISSING", "当前任务没有可引用的 H9 反馈证据")
        round_id = kwargs.get("round_id")
        if not isinstance(round_id, str) or not round_id:
            round_id = f"h9-{task_id}"
        try:
            candidate = store.create_candidate(
                kind="plugin",
                target_id=plugin_id,
                baseline_version=baseline_version,
                candidate_version=candidate_version,
                baseline_hash=baseline_manifest["content_hash"],
                candidate_hash=candidate_manifest["content_hash"],
                scope={"project_id": project_id, "workspace": workspace},
                evidence_refs=evidence_refs,
                round_id=round_id,
                budget=budget,
            )

            def host_trial(
                category: str,
                version: str,
                scenario: int,
                repeat: int,
                fixture: Mapping[str, object],
            ) -> Mapping[str, object]:
                source, manifest = (
                    (baseline_source, baseline_manifest)
                    if version == "baseline"
                    else (candidate_source, candidate_manifest)
                )
                input_data = _fixed_harness_input(target_oracle, fixture, candidate_rules)
                if any(rule[0] == "case" and rule[1] == "string" for rule in candidate_rules):
                    input_data["case"] = fixture["case"]
                grant = issuer.issue(
                    session_id=session_id,
                    task_id=task_id,
                    project_id=project_id,
                    allowed_input_rules=candidate_rules,
                    expires_at_epoch=int(time.time()) + 180,
                )
                request = CandidateTrialRequest(
                    task_id=task_id,
                    project_id=project_id,
                    trial_id=(
                        f"h9-{category}-{scenario}-{version}-{repeat}-"
                        f"{uuid.uuid4().hex[:8]}"
                    ),
                    session_id=session_id,
                    grant=grant,
                    manifest=manifest,
                    source=source,
                    input_data=input_data,
                    timeout_seconds=CANDIDATE_TIMEOUT_SECONDS,
                    execution_mode="live",
                )
                result = runner.execute(request)
                oracle_result = dict(
                    target_oracle(category, version, scenario, repeat, fixture, result)
                )
                return {
                    **oracle_result,
                    "fixture_contract_hash": _fixture_contract_hash(target_oracle, fixture),
                    "contract_passed": oracle_result.get(
                        "contract_passed", result.status == "passed"
                    ),
                    "side_effect_count": oracle_result.get("side_effect_count", 0),
                    "permission_violation": oracle_result.get("permission_violation", False),
                    "usage": {
                        "calls": 1,
                        "input_tokens": oracle_result.get("input_tokens"),
                        "output_tokens": oracle_result.get("output_tokens"),
                    },
                }

            report = store.run_fixed_evaluation(candidate.candidate_id, host_trial)
            binding = None
            if report.eligible:
                binding = manager.mark_candidate_tested(
                    plugin_id,
                    candidate_version,
                    session_id=session_id,
                    project_id=project_id,
                    trial_id=report.evaluation_id,
                    content_hash=candidate_manifest["content_hash"],
                    input_hash=hashlib.sha256(
                        _json(_fixed_trial_input(candidate_rules)).encode("utf-8")
                    ).hexdigest(),
                    contract_hash=candidate_contract,
                )
                if not binding.ok:
                    store.retire_candidate(candidate.candidate_id)
                    return _error(
                        "HARNESS_BINDING_FAILED",
                        "H9 评测通过但候选试用绑定失败；候选已停用",
                    )
            return _result(
                {
                    "candidate_id": candidate.candidate_id,
                    "evaluation_id": report.evaluation_id,
                    "status": report.status,
                    "eligible": report.eligible,
                    "total_runs": report.total_runs,
                    "baseline_successes": report.baseline_successes,
                    "candidate_successes": report.candidate_successes,
                    "result_hash": report.result_hash,
                    "candidate_status": "tested" if binding is not None else "defined",
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError):
            return _error(
                "HARNESS_EVALUATION_FAILED",
                "H9 固定评测未完成；本轮已停止，未晋升候选",
            )

    async def execute_active(
        self,
        registration: _RegisteredPlugin,
        rules: tuple[CandidateInputRule, ...],
        input_data: Mapping[str, object],
    ) -> str:
        context = self._context()
        if context is None:
            return _error("CANDIDATE_CONTEXT_INVALID", "候选调用缺少可信请求上下文")
        runner = self._ensure_runner()
        if runner is None:
            return self._runner_failure()
        try:
            source, manifest, artifact_rules, contract_hash = self._artifact(registration)
            manager = self._require_manager()
            active, _scope_key = manager._find_active(
                registration.record.manifest.plugin_id,
                context[1],
                context[2],
            )
            if (
                active is None
                or active.registration.registration_key != registration.registration_key
            ):
                return _error(
                    "INVALID_CANDIDATE_STATE",
                    "候选版本当前未激活或已被替换，不能从旧路由重放调用",
                )
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
            input_class = _candidate_input_class(input_data)
            budget_available = _live_budget_available(runner, context[3])
            autonomy = self._autonomy_summary(
                operation="call",
                input_class=input_class,
                budget_available=budget_available,
            )
            authorization_error = self._authorize(
                context,
                operation="call",
                input_class=input_class,
                budget_available=budget_available,
                confirmation_binding={
                    "plugin_id": registration.record.manifest.plugin_id,
                    "version": registration.record.manifest.version,
                    "content_hash": registration.record.manifest.content_hash,
                    "contract_hash": contract_hash,
                    "input_rules_hash": hashlib.sha256(
                        _json(_rules_view(rules)).encode("utf-8")
                    ).hexdigest(),
                    "max_calls": str(CANDIDATE_MAX_EXECUTIONS),
                    "max_seconds": str(CANDIDATE_MAX_EXECUTION_SECONDS),
                },
                apply_autonomy=True,
                candidate_id=registration.record.manifest.plugin_id,
                candidate_version=registration.record.manifest.version,
            )
            if authorization_error is not None:
                return authorization_error
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
                autonomy=autonomy,
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
        if action == "evaluate":
            return await self.evaluate(kwargs, context)
        if action == "define_loop":
            return await self.define_loop_candidate(kwargs, context)
        if action == "trial_loop":
            return await self.trial_loop_candidate(kwargs, context)
        if action == "evaluate_loop":
            return await self.evaluate_loop(kwargs, context)
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
            _request, session_id, project_id, task_id, _workspace = context
            manager = self._require_manager()
            stopped: list[dict[str, Any]] = []
            stop_autonomy = self._autonomy_summary(operation="stop", input_class="task")
            for item in manager.list_view(session_id, project_id=project_id):
                plugin_id = item.get("plugin_id")
                version = item.get("version")
                if item.get("source") != PluginSource.MODEL_CANDIDATE.value:
                    continue
                if not isinstance(plugin_id, str) or not isinstance(version, str):
                    continue
                active, _scope_key = manager._find_active(plugin_id, session_id, project_id)
                if active is None or active.registration.record.manifest.version != version:
                    continue
                result = manager.stop(
                    plugin_id,
                    session_id=session_id,
                    project_id=project_id,
                    policy=_MODEL_CANDIDATE_POLICY,
                    operation_details=(
                        {
                            "autonomy_access_mode": stop_autonomy["access_mode"],
                            "autonomy_decision": stop_autonomy["decision"],
                            "autonomy_input_class": "task",
                            "autonomy_budget": "available",
                            "autonomy_candidate_trialled": "yes",
                        }
                        if stop_autonomy is not None
                        else None
                    ),
                )
                if not result.ok:
                    return _json(result.to_dict())
                stopped.append(result.to_dict())
            self._revoked_autonomy.add((session_id, task_id))
            self._authorizations.pop((session_id, project_id), None)
            manager.revoke_authorization(session_id)
            return _result({"status": "authorization_revoked", "stopped": stopped})
        if action in {"stop", "remove"}:
            plugin_id, version = kwargs.get("plugin_id"), kwargs.get("version")
            if not isinstance(plugin_id, str) or not isinstance(version, str):
                return _error("CANDIDATE_REFERENCE_INVALID", f"{action} 需要 plugin_id 和 version")
            if (
                authorization_error := self._authorize(
                    context,
                    operation=action,
                    apply_autonomy=True,
                    candidate_id=plugin_id,
                    candidate_version=version,
                )
            ) is not None:
                return authorization_error
            registration = self._registration(plugin_id, version, context)
            if (
                registration is None
                or registration.record.source is not PluginSource.MODEL_CANDIDATE
            ):
                return _error("PLUGIN_NOT_FOUND", "候选版本在当前会话不可见")
            _request, session_id, project_id, _task_id, _workspace = context
            if action == "stop":
                stop_autonomy = self._autonomy_summary(operation=action, input_class="task")
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
                    operation_details=(
                        {
                            "autonomy_access_mode": stop_autonomy["access_mode"],
                            "autonomy_decision": stop_autonomy["decision"],
                            "autonomy_input_class": "task",
                            "autonomy_budget": "available",
                            "autonomy_candidate_trialled": "yes",
                        }
                        if stop_autonomy is not None
                        else None
                    ),
                    operation_id=(
                        kwargs.get("operation_id")
                        if isinstance(kwargs.get("operation_id"), str)
                        else None
                    ),
                )
            else:
                remove_autonomy = self._autonomy_summary(operation=action, input_class="task")
                result = self._require_manager().remove(
                    plugin_id,
                    version,
                    session_id=session_id,
                    project_id=project_id,
                    policy=_MODEL_CANDIDATE_POLICY,
                    operation_details=(
                        {
                            "autonomy_access_mode": remove_autonomy["access_mode"],
                            "autonomy_decision": remove_autonomy["decision"],
                            "autonomy_input_class": "task",
                            "autonomy_budget": "available",
                            "autonomy_candidate_trialled": "yes",
                        }
                        if remove_autonomy is not None
                        else None
                    ),
                    operation_id=(
                        kwargs.get("operation_id")
                        if isinstance(kwargs.get("operation_id"), str)
                        else None
                    ),
                )
            return _result(result.to_dict()) if result.ok else _json(result.to_dict())
        return _error(
            "CANDIDATE_ACTION_INVALID",
            "action 必须是 inspect/define/trial/evaluate/define_loop/trial_loop/"
            "evaluate_loop/activate/stop/remove/status/revoke",
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
                    "evaluate",
                    "define_loop",
                    "trial_loop",
                    "evaluate_loop",
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
            "strategy_id": {"type": "string", "minLength": 1},
            "version": {"type": "string", "minLength": 1},
            "baseline_version": {"type": "string", "minLength": 1},
            "source_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "baseline_source_hash": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "round_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "max_candidates": {"type": "integer", "minimum": 1, "maximum": 2},
            "max_evaluations": {"type": "integer", "minimum": 1, "maximum": 48},
            "max_tokens": {"type": "integer", "minimum": 1},
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
    """Expose H6 lifecycle and the H9 fixed-evaluation action through chat."""

    _scopes = {"core"}

    def __init__(self, controller: ModelCandidateController) -> None:
        self._controller = controller

    @property
    def name(self) -> str:
        return "devpilot_model_plugin"

    @property
    def description(self) -> str:
        return (
            "Manage model-generated candidate tools and FC-only Loop candidates live: "
            "inspect, define, trial, evaluate, define_loop, trial_loop, evaluate_loop, "
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
