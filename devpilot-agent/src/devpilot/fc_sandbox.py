"""Alibaba FC Agent Sandbox adapter through the E2B-compatible Python SDK.

The dependency is optional and imported lazily.  This keeps the local P2B
approval service usable without cloud credentials or network access.  The
adapter is an execution port only; it is not registered as a nanobot Tool and
does not mutate the local project directory.
"""

from __future__ import annotations

import importlib
import os
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any

from .execution import (
    MAX_EXECUTION_OUTPUT_BYTES,
    ExecutionBackend,
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
    execution_bundle_bytes,
    validate_request,
)

SANDBOX_ROOT = "/workspace/devpilot"
MAX_CLEANUP_ATTEMPTS = 3
FC_VERIFICATION_PROFILES = frozenset(
    {"python-syntax", "python-regression", "python-run", "node-syntax"}
)


def _provider_error(stage: str, exc: Exception) -> ExecutionError:
    """Map provider failures to stable, secret-free adapter error codes."""

    detail = str(exc)
    normalized = detail.casefold()
    if "sessionnetworkpolicy" in normalized:
        return ExecutionError(
            "FC_CAPABILITY_UNSUPPORTED",
            "FC endpoint does not support the required Sandbox network policy",
        )
    if any(marker in normalized for marker in ("401", "403", "unauthorized", "forbidden")):
        return ExecutionError("FC_AUTH_FAILED", "FC Sandbox authentication was rejected")
    if any(marker in normalized for marker in ("timeout", "timed out", "deadline exceeded")):
        return ExecutionError("FC_TIMEOUT", f"FC Sandbox {stage} timed out")
    codes = {
        "create": "FC_SANDBOX_CREATE_FAILED",
        "upload": "FC_UPLOAD_FAILED",
        "execute": "FC_EXECUTION_FAILED",
        "cleanup": "FC_CLEANUP_FAILED",
    }
    return ExecutionError(codes[stage], f"FC Sandbox {stage} failed")


@dataclass(frozen=True, slots=True)
class FCSandboxConfig:
    """Connection settings loaded from environment, never source-controlled."""

    api_key: str
    api_url: str
    domain: str
    template: str = "code-interpreter-v1"
    allow_network: bool = False
    max_timeout_seconds: int = 900
    region: str = ""
    network_policy_mode: str = "required"
    allowed_project_paths: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> FCSandboxConfig:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ExecutionError("FC_CONFIG_MISSING", f"missing {name}")
            return value

        try:
            max_timeout_seconds = int(os.environ.get("DEVPILOT_FC_MAX_TIMEOUT_SECONDS", "900"))
        except ValueError as exc:
            raise ExecutionError(
                "FC_CONFIG_INVALID", "DEVPILOT_FC_MAX_TIMEOUT_SECONDS must be an integer"
            ) from exc
        if not 1 <= max_timeout_seconds <= 900:
            raise ExecutionError(
                "FC_CONFIG_INVALID", "DEVPILOT_FC_MAX_TIMEOUT_SECONDS must be between 1 and 900"
            )
        network_policy_mode = os.environ.get("DEVPILOT_FC_NETWORK_POLICY_MODE", "required").strip()
        if network_policy_mode not in {"required", "provider-default"}:
            raise ExecutionError(
                "FC_CONFIG_INVALID",
                "DEVPILOT_FC_NETWORK_POLICY_MODE must be required or provider-default",
            )
        domain = required("E2B_DOMAIN")
        allowed_project_paths = tuple(
            str(os.path.abspath(item.strip()))
            for item in os.environ.get("DEVPILOT_FC_ALLOWED_PROJECTS", "").split(os.pathsep)
            if item.strip()
        )
        return cls(
            api_key=required("E2B_API_KEY"),
            api_url=required("E2B_API_URL"),
            domain=domain,
            template=os.environ.get("DEVPILOT_FC_TEMPLATE", "code-interpreter-v1").strip()
            or "code-interpreter-v1",
            allow_network=os.environ.get("DEVPILOT_FC_ALLOW_NETWORK", "0") == "1",
            max_timeout_seconds=max_timeout_seconds,
            region=os.environ.get("DEVPILOT_FC_REGION", domain.split(".", 1)[0]).strip(),
            network_policy_mode=network_policy_mode,
            allowed_project_paths=allowed_project_paths,
        )


