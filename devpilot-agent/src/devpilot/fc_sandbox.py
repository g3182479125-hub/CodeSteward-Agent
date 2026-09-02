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
        domain = required("E2B_DOMAIN")
        return cls(
            api_key=required("E2B_API_KEY"),
            api_url=required("E2B_API_URL"),
            domain=domain,
            template=os.environ.get("DEVPILOT_FC_TEMPLATE", "code-interpreter-v1").strip()
            or "code-interpreter-v1",
            allow_network=os.environ.get("DEVPILOT_FC_ALLOW_NETWORK", "0") == "1",
            max_timeout_seconds=max_timeout_seconds,
            region=os.environ.get("DEVPILOT_FC_REGION", domain.split(".", 1)[0]).strip(),
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


class FCSandboxBackend:
    """Run an explicit verification bundle in a short-lived FC Sandbox."""

    name = "fc-agent-sandbox"

    def __init__(self, config: FCSandboxConfig) -> None:
        self.config = config

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
        sandbox_cls = _sandbox_class()
        sandbox: Any | None = None
        started = time.monotonic()
        try:
            sandbox_handle: Any = sandbox_cls.create(
                template=self.config.template,
                api_key=self.config.api_key,
                api_url=self.config.api_url,
                domain=self.config.domain,
                timeout=request.timeout_seconds,
                allow_internet_access=self.config.allow_network and request.network_allowed,
            )
            if sandbox_handle is None:
                raise ExecutionError("FC_SANDBOX_CREATE_FAILED", "FC Sandbox returned no handle")
            sandbox = sandbox_handle
            for item in request.files:
                relative_path = item.relative_path.replace("\\", "/")
                target = f"{SANDBOX_ROOT}/{relative_path}"
                parent = target.rsplit("/", 1)[0]
                sandbox_handle.files.make_dir(parent)
                sandbox_handle.files.write(target, item.content)
            result = sandbox_handle.commands.run(
                request.command, timeout=request.timeout_seconds
            )
            return ExecutionResult(
                backend=self.name,
                sandbox_id=_sandbox_id(sandbox_handle),
                exit_code=int(getattr(result, "exit_code", 0)),
                stdout=str(getattr(result, "stdout", ""))[-MAX_EXECUTION_OUTPUT_BYTES:],
                stderr=str(getattr(result, "stderr", ""))[-MAX_EXECUTION_OUTPUT_BYTES:],
                duration_ms=int((time.monotonic() - started) * 1000),
                command_profile=request.command_profile,
                bundle_bytes=execution_bundle_bytes(request),
                region=self.config.region,
                template=self.config.template,
            )
        except ExecutionError:
            raise
        except Exception as exc:
            raise ExecutionError("FC_EXECUTION_FAILED", "FC Sandbox execution failed") from exc
        finally:
            if sandbox is not None:
                try:
                    sandbox.kill()
                except Exception:
                    # The primary execution result/error must not be replaced by cleanup noise.
                    pass


def build_fc_backend_from_environment() -> ExecutionBackend:
    """Create the optional backend only when the caller explicitly requests it."""

    return FCSandboxBackend(FCSandboxConfig.from_environment())
