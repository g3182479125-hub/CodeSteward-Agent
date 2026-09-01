from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from devpilot.execution import (
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
    FakeExecutionBackend,
    request_from_mapping,
)
from devpilot.fc_sandbox import FCSandboxBackend, FCSandboxConfig


def valid_request(**overrides: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "run_id": "run-1",
        "project_id": "project-1",
        "files": (ExecutionFile("src/main.py", b"print('ok')"),),
        "command": "python3 src/main.py",
    }
    values.update(overrides)
    return ExecutionRequest(**values)  # type: ignore[arg-type]


def test_fake_backend_keeps_execution_port_deterministic() -> None:
    backend = FakeExecutionBackend()
    result = backend.run(valid_request())

    assert result.backend == "fake"
    assert result.exit_code == 0
    assert backend.requests[0].files[0].relative_path == "src/main.py"


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"files": ()}, "EXECUTION_INPUT_EMPTY"),
        ({"command": "  "}, "EXECUTION_COMMAND_EMPTY"),
        ({"timeout_seconds": 901}, "EXECUTION_TIMEOUT_INVALID"),
        ({"files": (ExecutionFile("../escape.py", b"x"),)}, "EXECUTION_PATH_INVALID"),
        (
            {"files": (ExecutionFile("src/main.py", b"x"), ExecutionFile("src/main.py", b"y"))},
            "EXECUTION_PATH_DUPLICATE",
        ),
    ],
)
def test_execution_port_fails_closed(overrides: dict[str, object], code: str) -> None:
    with pytest.raises(ExecutionError, match=code):
        FakeExecutionBackend().run(valid_request(**overrides))


def test_fc_config_requires_environment_without_printing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ExecutionError, match="FC_CONFIG_MISSING"):
        FCSandboxConfig.from_environment()


def test_mapping_parser_accepts_internal_bytes_only() -> None:
    request = request_from_mapping(
        {
            "run_id": "run-1",
            "project_id": "project-1",
            "command": "python3 src/main.py",
            "files": [{"relative_path": "src/main.py", "content": b"print(1)"}],
        }
    )
    assert request.files[0].content == b"print(1)"


def test_fc_backend_uses_short_lived_sandbox_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class FakeFiles:
        def make_dir(self, path: str) -> None:
            events.append(("mkdir", path))

        def write(self, path: str, content: bytes) -> None:
            events.append(("write", path, content))

    class FakeCommands:
        def run(self, command: str, *, timeout: int) -> SimpleNamespace:
            events.append(("run", command, timeout))
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    class FakeSandbox:
        def __init__(self) -> None:
            self.sandbox_id = "fc-test-sandbox"
            self.files = FakeFiles()
            self.commands = FakeCommands()

        def kill(self) -> None:
            events.append("kill")

    class FakeSandboxFactory:
        @classmethod
        def create(cls, **kwargs: object) -> FakeSandbox:
            events.append(("create", kwargs))
            return FakeSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=FakeSandboxFactory),
    )
    result = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
        )
    ).run(valid_request())

    assert result.sandbox_id == "fc-test-sandbox"
    assert result.stdout == "ok"
    assert events[0][0] == "create"
    assert ("write", "/workspace/devpilot/src/main.py", b"print('ok')") in events
    assert events[-1] == "kill"


def test_fc_backend_rejects_network_before_provider_call() -> None:
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
        )
    )
    with pytest.raises(ExecutionError, match="FC_NETWORK_DENIED"):
        backend.run(valid_request(network_allowed=True))
