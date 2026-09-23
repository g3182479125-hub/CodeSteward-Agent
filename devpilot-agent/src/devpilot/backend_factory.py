"""Select the project verification backend from operator-owned settings."""

from __future__ import annotations

import os
from pathlib import Path

from .execution import ExecutionBackend, ExecutionError
from .local_execution import LocalExecutionBackend

FC_SANDBOX_SETTING = "DEVPILOT_ENABLE_FC_SANDBOX"


def fc_sandbox_enabled() -> bool:
    """Return the explicit FC opt-in state; unset always means disabled."""

    value = os.environ.get(FC_SANDBOX_SETTING, "0").strip()
    if value not in {"0", "1"}:
        raise ExecutionError(
            "FC_CONFIG_INVALID",
            f"{FC_SANDBOX_SETTING} must be exactly 0 or 1",
        )
    return value == "1"


def build_project_execution_backend(*, state_dir: str | Path) -> ExecutionBackend:
    """Build local execution by default; FC requires the explicit setting."""

    if fc_sandbox_enabled():
        from .fc_sandbox import build_fc_backend_from_environment

        return build_fc_backend_from_environment()
    return LocalExecutionBackend(state_dir=state_dir)