def _sandbox_class() -> Any:
    try:
        module = importlib.import_module("e2b_code_interpreter")
        return module.Sandbox
    except (ImportError, AttributeError) as exc:
        raise ExecutionError(
            "FC_SDK_MISSING", "install the optional fc-sandbox dependencies before using FC backend"
        ) from exc


def _sandbox_id(sandbox: Any) -> str:
    value = getattr(sandbox, "sandbox_id", None) or getattr(sandbox, "id", None)
    return str(value or "unknown-sandbox")


@dataclass(slots=True)
class _FCSandboxRun:
    run_id: str
    sandbox_id: str | None = None
    status: str = "starting"
    handle: Any | None = None
    result: ExecutionResult | None = None
    error_code: str | None = None
    cleanup_error: str | None = None
    cancel_requested: bool = False
    cleanup_done: bool = False
    cleanup_attempts: int = 0


class FCSandboxBackend:
    """Run an explicit verification bundle in a short-lived FC Sandbox.

    ``run`` remains the execution-port entry point used by the control plane.
    Lifecycle methods provide bounded cancellation, collection, and recovery
    receipts without creating a second execution framework.
    """

    name = "fc-agent-sandbox"

    def __init__(self, config: FCSandboxConfig) -> None:
        self.config = config
        self._lock = RLock()
        self._runs: dict[str, _FCSandboxRun] = {}

    @property
    def verification_available(self) -> bool:
        """Expose the explicitly opted-in degraded FC mode to the control plane."""

        return self.config.network_policy_mode == "provider-default"

    @property
    def supported_profiles(self) -> frozenset[str]:
        """Profiles with real FC evidence; React static remains Fake-only."""

        return FC_VERIFICATION_PROFILES

    def supports_profile(self, profile: str) -> bool:
        return profile in self.supported_profiles

    def project_allowed(self, project_path: str) -> bool:
        """Require an explicit operator allowlist before sending a snapshot to FC."""

        try:
            resolved = os.path.abspath(project_path)
        except (TypeError, ValueError):
            return False
        return resolved in self.config.allowed_project_paths

    def _reserve_run(self, run_id: str) -> _FCSandboxRun:
        with self._lock:
            if run_id in self._runs:
                raise ExecutionError("FC_RUN_DUPLICATE", "FC run ID has already been used")
            state = _FCSandboxRun(run_id=run_id)
            self._runs[run_id] = state
            return state

    def _state_payload(self, state: _FCSandboxRun) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": state.run_id,
            "sandbox_id": state.sandbox_id,
            "status": state.status,
            "error_code": state.error_code,
            "cleanup_error": state.cleanup_error,
            "cleanup_attempts": state.cleanup_attempts,
            "cleanup_status": (
                "cleaned"
                if state.cleanup_done
                else "manual_pending"
                if state.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS
                else "pending"
            ),
            "network_policy": self.config.network_policy_mode,
        }
        if state.result is not None:
            payload["result"] = {
                "backend": state.result.backend,
                "sandbox_id": state.result.sandbox_id,
                "exit_code": state.result.exit_code,
                "stdout": state.result.stdout,
                "stderr": state.result.stderr,
                "duration_ms": state.result.duration_ms,
                "command_profile": state.result.command_profile,
                "bundle_bytes": state.result.bundle_bytes,
                "region": state.result.region,
                "template": state.result.template,
                "network_policy": state.result.network_policy,
            }
        return payload

    def get_run_state(self, run_id: str) -> dict[str, Any]:
        """Return a bounded lifecycle receipt, or unknown after a restart."""

        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return {
                    "run_id": run_id,
                    "sandbox_id": None,
                    "status": "unknown",
                    "error_code": "FC_RUN_NOT_TRACKED",
                    "cleanup_error": None,
                    "cleanup_attempts": 0,
                    "cleanup_status": "unknown",
                    "network_policy": self.config.network_policy_mode,
                }
            return self._state_payload(state)

    def collect(self, run_id: str) -> dict[str, Any]:
        """Collect the persisted in-process result without starting a task."""

        return self.get_run_state(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Cancel one active run; repeated cancellation is idempotent."""

        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return self.get_run_state(run_id)
            if state.status not in {"starting", "running"} and not state.cleanup_error:
                return self._state_payload(state)
            state.cancel_requested = True
            handle = state.handle
        if handle is None:
            return self.get_run_state(run_id)
        with self._lock:
            if state.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS:
                return self._state_payload(state)
            state.cleanup_attempts += 1
        try:
            handle.kill()
        except Exception as exc:
            with self._lock:
                state.cleanup_error = _provider_error("cleanup", exc).code
            return self.get_run_state(run_id)
        with self._lock:
            state.cleanup_done = True
            state.cleanup_error = None
        return self.get_run_state(run_id)

    def cleanup(self, run_id: str) -> dict[str, Any]:
        """Clean up an active sandbox without creating a replacement task."""

        return self.cancel(run_id)

    def recover(self, run_id: str, sandbox_id: str) -> dict[str, Any]:
        """Reconnect to a provider sandbox, or persist an explicit unknown state."""

        if not run_id or not sandbox_id:
            raise ExecutionError("FC_RECOVERY_INVALID", "run_id and sandbox_id are required")
        with self._lock:
            existing = self._runs.get(run_id)
            if existing is not None and existing.handle is not None:
                return self._state_payload(existing)
        try:
            sandbox_cls = _sandbox_class()
            connector = getattr(sandbox_cls, "connect", None)
            if not callable(connector):
                raise RuntimeError("FC SDK does not expose Sandbox.connect")
            handle = connector(
                sandbox_id,
                api_key=self.config.api_key,
                api_url=self.config.api_url,
                domain=self.config.domain,
            )
            running = bool(handle.is_running())
        except Exception:
            with self._lock:
                state = self._runs.setdefault(run_id, _FCSandboxRun(run_id=run_id))
                state.sandbox_id = sandbox_id
                state.status = "unknown"
                state.error_code = "FC_RECOVERY_UNKNOWN"
                return self._state_payload(state)
        with self._lock:
            state = self._runs.setdefault(run_id, _FCSandboxRun(run_id=run_id))
            state.sandbox_id = sandbox_id
            if running:
                state.status = "running"
                state.handle = handle
                state.error_code = None
            else:
                state.status = "unknown"
                state.error_code = "FC_RESULT_UNAVAILABLE"
            return self._state_payload(state)

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        validate_request(request)
        if request.timeout_seconds > self.config.max_timeout_seconds:
            raise ExecutionError(
                "FC_COST_CAP_EXCEEDED",
                f"sandbox timeout exceeds the {self.config.max_timeout_seconds}s cost cap",
            )
        if request.network_allowed and not self.config.allow_network:
            raise ExecutionError(
                "FC_NETWORK_DENIED", "network access is disabled by DevPilot policy"
            )
        state = self._reserve_run(request.run_id)
        sandbox_cls = _sandbox_class()
        sandbox: Any | None = None
        started = time.monotonic()
        stage = "create"
        primary_error: ExecutionError | None = None
        try:
            create_kwargs: dict[str, Any] = {
                "template": self.config.template,
                "api_key": self.config.api_key,
                "api_url": self.config.api_url,
                "domain": self.config.domain,
                "timeout": request.timeout_seconds,
            }
            if self.config.network_policy_mode == "required":
                create_kwargs["allow_internet_access"] = (
                    self.config.allow_network and request.network_allowed
                )
            sandbox_handle: Any = sandbox_cls.create(**create_kwargs)
            if sandbox_handle is None:
                raise ExecutionError("FC_SANDBOX_CREATE_FAILED", "FC Sandbox returned no handle")
            sandbox = sandbox_handle
            with self._lock:
                state.handle = sandbox_handle
                state.sandbox_id = _sandbox_id(sandbox_handle)
                state.status = "running"
                if state.cancel_requested:
                    raise ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
            stage = "upload"
            for item in request.files:
                relative_path = item.relative_path.replace("\\", "/")
                target = f"{SANDBOX_ROOT}/{relative_path}"
                parent = target.rsplit("/", 1)[0]
                sandbox_handle.files.make_dir(parent)
                sandbox_handle.files.write(target, item.content)
            with self._lock:
                if state.cancel_requested:
                    raise ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
            stage = "execute"
            try:
                result = sandbox_handle.commands.run(
                    request.command, timeout=request.timeout_seconds
                )
            except Exception as exc:
                if not isinstance(getattr(exc, "exit_code", None), int):
                    raise
                result = exc
            with self._lock:
                if state.cancel_requested:
                    raise ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
            receipt = ExecutionResult(
                backend=self.name,
                sandbox_id=state.sandbox_id or _sandbox_id(sandbox_handle),
                exit_code=int(getattr(result, "exit_code", 0)),
                stdout=str(getattr(result, "stdout", ""))[-MAX_EXECUTION_OUTPUT_BYTES:],
                stderr=str(getattr(result, "stderr", ""))[-MAX_EXECUTION_OUTPUT_BYTES:],
                duration_ms=int((time.monotonic() - started) * 1000),
                command_profile=request.command_profile,
                bundle_bytes=execution_bundle_bytes(request),
                region=self.config.region,
                template=self.config.template,
                network_policy=self.config.network_policy_mode,
            )
            with self._lock:
                state.result = receipt
                state.status = "completed" if receipt.exit_code == 0 else "failed"
            return receipt
        except ExecutionError as exc:
            primary_error = exc
            with self._lock:
                state.error_code = exc.code
                state.status = "cancelled" if exc.code == "FC_CANCELLED" else "failed"
            raise
        except Exception as exc:
            with self._lock:
                cancelled = state.cancel_requested
            mapped = (
                ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
                if cancelled
                else _provider_error(stage, exc)
            )
            primary_error = mapped
            with self._lock:
                state.error_code = mapped.code
                state.status = "failed"
            raise mapped from exc
        finally:
            with self._lock:
                cleanup_done = state.cleanup_done
            if sandbox is not None and not cleanup_done:
                try:
                    with self._lock:
                        state.cleanup_attempts += 1
                    sandbox.kill()
                    with self._lock:
                        state.cleanup_done = True
                        state.cleanup_error = None
                except Exception as exc:
                    cleanup_error = _provider_error("cleanup", exc)
                    with self._lock:
                        state.cleanup_error = cleanup_error.code
                        cleanup_error.details.update(
                            {
                                "sandbox_id": state.sandbox_id,
                                "cleanup_status": (
                                    "manual_pending"
                                    if state.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS
                                    else "pending"
                                ),
                                "cleanup_error": cleanup_error.code,
                            }
                        )
                        if primary_error is not None:
                            primary_error.details.update(cleanup_error.details)
                        if primary_error is None:
                            state.status = "cleanup_failed"
                            state.error_code = cleanup_error.code
                    if primary_error is None:
                        raise cleanup_error from exc
            with self._lock:
                if state.cleanup_done:
                    state.handle = None


def build_fc_backend_from_environment() -> ExecutionBackend:
    """Create the optional backend only when the caller explicitly requests it."""

    return FCSandboxBackend(FCSandboxConfig.from_environment())
